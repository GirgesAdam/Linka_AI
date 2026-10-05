from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import ACTIVE_APPOINTMENT_STATUSES, Appointment
from app.models.availability_block import AvailabilityBlock
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.service import Service
from app.models.working_hours import (
    BranchWorkingHour,
    DoctorAvailabilityWindow,
    DoctorWorkingHour,
)
from app.models.workspace import Workspace
from app.services.inventory import InventoryOperationError, configured_device_price


class BookingRuleError(ValueError):
    pass


class BookingCompatibilityError(BookingRuleError):
    """Canonical booking rejection caused by an incompatible grounded selection."""

    def __init__(self, message: str, *, dimension: str) -> None:
        super().__init__(message)
        self.dimension = dimension


@dataclass(frozen=True)
class EffectiveBookingSettings:
    slot_interval_minutes: int = 15
    minimum_notice_minutes: int = 60
    booking_horizon_days: int = 90
    cancellation_notice_minutes: int = 720
    allow_same_day_booking: bool = True
    require_confirmation: bool = True
    default_currency: str = "EGP"


@dataclass(frozen=True)
class SlotCandidate:
    branch_id: UUID
    doctor_id: UUID
    service_id: UUID
    start_at: datetime
    end_at: datetime
    busy_start_at: datetime
    busy_end_at: datetime
    duration_minutes: int
    price_minor: int
    currency: str
    laser_device_key: str | None = None
    laser_device_name: str | None = None


def get_effective_booking_settings(db: Session, workspace_id: UUID) -> EffectiveBookingSettings:
    row = db.scalar(select(BookingSettings).where(BookingSettings.workspace_id == workspace_id))
    if row is None:
        return EffectiveBookingSettings()
    return EffectiveBookingSettings(
        slot_interval_minutes=row.slot_interval_minutes,
        minimum_notice_minutes=row.minimum_notice_minutes,
        booking_horizon_days=row.booking_horizon_days,
        cancellation_notice_minutes=row.cancellation_notice_minutes,
        allow_same_day_booking=row.allow_same_day_booking,
        require_confirmation=row.require_confirmation,
        default_currency=row.default_currency,
    )


def resolve_timezone(workspace: Workspace, branch: Branch) -> ZoneInfo:
    timezone_name = branch.timezone or workspace.timezone
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise BookingRuleError(f"Invalid clinic timezone: {timezone_name}") from exc


