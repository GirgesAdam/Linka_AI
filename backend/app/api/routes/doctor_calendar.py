from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess
from app.api.dependencies.page_permissions import require_page_permission
from app.database.session import get_db
from app.models.appointment import Appointment
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff

router = APIRouter()


class DoctorCalendarDoctorRead(BaseModel):
    id: UUID
    name: str
    specialization: str | None = None


class DoctorCalendarEventRead(BaseModel):
    appointment_id: UUID
    doctor_id: UUID
    doctor_name: str
    patient_id: UUID
    patient_name: str
    service_id: UUID
    service_name: str
    status: str
    start_at: datetime
    end_at: datetime
    laser_device_name: str | None = None


class DoctorCalendarRead(BaseModel):
    timezone: str
    start_date: date
    end_date: date
    doctors: list[DoctorCalendarDoctorRead]
    events: list[DoctorCalendarEventRead]


def _workspace_timezone(access: WorkspaceAccess) -> ZoneInfo:
    name = (access.workspace.timezone or "Africa/Cairo").strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Africa/Cairo")


@router.get("/doctor-calendar", response_model=DoctorCalendarRead)
def get_doctor_calendar(
    start_date: date,
    end_date: date,
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("appointments.calendar", legacy_access="reader"))],
    db: Annotated[Session, Depends(get_db)],
    doctor_id: UUID | None = None,
) -> DoctorCalendarRead:
    """Return one bounded calendar range with doctor and appointment display data."""
    if end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_date must be on or after start_date.",
        )
    if (end_date - start_date).days > 41:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Doctor calendar ranges cannot exceed 42 days.",
        )

    timezone = _workspace_timezone(access)
    start_local = datetime.combine(start_date, time.min, tzinfo=timezone)
    end_local = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone)
    start_utc = start_local.astimezone(UTC)
    end_utc = end_local.astimezone(UTC)

    doctor_stmt = (
        select(Doctor, Staff)
        .join(
            Staff,
            (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
        )
        .where(
            Doctor.workspace_id == access.workspace.id,
            Doctor.is_active.is_(True),
            Doctor.booking_enabled.is_(True),
            Staff.is_active.is_(True),
        )
        .order_by(Staff.first_name, Staff.last_name)
    )
    if doctor_id is not None:
        doctor_stmt = doctor_stmt.where(Doctor.id == doctor_id)
    doctor_rows = list(db.execute(doctor_stmt).all())

    event_stmt = (
        select(Appointment, Patient, Service, Doctor, Staff)
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
            Doctor,
            (Doctor.workspace_id == Appointment.workspace_id)
            & (Doctor.id == Appointment.doctor_id),
        )
        .join(
            Staff,
            (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
        )
        .where(
            Appointment.workspace_id == access.workspace.id,
            Appointment.start_at >= start_utc,
            Appointment.start_at < end_utc,
            Appointment.status.notin_(("cancelled", "rescheduled")),
        )
        .order_by(Appointment.start_at, Staff.first_name, Staff.last_name)
    )
    if doctor_id is not None:
        event_stmt = event_stmt.where(Appointment.doctor_id == doctor_id)
    event_rows = list(db.execute(event_stmt).all())

    doctors = [
        DoctorCalendarDoctorRead(
            id=doctor.id,
            name=f"{staff.first_name or ''} {staff.last_name or ''}".strip() or "دكتور",
            specialization=doctor.specialization,
        )
        for doctor, staff in doctor_rows
    ]
    events = [
        DoctorCalendarEventRead(
            appointment_id=appointment.id,
            doctor_id=doctor.id,
            doctor_name=f"{staff.first_name or ''} {staff.last_name or ''}".strip() or "دكتور",
            patient_id=patient.id,
            patient_name=f"{patient.first_name or ''} {patient.last_name or ''}".strip() or "عميل",
            service_id=service.id,
            service_name=service.name,
            status=appointment.status,
            start_at=appointment.start_at,
            end_at=appointment.end_at,
            laser_device_name=appointment.laser_device_name,
        )
        for appointment, patient, service, doctor, staff in event_rows
    ]
    return DoctorCalendarRead(
        timezone=getattr(timezone, "key", str(timezone)),
        start_date=start_date,
        end_date=end_date,
        doctors=doctors,
        events=events,
    )
