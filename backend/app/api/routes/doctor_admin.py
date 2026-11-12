from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies.section_access import require_section_access
from app.api.dependencies.security import WorkspaceAccess
from app.database.session import get_db
from app.models.appointment import ACTIVE_APPOINTMENT_STATUSES, Appointment
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_laser_weekday_exclusion import DoctorLaserWeekdayExclusion
from app.models.doctor_service import DoctorService
from app.models.staff import Staff
from app.models.working_hours import DoctorWorkingHour
from app.models.workspace import Workspace
from app.schemas.clinic import DoctorRead, DoctorWorkingHourRead, WorkingHoursReplace
from app.services.activity import record_activity_event
from app.services.doctor_categories import (
    categories_from_service_ids,
    doctor_service_categories,
    normalized_service_categories,
    sync_doctor_service_assignments,
)

router = APIRouter()


class DoctorAdminCreate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    first_name: str | None = Field(default=None, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    specialization: str | None = Field(default=None, max_length=200)
    service_categories: list[Literal["laser", "dermatology", "slimming"]] | None = None
    service_ids: list[UUID] = Field(default_factory=list, max_length=200)
    working_hours: WorkingHoursReplace = Field(default_factory=WorkingHoursReplace)
    booking_enabled: bool = True


class DoctorAdminUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    first_name: str | None = Field(default=None, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    specialization: str | None = Field(default=None, max_length=200)
    service_categories: list[Literal["laser", "dermatology", "slimming"]] | None = None
    service_ids: list[UUID] = Field(default_factory=list, max_length=200)
    booking_enabled: bool = True


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _stored_doctor_name(
    *,
    name: str | None,
    first_name: str | None,
    last_name: str | None,
) -> tuple[str, str]:
    """Accept the new single-name contract and the previous split-name form safely."""
    if name is not None:
        return (name.strip(), "")
    return ((first_name or "").strip(), (last_name or "").strip())


def _requested_categories(
    db: Session,
    *,
    workspace_id: UUID,
    service_categories: list[str] | None,
    service_ids: list[UUID] | None,
    current: list[str] | None = None,
) -> list[str]:
    try:
        if service_categories is not None:
            return normalized_service_categories(service_categories)
        if service_ids is not None:
            return categories_from_service_ids(db, workspace_id=workspace_id, service_ids=service_ids)
        return normalized_service_categories(current)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _operational_branch(db: Session, workspace: Workspace) -> Branch:
    """Resolve the hidden scheduling location used by the branchless product UI."""
    branch = db.scalar(
        select(Branch)
        .where(
            Branch.workspace_id == workspace.id,
            Branch.is_active.is_(True),
            func.lower(Branch.name) == workspace.name.strip().lower(),
        )
        .order_by(Branch.created_at)
    )
    if branch is None:
        branch = db.scalar(
            select(Branch)
            .where(Branch.workspace_id == workspace.id, Branch.is_active.is_(True))
            .order_by(Branch.created_at)
        )
    if branch is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Clinic scheduling location is not configured.",
        )
    return branch


def _doctor_operational_branch(
    db: Session,
    *,
    workspace: Workspace,
    doctor_id: UUID,
) -> Branch:
    assignment = db.execute(
        select(DoctorBranch, Branch)
        .join(
            Branch,
            (Branch.workspace_id == DoctorBranch.workspace_id)
            & (Branch.id == DoctorBranch.branch_id),
        )
        .where(
            DoctorBranch.workspace_id == workspace.id,
            DoctorBranch.doctor_id == doctor_id,
            DoctorBranch.is_active.is_(True),
            Branch.is_active.is_(True),
        )
        .order_by(DoctorBranch.is_primary.desc(), DoctorBranch.created_at)
    ).first()
    if assignment is not None:
        return assignment[1]

    branch = _operational_branch(db, workspace)
    db.add(
        DoctorBranch(
            workspace_id=workspace.id,
            doctor_id=doctor_id,
            branch_id=branch.id,
            is_primary=True,
            is_active=True,
        )
    )
    # SessionLocal deliberately uses autoflush=False. Persist this assignment now
    # so a second lookup in the same transaction sees it instead of creating a
    # duplicate (workspace, doctor, branch) row at commit time.
    db.flush()
    return branch


def _replace_doctor_hours(
    db: Session,
    *,
    workspace: Workspace,
    doctor_id: UUID,
    payload: WorkingHoursReplace,
) -> list[DoctorWorkingHour]:
    branch = _doctor_operational_branch(
        db,
        workspace=workspace,
        doctor_id=doctor_id,
    )
    rows = list(
        db.scalars(
            select(DoctorWorkingHour).where(
                DoctorWorkingHour.workspace_id == workspace.id,
                DoctorWorkingHour.doctor_id == doctor_id,
                DoctorWorkingHour.branch_id == branch.id,
            )
        )
    )
    for row in rows:
        db.delete(row)
    # SessionLocal has autoflush disabled, and the replacement payload often
    # retains some unchanged intervals. Flush deletions before inserting the
    # replacement rows so the unique working-hour constraint cannot see both
    # the old and new copy of an unchanged interval at the same time.
    if rows:
        db.flush()
    replacements = [
        DoctorWorkingHour(
            workspace_id=workspace.id,
            doctor_id=doctor_id,
            branch_id=branch.id,
            **interval.model_dump(),
        )
        for interval in payload.intervals
    ]
    db.add_all(replacements)
    active_days = {interval.weekday for interval in payload.intervals}
    for exclusion in db.scalars(select(DoctorLaserWeekdayExclusion).where(
        DoctorLaserWeekdayExclusion.workspace_id == workspace.id,
        DoctorLaserWeekdayExclusion.doctor_id == doctor_id,
        DoctorLaserWeekdayExclusion.branch_id == branch.id,
    )).all():
        if exclusion.weekday not in active_days:
            db.delete(exclusion)
    return replacements


def _commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc


@router.post("/doctor-admin", response_model=DoctorRead, status_code=status.HTTP_201_CREATED)
def create_doctor_from_admin(
    payload: DoctorAdminCreate,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
) -> Doctor:
    workspace = access.workspace
    categories = _requested_categories(
        db,
        workspace_id=workspace.id,
        service_categories=payload.service_categories,
        service_ids=(
            payload.service_ids
            if "service_ids" in payload.model_fields_set
            else None
        ),
        current=[],
    )
    first_name, last_name = _stored_doctor_name(
        name=payload.name,
        first_name=payload.first_name,
        last_name=payload.last_name,
    )

    staff = Staff(
        workspace_id=workspace.id,
        first_name=first_name,
        last_name=last_name,
        email=None,
        phone=_clean_optional(payload.phone),
        job_title="Doctor",
    )
    db.add(staff)
    db.flush()

    doctor = Doctor(
        workspace_id=workspace.id,
        staff_id=staff.id,
        doctor_type="regular",
        specialization=_clean_optional(payload.specialization),
        booking_enabled=payload.booking_enabled,
    )
    db.add(doctor)
    db.flush()

    _doctor_operational_branch(db, workspace=workspace, doctor_id=doctor.id)
    service_ids = sync_doctor_service_assignments(
        db, workspace_id=workspace.id, doctor=doctor, categories=categories
    )
    _replace_doctor_hours(
        db,
        workspace=workspace,
        doctor_id=doctor.id,
        payload=payload.working_hours,
    )
    record_activity_event(
        db,
        workspace_id=workspace.id,
        actor_type="staff",
        actor_user_id=access.user.id,
        action="clinic.doctor_created",
        entity_type="doctor",
        entity_id=doctor.id,
        summary="Doctor created from doctors schedule page.",
        metadata={
            "service_ids": sorted(str(item) for item in service_ids),
            "service_categories": categories,
            "working_hour_intervals": len(payload.working_hours.intervals),
        },
        flush=False,
    )
    _commit_or_conflict(db, "Could not create doctor. Check the schedule and try again.")
    db.refresh(doctor)
    return doctor


@router.patch("/doctor-admin/{doctor_id}", response_model=DoctorRead)
def update_doctor_from_admin(
    doctor_id: UUID,
    payload: DoctorAdminUpdate,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
) -> Doctor:
    workspace_id = access.workspace.id
    row = db.execute(
        select(Doctor, Staff)
        .join(
            Staff,
            (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
        )
        .where(
            Doctor.workspace_id == workspace_id,
            Doctor.id == doctor_id,
            Doctor.is_active.is_(True),
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found.")
    doctor, staff = row
    categories = _requested_categories(
        db,
        workspace_id=workspace_id,
        service_categories=payload.service_categories,
        service_ids=(
            payload.service_ids
            if "service_ids" in payload.model_fields_set
            else None
        ),
        current=doctor_service_categories(
            db,
            workspace_id=workspace_id,
            doctor_id=doctor.id,
        ),
    )
    first_name, last_name = _stored_doctor_name(
        name=payload.name,
        first_name=payload.first_name,
        last_name=payload.last_name,
    )
    staff.first_name = first_name
    staff.last_name = last_name
    staff.email = None
    staff.phone = _clean_optional(payload.phone)
    doctor.specialization = _clean_optional(payload.specialization)
    doctor.booking_enabled = payload.booking_enabled

    service_ids = sync_doctor_service_assignments(
        db, workspace_id=workspace_id, doctor=doctor, categories=categories
    )

    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=access.user.id,
        action="clinic.doctor_updated",
        entity_type="doctor",
        entity_id=doctor.id,
        summary="Doctor updated from doctors schedule page.",
        metadata={
            "service_ids": sorted(str(item) for item in service_ids),
            "service_categories": categories,
            "booking_enabled": doctor.booking_enabled,
        },
        flush=False,
    )
    _commit_or_conflict(db, "Could not update doctor.")
    db.refresh(doctor)
    return doctor


@router.put(
    "/doctor-admin/{doctor_id}/working-hours",
    response_model=list[DoctorWorkingHourRead],
)
def update_doctor_hours_from_admin(
    doctor_id: UUID,
    payload: WorkingHoursReplace,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[DoctorWorkingHour]:
    doctor = db.scalar(
        select(Doctor).where(
            Doctor.workspace_id == access.workspace.id,
            Doctor.id == doctor_id,
            Doctor.is_active.is_(True),
        )
    )
    if doctor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found.")
    rows = _replace_doctor_hours(
        db,
        workspace=access.workspace,
        doctor_id=doctor.id,
        payload=payload,
    )
    record_activity_event(
        db,
        workspace_id=access.workspace.id,
        actor_type="staff",
        actor_user_id=access.user.id,
        action="clinic.doctor_working_hours_replaced",
        entity_type="doctor",
        entity_id=doctor.id,
        summary="Doctor working hours replaced from doctors schedule page.",
        metadata={"interval_count": len(rows)},
        flush=False,
    )
    _commit_or_conflict(db, "Doctor working-hour intervals must be unique and valid.")
    return rows


@router.delete("/doctor-admin/{doctor_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_doctor_from_admin(
    doctor_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    workspace_id = access.workspace.id
    doctor = db.scalar(
        select(Doctor).where(
            Doctor.workspace_id == workspace_id,
            Doctor.id == doctor_id,
            Doctor.is_active.is_(True),
        )
    )
    if doctor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found.")

    future_appointment_id = db.scalar(
        select(Appointment.id)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.doctor_id == doctor.id,
            Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            Appointment.start_at >= datetime.now(UTC),
        )
        .limit(1)
    )
    if future_appointment_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Doctor still has upcoming appointments. Reassign or cancel them before removing the doctor."
            ),
        )

    doctor.is_active = False
    doctor.booking_enabled = False
    for assignment in db.scalars(
        select(DoctorBranch).where(
            DoctorBranch.workspace_id == workspace_id,
            DoctorBranch.doctor_id == doctor.id,
        )
    ):
        assignment.is_active = False
    for assignment in db.scalars(
        select(DoctorService).where(
            DoctorService.workspace_id == workspace_id,
            DoctorService.doctor_id == doctor.id,
        )
    ):
        assignment.is_active = False

    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=access.user.id,
        action="clinic.doctor_archived",
        entity_type="doctor",
        entity_id=doctor.id,
        summary="Doctor removed from active scheduling.",
        metadata={"soft_delete": True},
        flush=False,
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