def ceil_to_interval(value: datetime, interval_minutes: int) -> datetime:
    midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_minutes = int((value - midnight).total_seconds() // 60)
    remainder = elapsed_minutes % interval_minutes
    if remainder == 0 and value.second == 0 and value.microsecond == 0:
        return value
    add_minutes = interval_minutes - remainder
    rounded = value + timedelta(minutes=add_minutes)
    return rounded.replace(second=0, microsecond=0)


def _combine(local_date: date, local_time: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(local_date, local_time, tzinfo=tz)


def _interval_intersections(
    branch_hours: list[BranchWorkingHour],
    doctor_hours: list[DoctorWorkingHour],
    local_date: date,
    tz: ZoneInfo,
) -> list[tuple[datetime, datetime]]:
    intersections: list[tuple[datetime, datetime]] = []
    for branch_hour in branch_hours:
        branch_start = _combine(local_date, branch_hour.start_time, tz)
        branch_end = _combine(local_date, branch_hour.end_time, tz)
        for doctor_hour in doctor_hours:
            doctor_start = _combine(local_date, doctor_hour.start_time, tz)
            doctor_end = _combine(local_date, doctor_hour.end_time, tz)
            start = max(branch_start, doctor_start)
            end = min(branch_end, doctor_end)
            if end > start:
                intersections.append((start, end))
    return intersections


def _window_intersections(
    branch_hours: list[BranchWorkingHour],
    windows: list[DoctorAvailabilityWindow],
    local_date: date,
    tz: ZoneInfo,
) -> list[tuple[datetime, datetime]]:
    intersections: list[tuple[datetime, datetime]] = []
    for branch_hour in branch_hours:
        branch_start = _combine(local_date, branch_hour.start_time, tz)
        branch_end = _combine(local_date, branch_hour.end_time, tz)
        for window in windows:
            doctor_start = window.start_at.astimezone(tz)
            doctor_end = window.end_at.astimezone(tz)
            start = max(branch_start, doctor_start)
            end = min(branch_end, doctor_end)
            if end > start:
                intersections.append((start, end))
    return intersections


def service_duration_minutes(service: Service) -> int:
    return int(service.duration_minutes)


def _overlaps_existing(
    busy_start_at: datetime,
    busy_end_at: datetime,
    appointments: list[Appointment],
) -> bool:
    return any(
        appointment.busy_start_at < busy_end_at and appointment.busy_end_at > busy_start_at
        for appointment in appointments
    )


def _candidate_start_times(
    *,
    interval_start: datetime,
    interval_end: datetime,
    interval_minutes: int,
    release_times: Collection[datetime],
) -> list[datetime]:
    """Keep the grid, plus exact resource-release times for back-to-back visits."""
    candidates: set[datetime] = {interval_start}
    candidate = ceil_to_interval(interval_start, interval_minutes)
    while candidate <= interval_end:
        candidates.add(candidate)
        candidate += timedelta(minutes=interval_minutes)
    timezone = interval_start.tzinfo
    for release_at in release_times:
        local_release = release_at.astimezone(timezone) if timezone is not None else release_at
        if interval_start <= local_release <= interval_end:
            candidates.add(local_release)
    return sorted(candidates)


def calculate_availability(
    db: Session,
    workspace: Workspace,
    branch_id: UUID,
    service_id: UUID,
    booking_date: date,
    doctor_id: UUID | None = None,
    exclude_appointment_id: UUID | None = None,
    now: datetime | None = None,
    preloaded_branch: Branch | None = None,
    preloaded_service: Service | None = None,
    laser_device_key: str | None = None,
    exclude_appointment_ids: Collection[UUID] = (),
    minimum_notice_minutes_override: int | None = None,
) -> tuple[str, list[SlotCandidate]]:
    if preloaded_branch is not None:
        if (
            preloaded_branch.id != branch_id
            or preloaded_branch.workspace_id != workspace.id
            or not preloaded_branch.is_active
        ):
            raise BookingRuleError("Branch not found or inactive.")
        branch = preloaded_branch
    else:
        branch = db.scalar(
            select(Branch).where(
                Branch.id == branch_id,
                Branch.workspace_id == workspace.id,
                Branch.is_active.is_(True),
            )
        )
        if branch is None:
            raise BookingRuleError("Branch not found or inactive.")

    if preloaded_service is not None:
        if (
            preloaded_service.id != service_id
            or preloaded_service.workspace_id != workspace.id
            or not preloaded_service.is_active
        ):
            raise BookingRuleError("Service not found or inactive.")
        service = preloaded_service
    else:
        service = db.scalar(
            select(Service).where(
                Service.id == service_id,
                Service.workspace_id == workspace.id,
                Service.is_active.is_(True),
            )
        )
        if service is None:
            raise BookingRuleError("Service not found or inactive.")

    requires_device = bool(getattr(service, "requires_laser_device", False))
    if requires_device and not laser_device_key:
        raise BookingRuleError("Laser device choice is required for this service.")
    if not requires_device and laser_device_key:
        raise BookingRuleError("A laser device cannot be selected for this service.")

    device_price = None
    if requires_device:
        try:
            device_price = configured_device_price(
                db,
                workspace_id=workspace.id,
                service_id=service.id,
                device_key=laser_device_key,
            )
        except InventoryOperationError as exc:
            raise BookingRuleError(str(exc)) from exc

    tz = resolve_timezone(workspace, branch)
    settings = get_effective_booking_settings(db, workspace.id)
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    local_today = now_utc.astimezone(tz).date()

    if booking_date < local_today:
        raise BookingRuleError("Cannot book a date in the past.")
    if booking_date > local_today + timedelta(days=settings.booking_horizon_days):
        raise BookingRuleError("Requested date is outside the booking horizon.")
    if booking_date == local_today and not settings.allow_same_day_booking:
        return tz.key, []

    weekday = booking_date.weekday()
    branch_hours = list(
        db.scalars(
            select(BranchWorkingHour).where(
                BranchWorkingHour.workspace_id == workspace.id,
                BranchWorkingHour.branch_id == branch.id,
                BranchWorkingHour.weekday == weekday,
            )
        )
    )
    if not branch_hours:
        return tz.key, []

    assignment_stmt = (
        select(DoctorBranch, DoctorService, Doctor)
        .join(
            DoctorService,
            (DoctorService.workspace_id == DoctorBranch.workspace_id)
            & (DoctorService.doctor_id == DoctorBranch.doctor_id),
        )
        .join(
            Doctor,
            (Doctor.workspace_id == DoctorBranch.workspace_id)
            & (Doctor.id == DoctorBranch.doctor_id),
        )
        .where(
            DoctorBranch.workspace_id == workspace.id,
            DoctorBranch.branch_id == branch.id,
            DoctorBranch.is_active.is_(True),
            DoctorService.service_id == service.id,
            DoctorService.is_active.is_(True),
            Doctor.is_active.is_(True),
            Doctor.booking_enabled.is_(True),
        )
    )
    if doctor_id is not None:
        assignment_stmt = assignment_stmt.where(DoctorBranch.doctor_id == doctor_id)

    assignments = list(db.execute(assignment_stmt).all())
    if doctor_id is not None and not assignments:
        raise BookingCompatibilityError(
            "Doctor is not available for this service at this branch.",
            dimension="doctor",
        )
    if not assignments:
        return tz.key, []

    duration_minutes = (
        int(device_price.duration_minutes)
        if device_price is not None
        else service_duration_minutes(service)
    )
    before = timedelta(0)
    duration = timedelta(minutes=duration_minutes)
    after = timedelta(0)

    doctor_ids = [doctor.id for _, _, doctor in assignments]
    day_start_local = datetime.combine(booking_date, time.min, tzinfo=tz)
    day_end_local = day_start_local + timedelta(days=1)
    day_start_utc = day_start_local.astimezone(UTC)
    day_end_utc = day_end_local.astimezone(UTC)
    conflict_start_utc = (day_start_local - before).astimezone(UTC)
    conflict_end_utc = (day_end_local + duration + after).astimezone(UTC)

    excluded_appointment_ids = set(exclude_appointment_ids)
    if exclude_appointment_id is not None:
        excluded_appointment_ids.add(exclude_appointment_id)

    appointment_stmt = select(Appointment).where(
        Appointment.workspace_id == workspace.id,
        Appointment.doctor_id.in_(doctor_ids),
        Appointment.is_quick_booking.is_(False),
        Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
        Appointment.busy_start_at < conflict_end_utc,
        Appointment.busy_end_at > conflict_start_utc,
    )
    if excluded_appointment_ids:
        appointment_stmt = appointment_stmt.where(
            Appointment.id.notin_(tuple(excluded_appointment_ids))
        )
    existing = list(db.scalars(appointment_stmt))
    availability_blocks = list(
        db.scalars(
            select(AvailabilityBlock).where(
                AvailabilityBlock.workspace_id == workspace.id,
                AvailabilityBlock.branch_id == branch.id,
                AvailabilityBlock.start_at < conflict_end_utc,
                AvailabilityBlock.end_at > conflict_start_utc,
            )
        )
    )
    by_doctor: dict[UUID, list[Appointment]] = {doctor: [] for doctor in doctor_ids}
    for appointment in existing:
        by_doctor.setdefault(appointment.doctor_id, []).append(appointment)

    device_existing: list[Appointment] = []
    if requires_device and laser_device_key:
        device_stmt = select(Appointment).where(
            Appointment.workspace_id == workspace.id,
            Appointment.laser_device_key == laser_device_key,
            Appointment.is_quick_booking.is_(False),
            Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            Appointment.busy_start_at < conflict_end_utc,
            Appointment.busy_end_at > conflict_start_utc,
        )
        if excluded_appointment_ids:
            device_stmt = device_stmt.where(
                Appointment.id.notin_(tuple(excluded_appointment_ids))
            )
        device_existing = list(db.scalars(device_stmt))

    minimum_notice_minutes = (
        settings.minimum_notice_minutes
        if minimum_notice_minutes_override is None
        else max(int(minimum_notice_minutes_override), 0)
    )
    minimum_start_utc = now_utc + timedelta(minutes=minimum_notice_minutes)
    slots: list[SlotCandidate] = []

    doctor_hour_rows = list(
        db.scalars(
            select(DoctorWorkingHour).where(
                DoctorWorkingHour.workspace_id == workspace.id,
                DoctorWorkingHour.doctor_id.in_(doctor_ids),
                DoctorWorkingHour.branch_id == branch.id,
                DoctorWorkingHour.weekday == weekday,
            )
        )
    )
    doctor_hours_by_doctor: dict[UUID, list[DoctorWorkingHour]] = {
        doctor_id_value: [] for doctor_id_value in doctor_ids
    }
    for doctor_hour in doctor_hour_rows:
        doctor_hours_by_doctor.setdefault(doctor_hour.doctor_id, []).append(doctor_hour)

    window_rows = list(
        db.scalars(
            select(DoctorAvailabilityWindow).where(
                DoctorAvailabilityWindow.workspace_id == workspace.id,
                DoctorAvailabilityWindow.doctor_id.in_(doctor_ids),
                DoctorAvailabilityWindow.branch_id == branch.id,
                DoctorAvailabilityWindow.start_at < day_end_utc,
                DoctorAvailabilityWindow.end_at > day_start_utc,
            )
        )
    )
    windows_by_doctor: dict[UUID, list[DoctorAvailabilityWindow]] = {
        doctor_id_value: [] for doctor_id_value in doctor_ids
    }
    for window in window_rows:
        windows_by_doctor.setdefault(window.doctor_id, []).append(window)

    for _, doctor_service, doctor in assignments:
        doctor_hours = doctor_hours_by_doctor.get(doctor.id, [])
        dated_windows = windows_by_doctor.get(doctor.id, [])
        if doctor.doctor_type == "visiting":
            availability_intervals = _window_intersections(
                branch_hours,
                dated_windows,
                booking_date,
                tz,
            )
        else:
            availability_intervals = _interval_intersections(
                branch_hours,
                doctor_hours,
                booking_date,
                tz,
            )
            if dated_windows:
                availability_intervals.extend(
                    _window_intersections(branch_hours, dated_windows, booking_date, tz)
                )
        if not availability_intervals:
            continue

        if device_price is not None:
            price_minor = int(device_price.price_minor or 0)
            currency = device_price.currency
            device_key = device_price.device_key
            device_name = device_price.device_name
        else:
            price_minor = (
                doctor_service.custom_price_minor
                if doctor_service.custom_price_minor is not None
                else service.price_minor
            )
            currency = service.currency or settings.default_currency
            device_key = None
            device_name = None

        release_times = [appointment.busy_end_at for appointment in by_doctor.get(doctor.id, [])]
        if device_key:
            release_times.extend(appointment.busy_end_at for appointment in device_existing)
        for interval_start, interval_end in availability_intervals:
            for candidate in _candidate_start_times(
                interval_start=interval_start,
                interval_end=interval_end,
                interval_minutes=settings.slot_interval_minutes,
                release_times=release_times,
            ):
                service_start = candidate
                service_end = service_start + duration
                busy_start = service_start - before
                busy_end = service_end + after

                start_utc = service_start.astimezone(UTC)
                end_utc = service_end.astimezone(UTC)
                busy_start_utc = busy_start.astimezone(UTC)
                busy_end_utc = busy_end.astimezone(UTC)

                doctor_busy = _overlaps_existing(
                    busy_start_utc,
                    busy_end_utc,
                    by_doctor.get(doctor.id, []),
                )
                device_busy = bool(device_key) and _overlaps_existing(
                    busy_start_utc,
                    busy_end_utc,
                    device_existing,
                )
                branch_blocked = any(
                    block.start_at < busy_end_utc and block.end_at > busy_start_utc
                    for block in availability_blocks
                )
                if (
                    start_utc >= minimum_start_utc
                    and not doctor_busy
                    and not device_busy
                    and not branch_blocked
                ):
                    slots.append(
                        SlotCandidate(
                            branch_id=branch.id,
                            doctor_id=doctor.id,
                            service_id=service.id,
                            start_at=start_utc,
                            end_at=end_utc,
                            busy_start_at=busy_start_utc,
                            busy_end_at=busy_end_utc,
                            duration_minutes=duration_minutes,
                            price_minor=price_minor,
                            currency=currency,
                            laser_device_key=device_key,
                            laser_device_name=device_name,
                        )
                    )

    slots.sort(key=lambda slot: (slot.start_at, str(slot.doctor_id)))
    return tz.key, slots


def find_exact_slot(
    db: Session,
    workspace: Workspace,
    branch_id: UUID,
    service_id: UUID,
    doctor_id: UUID,
    requested_start_at: datetime,
    exclude_appointment_id: UUID | None = None,
    laser_device_key: str | None = None,
    exclude_appointment_ids: Collection[UUID] = (),
    minimum_notice_minutes_override: int | None = None,
) -> SlotCandidate:
    requested_utc = requested_start_at.astimezone(UTC)

    branch = db.scalar(
        select(Branch).where(
            Branch.id == branch_id,
            Branch.workspace_id == workspace.id,
            Branch.is_active.is_(True),
        )
    )
    if branch is None:
        raise BookingRuleError("Branch not found or inactive.")
    tz = resolve_timezone(workspace, branch)
    booking_date = requested_utc.astimezone(tz).date()

    _, slots = calculate_availability(
        db=db,
        workspace=workspace,
        branch_id=branch_id,
        service_id=service_id,
        booking_date=booking_date,
        doctor_id=doctor_id,
        exclude_appointment_id=exclude_appointment_id,
        laser_device_key=laser_device_key,
        exclude_appointment_ids=exclude_appointment_ids,
        minimum_notice_minutes_override=minimum_notice_minutes_override,
    )
    for slot in slots:
        if slot.start_at == requested_utc:
            return slot
    raise BookingRuleError("Requested appointment slot is no longer available.")
