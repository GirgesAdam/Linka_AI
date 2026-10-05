from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess, get_workspace_reader
from app.database.session import get_db
from app.models.appointment import Appointment
from app.models.automation_job import AutomationJob
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.channel_connection import ChannelConnection
from app.models.doctor import Doctor
from app.models.handoff_request import HandoffRequest
from app.models.patient import Patient
from app.models.payment_transaction import PaymentTransaction
from app.models.service import Service
from app.models.staff import Staff
from app.schemas.dashboard import (
    DashboardAppointmentRead,
    DashboardSummaryRead,
    DashboardTodayRead,
    DashboardTodayRevenueRead,
)

router = APIRouter()


def _summarize_today_revenue(rows, *, currency: str) -> DashboardTodayRevenueRead:
    currency = currency.upper()
    gross_collected_minor = 0
    refunds_minor = 0
    net_by_method = {"cash": 0, "visa": 0, "instapay": 0, "other": 0}
    for method, row_currency, transaction_type, amount in rows:
        if str(row_currency).upper() != currency:
            continue
        value = int(amount or 0)
        bucket = str(method) if str(method) in {"cash", "visa", "instapay"} else "other"
        if transaction_type == "payment":
            gross_collected_minor += value
            net_by_method[bucket] += value
        elif transaction_type == "refund":
            refunds_minor += value
            net_by_method[bucket] -= value
    return DashboardTodayRevenueRead(
        currency=currency,
        gross_collected_minor=gross_collected_minor,
        refunds_minor=refunds_minor,
        total_minor=gross_collected_minor - refunds_minor,
        cash_minor=net_by_method["cash"],
        visa_minor=net_by_method["visa"],
        instapay_minor=net_by_method["instapay"],
        other_minor=net_by_method["other"],
    )


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Africa/Cairo")


def _today_bounds(timezone_name: str, *, now: datetime | None = None) -> tuple[ZoneInfo, datetime, datetime, datetime]:
    reference = now or datetime.now(UTC)
    tz = _timezone(timezone_name)
    local_today = reference.astimezone(tz).date()
    start_local = datetime.combine(local_today, time.min, tzinfo=tz)
    start_utc = start_local.astimezone(UTC)
    next_day_start_utc = (start_local + timedelta(days=1)).astimezone(UTC)
    return tz, reference, start_utc, next_day_start_utc


def _appointment_reads(rows) -> list[DashboardAppointmentRead]:
    return [
        DashboardAppointmentRead(
            id=appointment.id,
            patient_id=patient.id,
            patient_name=f"{patient.first_name} {patient.last_name or ''}".strip(),
            service_name=service.name,
            branch_name=branch.name,
            doctor_name=f"{staff.first_name} {staff.last_name}".strip(),
            status=appointment.status,
            start_at=appointment.start_at,
            end_at=appointment.end_at,
            price_minor=appointment.price_minor,
            currency=appointment.currency,
        )
        for appointment, patient, service, branch, staff in rows
    ]


def _appointment_detail_stmt(workspace_id):
    return (
        select(Appointment, Patient, Service, Branch, Staff)
        .join(
            Patient,
            (Patient.workspace_id == Appointment.workspace_id)
            & (Patient.id == Appointment.patient_id),
        )
        .join(
            Service,
            (Service.workspace_id == Appointment.workspace_id)
            & (Service.id == Appointment.service_id),
        )
        .join(
            Branch,
            (Branch.workspace_id == Appointment.workspace_id)
            & (Branch.id == Appointment.branch_id),
        )
        .join(
            Doctor,
            (Doctor.workspace_id == Appointment.workspace_id)
            & (Doctor.id == Appointment.doctor_id),
        )
        .join(Staff, (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id))
        .where(Appointment.workspace_id == workspace_id)
    )


