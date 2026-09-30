from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess, get_workspace_reader
from app.database.session import get_db
from app.models.appointment import Appointment
from app.models.automation_job import AutomationJob
from app.models.branch import Branch
from app.models.channel_connection import ChannelConnection
from app.models.doctor import Doctor
from app.models.handoff_request import HandoffRequest
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.schemas.dashboard import DashboardAppointmentRead, DashboardSummaryRead

router = APIRouter()


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Africa/Cairo")


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
            Appointment.status.in_(("pending", "confirmed", "checked_in", "in_progress")),
        )
        .order_by(Appointment.start_at)
        .limit(8)
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
    )
