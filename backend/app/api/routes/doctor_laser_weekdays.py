"""Workspace-scoped doctor/day/device restrictions for clinic operations."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies.section_access import require_section_access
from app.api.dependencies.security import WorkspaceAccess
from app.database.session import get_db
from app.models.branch import Branch
from app.models.clinic_inventory import ClinicLaserDevice
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_laser_weekday_exclusion import DoctorLaserWeekdayExclusion
from app.models.doctor_service_category import DoctorServiceCategory
from app.models.staff import Staff
from app.models.working_hours import DoctorWorkingHour
from app.services.activity import record_activity_event

router = APIRouter()


class DoctorLaserWeekdayRead(BaseModel):
    doctor_id: UUID
    doctor_name: str
    branch_id: UUID
    branch_name: str
    weekday: int
    excluded_device_keys: list[str]


class DoctorLaserWeekdayReplace(BaseModel):
    excluded_device_keys: list[str] = Field(default_factory=list, max_length=100)


def _laser_doctor_assignment(
    db: Session, workspace_id: UUID, doctor_id: UUID, branch_id: UUID
) -> None:
    valid = db.scalar(
        select(DoctorBranch.id)
        .join(
            Doctor,
            (Doctor.workspace_id == DoctorBranch.workspace_id)
            & (Doctor.id == DoctorBranch.doctor_id),
        )
        .join(
            Branch,
            (Branch.workspace_id == DoctorBranch.workspace_id)
            & (Branch.id == DoctorBranch.branch_id),
        )
        .join(
            DoctorServiceCategory,
            (DoctorServiceCategory.workspace_id == Doctor.workspace_id)
            & (DoctorServiceCategory.doctor_id == Doctor.id),
        )
        .where(
            DoctorBranch.workspace_id == workspace_id,
            DoctorBranch.doctor_id == doctor_id,
            DoctorBranch.branch_id == branch_id,
            DoctorBranch.is_active.is_(True),
            Doctor.is_active.is_(True),
            Branch.is_active.is_(True),
            DoctorServiceCategory.category == "laser",
        )
    )
    if valid is None:
        raise HTTPException(status_code=404, detail="Active laser doctor/branch assignment not found.")


@router.get("/doctor-laser-weekdays", response_model=list[DoctorLaserWeekdayRead])
def list_doctor_laser_weekdays(
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[DoctorLaserWeekdayRead]:
    wid = access.workspace.id
    # Device choices exist only for actual recurring weekly doctor hours.
    assignments = db.execute(
        select(Doctor, Branch, Staff)
        .join(
            DoctorBranch,
            (DoctorBranch.workspace_id == Doctor.workspace_id)
            & (DoctorBranch.doctor_id == Doctor.id),
        )
        .join(
            Branch,
            (Branch.workspace_id == DoctorBranch.workspace_id)
            & (Branch.id == DoctorBranch.branch_id),
        )
        .join(
            Staff,
            (Staff.workspace_id == Doctor.workspace_id)
            & (Staff.id == Doctor.staff_id),
        )
        .join(
            DoctorServiceCategory,
            (DoctorServiceCategory.workspace_id == Doctor.workspace_id)
            & (DoctorServiceCategory.doctor_id == Doctor.id),
        )
        .where(
            Doctor.workspace_id == wid,
            Doctor.is_active.is_(True),
            Branch.is_active.is_(True),
            DoctorBranch.is_active.is_(True),
            DoctorServiceCategory.category == "laser",
        )
        .order_by(Staff.first_name, Doctor.id, Branch.id)
    ).all()
    hours = db.scalars(select(DoctorWorkingHour).where(
        DoctorWorkingHour.workspace_id == wid
    )).all()
    schedule: dict[tuple[UUID, UUID], set[int]] = {}
    for hour in hours:
        schedule.setdefault((hour.doctor_id, hour.branch_id), set()).add(hour.weekday)
    exclusions = db.scalars(
        select(DoctorLaserWeekdayExclusion).where(
            DoctorLaserWeekdayExclusion.workspace_id == wid
        )
    ).all()
    by_key: dict[tuple[UUID, UUID, int], list[str]] = {}
    for exclusion in exclusions:
        by_key.setdefault(
            (exclusion.doctor_id, exclusion.branch_id, exclusion.weekday), []
        ).append(exclusion.device_key)
    result = {}
    for doctor, branch, staff in assignments:
        for weekday in sorted(schedule.get((doctor.id, branch.id), set())):
            key = (doctor.id, branch.id, weekday)
            if key not in result:
                result[key] = DoctorLaserWeekdayRead(
                    doctor_id=doctor.id,
                    doctor_name=f"{staff.first_name} {staff.last_name}".strip(),
                    branch_id=branch.id,
                    branch_name=branch.name,
                    weekday=weekday,
                    excluded_device_keys=sorted(by_key.get(key, [])),
                )
    return list(result.values())


@router.put(
    "/doctor-laser-weekdays/{doctor_id}/{branch_id}/{weekday}",
    response_model=DoctorLaserWeekdayRead,
)
def replace_doctor_laser_weekday(
    doctor_id: UUID,
    branch_id: UUID,
    weekday: int,
    payload: DoctorLaserWeekdayReplace,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
) -> DoctorLaserWeekdayRead:
    wid = access.workspace.id
    if weekday not in range(7):
        raise HTTPException(status_code=422, detail="Weekday must be between 0 and 6.")
    _laser_doctor_assignment(db, wid, doctor_id, branch_id)
    hours = db.scalar(
        select(DoctorWorkingHour.id).where(
            DoctorWorkingHour.workspace_id == wid,
            DoctorWorkingHour.doctor_id == doctor_id,
            DoctorWorkingHour.branch_id == branch_id,
            DoctorWorkingHour.weekday == weekday,
        )
    )
    if hours is None:
        raise HTTPException(status_code=409, detail="Doctor has no weekly hours on this weekday.")
    requested = set(payload.excluded_device_keys)
    active_devices = set(db.scalars(
        select(ClinicLaserDevice.device_key).where(
            ClinicLaserDevice.workspace_id == wid,
            ClinicLaserDevice.is_active.is_(True),
        )
    ).all())
    if len(requested) != len(payload.excluded_device_keys) or not requested.issubset(active_devices):
        raise HTTPException(status_code=422, detail="Invalid, inactive or duplicate laser device.")
    current = db.scalars(
        select(DoctorLaserWeekdayExclusion).where(
            DoctorLaserWeekdayExclusion.workspace_id == wid,
            DoctorLaserWeekdayExclusion.doctor_id == doctor_id,
            DoctorLaserWeekdayExclusion.branch_id == branch_id,
            DoctorLaserWeekdayExclusion.weekday == weekday,
        ).with_for_update()
    ).all()
    for row in current:
        if row.device_key not in requested:
            db.delete(row)
    existing = {row.device_key for row in current}
    for device_key in sorted(requested - existing):
        db.add(DoctorLaserWeekdayExclusion(
            workspace_id=wid, doctor_id=doctor_id, branch_id=branch_id,
            weekday=weekday, device_key=device_key,
        ))
    record_activity_event(
        db,
        workspace_id=wid, actor_type="staff", actor_user_id=access.user.id,
        action="clinic.doctor_laser_weekday_updated",
        entity_type="doctor", entity_id=doctor_id,
        summary="Doctor laser weekday device exclusions updated.",
        metadata={"branch_id": str(branch_id), "weekday": weekday, "excluded_device_keys": sorted(requested)},
        flush=False,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Could not save weekday device restrictions.") from exc
    branch = db.get(Branch, branch_id)
    doctor = db.get(Doctor, doctor_id)
    staff = db.get(Staff, doctor.staff_id)
    return DoctorLaserWeekdayRead(
        doctor_id=doctor_id, doctor_name=f"{staff.first_name} {staff.last_name}".strip(),
        branch_id=branch_id, branch_name=branch.name,
        weekday=weekday, excluded_device_keys=sorted(requested),
    )
