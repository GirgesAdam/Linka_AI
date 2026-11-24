from __future__ import annotations

from collections import defaultdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies.section_access import require_section_access
from app.api.dependencies.security import WorkspaceAccess
from app.database.session import get_db
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.doctor_service_category import DoctorServiceCategory
from app.models.service import Service
from app.models.staff import Staff
from app.models.working_hours import DoctorWorkingHour

router = APIRouter()


class DoctorAdminNamedLink(BaseModel):
    id: UUID
    name: str


class DoctorAdminHour(BaseModel):
    weekday: int
    start_time: str
    end_time: str


class DoctorAdminBranchSchedule(BaseModel):
    branch_id: UUID
    branch_name: str
    is_primary: bool
    working_hours: list[DoctorAdminHour] = Field(default_factory=list)


class DoctorAdminListItem(BaseModel):
    id: UUID
    staff_id: UUID
    name: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    specialization: str | None
    phone: str | None
    booking_enabled: bool
    is_active: bool
    services: list[DoctorAdminNamedLink]
    service_categories: list[str] = Field(default_factory=list)
    working_hours: list[DoctorAdminHour] = Field(default_factory=list)
    branch_schedules: list[DoctorAdminBranchSchedule] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


@router.get("/doctor-admin", response_model=list[DoctorAdminListItem])
def list_doctors_for_admin(
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[DoctorAdminListItem]:
    workspace_id = access.workspace.id
    rows = list(
        db.execute(
            select(Doctor, Staff)
            .join(
                Staff,
                (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
            )
            .where(
                Doctor.workspace_id == workspace_id,
                Doctor.is_active.is_(True),
            )
            .order_by(Staff.first_name, Staff.last_name)
        )
    )
    doctor_ids = [doctor.id for doctor, _staff in rows]
    if not doctor_ids:
        return []

    service_rows = list(
        db.execute(
            select(DoctorService, Service)
            .join(
                Service,
                (Service.workspace_id == DoctorService.workspace_id)
                & (Service.id == DoctorService.service_id),
            )
            .where(
                DoctorService.workspace_id == workspace_id,
                DoctorService.doctor_id.in_(doctor_ids),
                DoctorService.is_active.is_(True),
                Service.is_active.is_(True),
            )
        )
    )
    category_rows = list(
        db.scalars(
            select(DoctorServiceCategory).where(
                DoctorServiceCategory.workspace_id == workspace_id,
                DoctorServiceCategory.doctor_id.in_(doctor_ids),
            )
        )
    )
    assignments = list(
        db.scalars(
            select(DoctorBranch)
            .join(Branch, (Branch.workspace_id == DoctorBranch.workspace_id) & (Branch.id == DoctorBranch.branch_id))
            .where(
                Branch.is_active.is_(True),
                DoctorBranch.workspace_id == workspace_id,
                DoctorBranch.doctor_id.in_(doctor_ids),
                DoctorBranch.is_active.is_(True),
            )
            .order_by(
                DoctorBranch.doctor_id,
                DoctorBranch.is_primary.desc(),
                DoctorBranch.created_at,
            )
        )
    )
    primary_branch_by_doctor: dict[UUID, UUID] = {}
    for assignment in assignments:
        primary_branch_by_doctor.setdefault(assignment.doctor_id, assignment.branch_id)

    hour_rows: list[DoctorWorkingHour] = []
    if primary_branch_by_doctor:
        candidate_hours = list(
            db.scalars(
                select(DoctorWorkingHour)
                .where(
                    DoctorWorkingHour.workspace_id == workspace_id,
                    DoctorWorkingHour.doctor_id.in_(doctor_ids),
                )
                .order_by(
                    DoctorWorkingHour.doctor_id,
                    DoctorWorkingHour.weekday,
                    DoctorWorkingHour.start_time,
                )
            )
        )
        hour_rows = [
            row
            for row in candidate_hours
            if any(a.doctor_id == row.doctor_id and a.branch_id == row.branch_id for a in assignments)
        ]

    services_by_doctor: dict[UUID, list[DoctorAdminNamedLink]] = defaultdict(list)
    for assignment, service in service_rows:
        services_by_doctor[assignment.doctor_id].append(
            DoctorAdminNamedLink(id=service.id, name=service.name)
        )
    categories_by_doctor: dict[UUID, list[str]] = defaultdict(list)
    for row in category_rows:
        categories_by_doctor[row.doctor_id].append(row.category)

    hours_by_doctor: dict[tuple[UUID, UUID], list[DoctorAdminHour]] = defaultdict(list)
    for hour in hour_rows:
        hours_by_doctor[(hour.doctor_id, hour.branch_id)].append(
            DoctorAdminHour(
                weekday=hour.weekday,
                start_time=hour.start_time.strftime("%H:%M"),
                end_time=hour.end_time.strftime("%H:%M"),
            )
        )

    branch_ids = {a.branch_id for a in assignments}
    branch_names = {b.id: b.name for b in db.scalars(select(Branch).where(
        Branch.workspace_id == workspace_id, Branch.id.in_(branch_ids),
    ))} if branch_ids else {}
    return [
        DoctorAdminListItem(
            id=doctor.id,
            staff_id=doctor.staff_id,
            name=f"{staff.first_name} {staff.last_name}".strip(),
            first_name=staff.first_name,
            last_name=staff.last_name,
            email=None,
            specialization=doctor.specialization,
            phone=staff.phone,
            booking_enabled=doctor.booking_enabled,
            is_active=doctor.is_active,
            services=sorted(services_by_doctor.get(doctor.id, []), key=lambda item: item.name),
            service_categories=sorted(categories_by_doctor.get(doctor.id, [])),
            working_hours=hours_by_doctor.get((doctor.id, primary_branch_by_doctor.get(doctor.id)), []),
            branch_schedules=[DoctorAdminBranchSchedule(
                branch_id=a.branch_id, branch_name=branch_names[a.branch_id],
                is_primary=a.is_primary, working_hours=hours_by_doctor.get((doctor.id, a.branch_id), []),
            ) for a in assignments if a.doctor_id == doctor.id and a.branch_id in branch_names],
        )
        for doctor, staff in rows
    ]