@router.get("/today", response_model=DashboardTodayRead)
def dashboard_today(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> DashboardTodayRead:
    workspace_id = access.workspace.id
    tz, now, start_utc, next_day_start_utc = _today_bounds(access.workspace.timezone)
    rows = db.execute(
        _appointment_detail_stmt(workspace_id)
        .where(
            Appointment.start_at >= start_utc,
            Appointment.start_at < next_day_start_utc,
            Appointment.status.in_((
                "pending",
                "confirmed",
                "checked_in",
                "in_progress",
                "completed",
                "cancelled",
                "no_show",
            )),
        )
        .order_by(Appointment.start_at, Appointment.id)
    ).all()
    next_appointment_id = next(
        (
            appointment.id
            for appointment, _patient, _service, _branch, _staff in rows
            if appointment.status in {"pending", "confirmed", "checked_in", "in_progress"}
            and appointment.end_at >= now
        ),
        None,
    )
    return DashboardTodayRead(
        timezone=tz.key,
        local_date=now.astimezone(tz).date().isoformat(),
        appointments=_appointment_reads(rows),
        next_appointment_id=next_appointment_id,
    )


@router.get("/today-revenue", response_model=DashboardTodayRevenueRead)
def dashboard_today_revenue(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> DashboardTodayRevenueRead:
    workspace_id = access.workspace.id
    _tz, _now, start_utc, next_day_start_utc = _today_bounds(access.workspace.timezone)
    rows = db.execute(
        select(
            PaymentTransaction.payment_method,
            PaymentTransaction.currency,
            PaymentTransaction.transaction_type,
            func.coalesce(func.sum(PaymentTransaction.amount_minor), 0),
        )
        .where(
            PaymentTransaction.workspace_id == workspace_id,
            PaymentTransaction.created_at >= start_utc,
            PaymentTransaction.created_at < next_day_start_utc,
        )
        .group_by(
            PaymentTransaction.payment_method,
            PaymentTransaction.currency,
            PaymentTransaction.transaction_type,
        )
    ).all()
    currency = str(
        db.scalar(
            select(BookingSettings.default_currency).where(
                BookingSettings.workspace_id == workspace_id
            )
        )
        or "EGP"
    ).upper()
    return _summarize_today_revenue(rows, currency=currency)


@router.get("/summary", response_model=DashboardSummaryRead)
def dashboard_summary(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> DashboardSummaryRead:
    workspace_id = access.workspace.id
    now = datetime.now(UTC)
    tz = _timezone(access.workspace.timezone)
    local_today = now.astimezone(tz).date()
    start_local = datetime.combine(local_today, time.min, tzinfo=tz)
    end_local = datetime.combine(local_today, time.max, tzinfo=tz)
    start_utc, end_utc = start_local.astimezone(UTC), end_local.astimezone(UTC)

    active_patients = _count(
        db,
        select(func.count())
        .select_from(Patient)
        .where(Patient.workspace_id == workspace_id, Patient.status == "active"),
    )
    appointments_today = _count(
        db,
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.start_at >= start_utc,
            Appointment.start_at <= end_utc,
            Appointment.status.notin_(("cancelled", "rescheduled")),
        ),
    )
    upcoming = _count(
        db,
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.start_at >= now,
            Appointment.status.in_(("pending", "confirmed")),
        ),
    )
    appointments_after_today = _count(
        db,
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.start_at > end_utc,
            Appointment.status.in_(("pending", "confirmed")),
        ),
    )
    handoffs = _count(
        db,
        select(func.count())
        .select_from(HandoffRequest)
        .where(
            HandoffRequest.workspace_id == workspace_id,
            HandoffRequest.status.in_(("pending", "claimed")),
        ),
    )
    channels = _count(
        db,
        select(func.count())
        .select_from(ChannelConnection)
        .where(
            ChannelConnection.workspace_id == workspace_id, ChannelConnection.status == "active"
        ),
    )
    failed_jobs = _count(
        db,
        select(func.count())
        .select_from(AutomationJob)
        .where(AutomationJob.workspace_id == workspace_id, AutomationJob.status == "failed"),
    )

    next_day_start_utc = (start_local + timedelta(days=1)).astimezone(UTC)
    revenue_rows = db.execute(
        select(
            PaymentTransaction.payment_method,
            PaymentTransaction.currency,
            PaymentTransaction.transaction_type,
            func.coalesce(func.sum(PaymentTransaction.amount_minor), 0),
        )
        .where(
            PaymentTransaction.workspace_id == workspace_id,
            PaymentTransaction.created_at >= start_utc,
            PaymentTransaction.created_at < next_day_start_utc,
        )
        .group_by(
            PaymentTransaction.payment_method,
            PaymentTransaction.currency,
            PaymentTransaction.transaction_type,
        )
    ).all()
    revenue_currency = str(
        db.scalar(
            select(BookingSettings.default_currency).where(
                BookingSettings.workspace_id == workspace_id
            )
        )
        or "EGP"
    ).upper()
    appointment_detail_stmt = (
        select(Appointment, Patient, Service, Branch, Staff)
        .join(
            Patient,
            (Patient.workspace_id == Appointment.workspace_id)
            & (Patient.id == Appointment.patient_id),
        )
        .join(
            Service,
            (Service.workspace_id == Appointment.workspace_id)
            & (Service.id == Appointment.service_id),
        )
        .join(
            Branch,
            (Branch.workspace_id == Appointment.workspace_id)
            & (Branch.id == Appointment.branch_id),
        )
        .join(
            Doctor,
            (Doctor.workspace_id == Appointment.workspace_id)
            & (Doctor.id == Appointment.doctor_id),
        )
        .join(Staff, (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id))
        .where(Appointment.workspace_id == workspace_id)
    )

    def appointment_reads(rows) -> list[DashboardAppointmentRead]:
        return [
            DashboardAppointmentRead(
                id=appointment.id,
                patient_id=patient.id,
                patient_name=f"{patient.first_name} {patient.last_name or ''}".strip(),
                service_name=service.name,
                branch_name=branch.name,
                doctor_name=f"{staff.first_name} {staff.last_name}".strip(),
                status=appointment.status,
                start_at=appointment.start_at,
                end_at=appointment.end_at,
                price_minor=appointment.price_minor,
                currency=appointment.currency,
            )
            for appointment, patient, service, branch, staff in rows
        ]

    recent_rows = db.execute(
        appointment_detail_stmt.where(
            Appointment.start_at >= now,
            Appointment.status.in_(("pending", "confirmed")),
        )
        .order_by(Appointment.start_at)
        .limit(8)
    ).all()
    today_rows = db.execute(
        appointment_detail_stmt.where(
            Appointment.start_at >= start_utc,
            Appointment.start_at <= end_utc,
            Appointment.status.in_(("pending", "confirmed", "checked_in", "in_progress", "completed")),
        )
        .order_by(Appointment.start_at)
    ).all()
    next_rows = db.execute(
        appointment_detail_stmt.where(
            Appointment.start_at > end_utc,
            Appointment.status.in_(("pending", "confirmed")),
        )
        .order_by(Appointment.start_at)
        .limit(6)
    ).all()

    return DashboardSummaryRead(
        timezone=tz.key,
        active_patients=active_patients,
        appointments_today=appointments_today,
        upcoming_appointments=upcoming,
        appointments_after_today=appointments_after_today,
        open_handoffs=handoffs,
        active_channels=channels,
        failed_automation_jobs=failed_jobs,
        recent_appointments=appointment_reads(recent_rows),
        today_appointments=appointment_reads(today_rows),
        next_appointments=appointment_reads(next_rows),
        today_revenue=_summarize_today_revenue(revenue_rows, currency=revenue_currency),
    )
