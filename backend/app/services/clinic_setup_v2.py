from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.clinic_inventory import ServiceDevicePrice
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.service import Service
from app.models.staff import Staff
from app.models.working_hours import BranchWorkingHour, DoctorAvailabilityWindow, DoctorWorkingHour
from app.models.workspace import Workspace
from app.schemas.clinic import normalize_service_category
from app.schemas.clinic_setup_v2 import (
    BookingPolicyUpdateV2,
    ClinicDoctorCreateV2,
    ClinicDoctorReadV2,
    ClinicDoctorUpdateV2,
    ClinicProfileReadV2,
    ClinicProfileUpsert,
    ClinicServiceCreateV2,
    ClinicServiceReadV2,
    ClinicServiceUpdateV2,
    ClinicSetupReadinessV2,
    ClinicSetupV2Snapshot,
    VisitingWindowsUpdateV2,
    WorkingHourInputV2,
    WorkingHoursUpdateV2,
)


class ClinicSetupV2Error(ValueError):
    pass


def _price_minor(price: Decimal) -> int:
    return int((price * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _price_egp(price_minor: int) -> Decimal:
    return (Decimal(price_minor) / Decimal("100")).quantize(Decimal("0.01"))


def _service_slug(workspace_id: UUID, name: str) -> str:
    digest = hashlib.sha256(f"{workspace_id}:{name.casefold()}".encode()).hexdigest()[:12]
    return f"svc-{digest}"


def _branch_code(workspace_id: UUID) -> str:
    return f"main-{str(workspace_id).replace('-', '')[:10]}"


def _full_name(staff: Staff) -> str:
    return f"{staff.first_name} {staff.last_name}".strip()


def _split_name(full_name: str) -> tuple[str, str]:
    parts = [part for part in full_name.split() if part]
    if not parts:
        raise ClinicSetupV2Error("Doctor name is required.")
    return parts[0], " ".join(parts[1:])


def _primary_branch(db: Session, workspace: Workspace) -> Branch | None:
    if workspace.primary_branch_id is not None:
        branch = db.scalar(
            select(Branch).where(
                Branch.workspace_id == workspace.id,
                Branch.id == workspace.primary_branch_id,
            )
        )
        if branch is not None:
            return branch
    return db.scalar(
        select(Branch)
        .where(Branch.workspace_id == workspace.id, Branch.is_active.is_(True))
        .order_by(Branch.created_at.asc())
        .limit(1)
    )


def upsert_clinic_profile(
    db: Session,
    *,
    workspace: Workspace,
    payload: ClinicProfileUpsert,
) -> Branch:
    branch = _primary_branch(db, workspace)
    if branch is None:
        branch = Branch(
            workspace_id=workspace.id,
            name=payload.name,
            code=_branch_code(workspace.id),
            phone=payload.phone,
            address_line1=payload.address,
            city=payload.city,
            country_code="EG",
            timezone=workspace.timezone,
            is_active=True,
        )
        db.add(branch)
        db.flush()
    else:
        branch.name = payload.name
        branch.phone = payload.phone
        branch.address_line1 = payload.address
        branch.city = payload.city
        branch.country_code = "EG"
        branch.timezone = workspace.timezone
        branch.is_active = True

    workspace.primary_branch_id = branch.id

    settings = db.scalar(
        select(BookingSettings).where(BookingSettings.workspace_id == workspace.id)
    )
    if settings is None:
        db.add(
            BookingSettings(
                workspace_id=workspace.id,
                slot_interval_minutes=15,
                minimum_notice_minutes=60,
                booking_horizon_days=90,
                cancellation_notice_minutes=720,
                allow_same_day_booking=True,
                require_confirmation=True,
                default_currency="EGP",
            )
        )
    else:
        settings.default_currency = "EGP"
    db.flush()
    return branch


def create_service_v2(
    db: Session,
    *,
    workspace: Workspace,
    payload: ClinicServiceCreateV2,
) -> Service:
    name_key = payload.name.strip().casefold()
    existing = list(
        db.scalars(
            select(Service).where(
                Service.workspace_id == workspace.id,
                Service.is_active.is_(True),
            )
        )
    )
    if any(row.name.strip().casefold() == name_key for row in existing):
        raise ClinicSetupV2Error("A service with this name already exists.")
    service = Service(
        workspace_id=workspace.id,
        name=payload.name.strip(),
        slug=_service_slug(workspace.id, payload.name),
        category=payload.category,
        operational_category=normalize_service_category(payload.category),
        description=None,
        duration_minutes=payload.duration_minutes,
        buffer_before_minutes=0,
        buffer_after_minutes=0,
        price_minor=_price_minor(payload.price),
        currency="EGP",
        requires_medical_review=False,
        requires_laser_device=normalize_service_category(payload.category) == "laser",
        is_active=True,
    )
    db.add(service)
    db.flush()
    return service


def update_service_v2(
    db: Session,
    *,
    workspace: Workspace,
    service_id: UUID,
    payload: ClinicServiceUpdateV2,
) -> Service:
    service = db.scalar(select(Service).where(Service.workspace_id == workspace.id, Service.id == service_id))
    if service is None:
        raise ClinicSetupV2Error("Service not found.")
    duplicate_rows = list(
        db.scalars(
            select(Service).where(
                Service.workspace_id == workspace.id,
                Service.id != service.id,
                Service.is_active.is_(True),
            )
        )
    )
    if any(row.name.strip().casefold() == payload.name.strip().casefold() for row in duplicate_rows):
        raise ClinicSetupV2Error("A service with this name already exists.")
    service.name = payload.name.strip()
    service.category = payload.category
    service.operational_category = normalize_service_category(payload.category)
    service.requires_laser_device = service.operational_category == "laser"
    service.duration_minutes = payload.duration_minutes
    service.price_minor = _price_minor(payload.price)
    service.currency = "EGP"
    if service.operational_category != "laser":
        db.execute(
            update(ServiceDevicePrice)
            .where(
                ServiceDevicePrice.workspace_id == workspace.id,
                ServiceDevicePrice.service_id == service.id,
                ServiceDevicePrice.is_active.is_(True),
            )
            .values(is_active=False)
        )
    db.flush()
    return service


def _validate_services(db: Session, *, workspace_id: UUID, service_ids: list[UUID]) -> list[Service]:
    if not service_ids:
        return []
    rows = list(
        db.scalars(
            select(Service).where(
                Service.workspace_id == workspace_id,
                Service.id.in_(service_ids),
                Service.is_active.is_(True),
            )
        )
    )
    if {row.id for row in rows} != set(service_ids):
        raise ClinicSetupV2Error("One or more selected services do not exist.")
    return rows


def create_doctor_v2(
    db: Session,
    *,
    workspace: Workspace,
    payload: ClinicDoctorCreateV2,
) -> Doctor:
    branch = _primary_branch(db, workspace)
    if branch is None:
        raise ClinicSetupV2Error("Save the clinic profile before adding doctors.")
    _validate_services(db, workspace_id=workspace.id, service_ids=payload.service_ids)
    requested_name = " ".join(payload.full_name.split()).casefold()
    existing_rows = list(
        db.execute(
            select(Doctor, Staff)
            .join(Staff, (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id))
            .where(Doctor.workspace_id == workspace.id, Doctor.is_active.is_(True), Staff.is_active.is_(True))
        ).all()
    )
    same_name = [
        (doctor, staff)
        for doctor, staff in existing_rows
        if _full_name(staff).casefold() == requested_name
    ]
    if same_name:
        doctor, staff = same_name[0]
        if doctor.booking_enabled:
            raise ClinicSetupV2Error("A doctor with this name already exists.")
        first_name, last_name = _split_name(payload.full_name)
        staff.first_name = first_name
        staff.last_name = last_name
        doctor.doctor_type = payload.doctor_type
        doctor.specialization = payload.specialization
        doctor.booking_enabled = True
        assignment = db.scalar(
            select(DoctorBranch).where(
                DoctorBranch.workspace_id == workspace.id,
                DoctorBranch.doctor_id == doctor.id,
                DoctorBranch.branch_id == branch.id,
            )
        )
        if assignment is None:
            db.add(DoctorBranch(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                branch_id=branch.id,
                is_primary=True,
                is_active=True,
            ))
        else:
            assignment.is_active = True
            assignment.is_primary = True
        replace_doctor_services_v2(db, workspace=workspace, doctor_id=doctor.id, service_ids=payload.service_ids)
        db.flush()
        return doctor

    first_name, last_name = _split_name(payload.full_name)
    staff = Staff(
        workspace_id=workspace.id,
        user_id=None,
        first_name=first_name,
        last_name=last_name,
        email=None,
        phone=None,
        job_title="doctor",
        is_active=True,
    )
    db.add(staff)
    db.flush()
    doctor = Doctor(
        workspace_id=workspace.id,
        staff_id=staff.id,
        doctor_type=payload.doctor_type,
        specialization=payload.specialization,
        license_number=None,
        bio=None,
        booking_enabled=True,
        is_active=True,
    )
    db.add(doctor)
    db.flush()
    db.add(
        DoctorBranch(
            workspace_id=workspace.id,
            doctor_id=doctor.id,
            branch_id=branch.id,
            is_primary=True,
            is_active=True,
        )
    )
    for service_id in payload.service_ids:
        db.add(
            DoctorService(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                service_id=service_id,
                custom_duration_minutes=None,
                custom_price_minor=None,
                is_active=True,
            )
        )
    db.flush()
    return doctor


def update_doctor_v2(
    db: Session,
    *,
    workspace: Workspace,
    doctor_id: UUID,
    payload: ClinicDoctorUpdateV2,
) -> Doctor:
    row = db.execute(
        select(Doctor, Staff)
        .join(Staff, (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id))
        .where(Doctor.workspace_id == workspace.id, Doctor.id == doctor_id)
    ).first()
    if row is None:
        raise ClinicSetupV2Error("Doctor not found.")
    doctor, staff = row
    requested_name = " ".join(payload.full_name.split()).casefold()
    other_rows = list(
        db.execute(
            select(Doctor, Staff)
            .join(Staff, (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id))
            .where(
                Doctor.workspace_id == workspace.id,
                Doctor.id != doctor.id,
                Doctor.is_active.is_(True),
                Doctor.booking_enabled.is_(True),
                Staff.is_active.is_(True),
            )
        ).all()
    )
    if any(_full_name(other_staff).casefold() == requested_name for _other, other_staff in other_rows):
        raise ClinicSetupV2Error("A doctor with this name already exists.")
    first_name, last_name = _split_name(payload.full_name)
    staff.first_name = first_name
    staff.last_name = last_name
    doctor.specialization = payload.specialization
    previous_type = doctor.doctor_type
    doctor.doctor_type = payload.doctor_type
    doctor.booking_enabled = True
    if previous_type != payload.doctor_type:
        if payload.doctor_type == "visiting":
            db.execute(delete(DoctorWorkingHour).where(DoctorWorkingHour.workspace_id == workspace.id, DoctorWorkingHour.doctor_id == doctor.id))
        else:
            db.execute(delete(DoctorAvailabilityWindow).where(DoctorAvailabilityWindow.workspace_id == workspace.id, DoctorAvailabilityWindow.doctor_id == doctor.id))
    replace_doctor_services_v2(db, workspace=workspace, doctor_id=doctor.id, service_ids=payload.service_ids)
    db.flush()
    return doctor


def replace_doctor_services_v2(
    db: Session,
    *,
    workspace: Workspace,
    doctor_id: UUID,
    service_ids: list[UUID],
) -> None:
    doctor = db.scalar(
        select(Doctor).where(Doctor.workspace_id == workspace.id, Doctor.id == doctor_id)
    )
    if doctor is None:
        raise ClinicSetupV2Error("Doctor not found.")
    _validate_services(db, workspace_id=workspace.id, service_ids=service_ids)
    db.execute(
        delete(DoctorService).where(
            DoctorService.workspace_id == workspace.id,
            DoctorService.doctor_id == doctor_id,
        )
    )
    for service_id in service_ids:
        db.add(
            DoctorService(
                workspace_id=workspace.id,
                doctor_id=doctor_id,
                service_id=service_id,
                custom_duration_minutes=None,
                custom_price_minor=None,
                is_active=True,
            )
        )
    db.flush()


def update_doctor_type_v2(
    db: Session,
    *,
    workspace: Workspace,
    doctor_id: UUID,
    doctor_type: str,
) -> Doctor:
    doctor = db.scalar(
        select(Doctor).where(Doctor.workspace_id == workspace.id, Doctor.id == doctor_id)
    )
    if doctor is None:
        raise ClinicSetupV2Error("Doctor not found.")
    doctor.doctor_type = doctor_type
    doctor.booking_enabled = True
    if doctor_type == "visiting":
        db.execute(
            delete(DoctorWorkingHour).where(
                DoctorWorkingHour.workspace_id == workspace.id,
                DoctorWorkingHour.doctor_id == doctor.id,
            )
        )
    db.flush()
    return doctor


def replace_clinic_hours_v2(
    db: Session,
    *,
    workspace: Workspace,
    payload: WorkingHoursUpdateV2,
) -> None:
    branch = _primary_branch(db, workspace)
    if branch is None:
        raise ClinicSetupV2Error("Save the clinic profile first.")
    db.execute(
        delete(BranchWorkingHour).where(
            BranchWorkingHour.workspace_id == workspace.id,
            BranchWorkingHour.branch_id == branch.id,
        )
    )
    for interval in payload.intervals:
        db.add(
            BranchWorkingHour(
                workspace_id=workspace.id,
                branch_id=branch.id,
                weekday=interval.weekday,
                start_time=interval.start_time,
                end_time=interval.end_time,
            )
        )
    db.flush()


def replace_regular_doctor_hours_v2(
    db: Session,
    *,
    workspace: Workspace,
    doctor_id: UUID,
    payload: WorkingHoursUpdateV2,
) -> None:
    branch = _primary_branch(db, workspace)
    doctor = db.scalar(
        select(Doctor).where(Doctor.workspace_id == workspace.id, Doctor.id == doctor_id)
    )
    if branch is None or doctor is None:
        raise ClinicSetupV2Error("Doctor or clinic profile not found.")
    if doctor.doctor_type != "regular":
        raise ClinicSetupV2Error("Visiting doctors use dated availability windows, not weekly hours.")
    db.execute(
        delete(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == workspace.id,
            DoctorWorkingHour.doctor_id == doctor.id,
            DoctorWorkingHour.branch_id == branch.id,
        )
    )
    for interval in payload.intervals:
        db.add(
            DoctorWorkingHour(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                branch_id=branch.id,
                weekday=interval.weekday,
                start_time=interval.start_time,
                end_time=interval.end_time,
            )
        )
    db.flush()


def replace_visiting_windows_v2(
    db: Session,
    *,
    workspace: Workspace,
    doctor_id: UUID,
    payload: VisitingWindowsUpdateV2,
) -> None:
    branch = _primary_branch(db, workspace)
    doctor = db.scalar(
        select(Doctor).where(Doctor.workspace_id == workspace.id, Doctor.id == doctor_id)
    )
    if branch is None or doctor is None:
        raise ClinicSetupV2Error("Doctor or clinic profile not found.")
    if doctor.doctor_type != "visiting":
        raise ClinicSetupV2Error("Regular doctors use weekly working hours.")
    db.execute(
        delete(DoctorAvailabilityWindow).where(
            DoctorAvailabilityWindow.workspace_id == workspace.id,
            DoctorAvailabilityWindow.doctor_id == doctor.id,
            DoctorAvailabilityWindow.branch_id == branch.id,
        )
    )
    tz = ZoneInfo(workspace.timezone)
    now_utc = datetime.now(UTC)
    for item in payload.windows:
        start_at = datetime.combine(item.date, item.start_time, tzinfo=tz).astimezone(UTC)
        end_at = datetime.combine(item.date, item.end_time, tzinfo=tz).astimezone(UTC)
        if end_at <= now_utc:
            raise ClinicSetupV2Error("Visiting doctor availability must end in the future.")
        db.add(
            DoctorAvailabilityWindow(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                branch_id=branch.id,
                start_at=start_at,
                end_at=end_at,
            )
        )
    db.flush()


def update_booking_policy_v2(
    db: Session,
    *,
    workspace: Workspace,
    payload: BookingPolicyUpdateV2,
) -> None:
    settings = db.scalar(select(BookingSettings).where(BookingSettings.workspace_id == workspace.id))
    if settings is None:
        settings = BookingSettings(workspace_id=workspace.id, default_currency="EGP")
        db.add(settings)
    settings.slot_interval_minutes = payload.slot_interval_minutes
    settings.minimum_notice_minutes = payload.minimum_notice_minutes
    settings.booking_horizon_days = payload.booking_horizon_days
    settings.cancellation_notice_minutes = payload.cancellation_notice_minutes
    settings.allow_same_day_booking = payload.allow_same_day_booking
    settings.require_confirmation = payload.require_confirmation
    settings.default_currency = "EGP"
    db.flush()


def build_setup_v2_snapshot(db: Session, *, workspace: Workspace) -> ClinicSetupV2Snapshot:
    branch = _primary_branch(db, workspace)
    services = list(
        db.scalars(
            select(Service)
            .where(Service.workspace_id == workspace.id, Service.is_active.is_(True))
            .order_by(Service.name)
        )
    )
    doctor_rows = list(
        db.execute(
            select(Doctor, Staff)
            .join(
                Staff,
                (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
            )
            .where(
                Doctor.workspace_id == workspace.id,
                Doctor.is_active.is_(True),
                Doctor.booking_enabled.is_(True),
                Staff.is_active.is_(True),
            )
            .order_by(Staff.first_name, Staff.last_name)
        ).all()
    )
    assignments = list(
        db.scalars(
            select(DoctorService).where(
                DoctorService.workspace_id == workspace.id,
                DoctorService.is_active.is_(True),
            )
        )
    )
    service_ids_by_doctor: dict[UUID, list[UUID]] = {}
    for row in assignments:
        service_ids_by_doctor.setdefault(row.doctor_id, []).append(row.service_id)

    weekly_rows = list(
        db.scalars(
            select(DoctorWorkingHour).where(DoctorWorkingHour.workspace_id == workspace.id)
        )
    )
    weekly_by_doctor: dict[UUID, list[WorkingHourInputV2]] = {}
    for row in weekly_rows:
        weekly_by_doctor.setdefault(row.doctor_id, []).append(
            WorkingHourInputV2(
                weekday=row.weekday,
                start_time=row.start_time,
                end_time=row.end_time,
            )
        )

    windows = list(
        db.scalars(
            select(DoctorAvailabilityWindow)
            .where(DoctorAvailabilityWindow.workspace_id == workspace.id)
            .order_by(DoctorAvailabilityWindow.start_at)
        )
    )
    windows_by_doctor: dict[UUID, list[dict]] = {}
    for row in windows:
        windows_by_doctor.setdefault(row.doctor_id, []).append(
            {"start_at": row.start_at, "end_at": row.end_at}
        )

    clinic_hours: list[WorkingHourInputV2] = []
    if branch is not None:
        clinic_hours = [
            WorkingHourInputV2(
                weekday=row.weekday,
                start_time=row.start_time,
                end_time=row.end_time,
            )
            for row in db.scalars(
                select(BranchWorkingHour)
                .where(
                    BranchWorkingHour.workspace_id == workspace.id,
                    BranchWorkingHour.branch_id == branch.id,
                )
                .order_by(BranchWorkingHour.weekday, BranchWorkingHour.start_time)
            )
        ]

    now_utc = datetime.now(UTC)
    bookable_doctor_ids: set[UUID] = set()
    for doctor, _staff in doctor_rows:
        if not service_ids_by_doctor.get(doctor.id):
            continue
        if doctor.doctor_type == "regular" and weekly_by_doctor.get(doctor.id):
            bookable_doctor_ids.add(doctor.id)
            continue
        if doctor.doctor_type == "visiting" and any(
            item.get("end_at") is not None and item["end_at"] >= now_utc
            for item in windows_by_doctor.get(doctor.id, [])
        ):
            bookable_doctor_ids.add(doctor.id)

    checks = {
        "clinic_profile": branch is not None,
        "services": bool(services),
        "clinic_hours": bool(clinic_hours),
        "bookable_doctor": bool(bookable_doctor_ids),
    }
    labels = {
        "clinic_profile": "أكمل بيانات العيادة",
        "services": "أضف خدمة واحدة على الأقل",
        "clinic_hours": "حدد مواعيد عمل العيادة",
        "bookable_doctor": "أضف دكتورًا له خدمة ومواعيد متاحة",
    }
    completed = sum(1 for ok in checks.values() if ok)

    settings = db.scalar(
        select(BookingSettings).where(BookingSettings.workspace_id == workspace.id)
    )
    policy = {
        "slot_interval_minutes": settings.slot_interval_minutes if settings else 15,
        "minimum_notice_minutes": settings.minimum_notice_minutes if settings else 60,
        "booking_horizon_days": settings.booking_horizon_days if settings else 90,
        "cancellation_notice_minutes": settings.cancellation_notice_minutes if settings else 720,
        "allow_same_day_booking": settings.allow_same_day_booking if settings else True,
        "require_confirmation": settings.require_confirmation if settings else True,
    }

    return ClinicSetupV2Snapshot(
        workspace_id=workspace.id,
        clinic=ClinicProfileReadV2(
            branch_id=branch.id if branch else None,
            name=branch.name if branch else workspace.name,
            phone=branch.phone if branch else None,
            address=branch.address_line1 if branch else None,
            city=branch.city if branch else None,
            timezone=workspace.timezone,
        ),
        services=[
            ClinicServiceReadV2(
                id=row.id,
                name=row.name,
                category=row.category,
                duration_minutes=row.duration_minutes,
                price=_price_egp(row.price_minor),
            )
            for row in services
        ],
        doctors=[
            ClinicDoctorReadV2(
                id=doctor.id,
                staff_id=doctor.staff_id,
                full_name=_full_name(staff),
                doctor_type=doctor.doctor_type,
                specialization=doctor.specialization,
                service_ids=sorted(service_ids_by_doctor.get(doctor.id, []), key=str),
                weekly_hours=weekly_by_doctor.get(doctor.id, []),
                visiting_windows=windows_by_doctor.get(doctor.id, []),
            )
            for doctor, staff in doctor_rows
        ],
        clinic_hours=clinic_hours,
        booking_policy=policy,
        readiness=ClinicSetupReadinessV2(
            ready=all(checks.values()),
            checks=checks,
            missing=[labels[key] for key, ok in checks.items() if not ok],
            progress_percent=round(completed / len(checks) * 100),
        ),
    )
