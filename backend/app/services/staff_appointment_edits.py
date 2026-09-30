from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import ACTIVE_APPOINTMENT_STATUSES, Appointment
from app.models.branch import Branch
from app.models.doctor_service import DoctorService
from app.models.patient_package import PatientPackage
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.activity import ActivityActorType, record_activity_event
from app.services.booking import (
    BookingRuleError,
    SlotCandidate,
    calculate_availability,
    resolve_timezone,
)
from app.services.inventory import InventoryOperationError, configured_device_price
from app.services.patient_packages import PackageOperationError, release_package_usage
from app.services.payments import refresh_appointment_payment_snapshots

_EDITABLE_STATUSES = frozenset({"pending", "confirmed", "checked_in", "in_progress"})


class StaffAppointmentEditError(ValueError):
    pass


class StaffAppointmentEditNotFound(StaffAppointmentEditError):
    pass


def _locked_appointment(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
) -> Appointment:
    appointment = db.scalar(
        select(Appointment)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.id == appointment_id,
        )
        .with_for_update()
    )
    if appointment is None:
        raise StaffAppointmentEditNotFound("Appointment not found.")
    return appointment


def _overlapping_appointment_id(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    busy_start_at: datetime,
    busy_end_at: datetime,
    doctor_id: UUID | None = None,
    laser_device_key: str | None = None,
) -> UUID | None:
    stmt = select(Appointment.id).where(
        Appointment.workspace_id == workspace_id,
        Appointment.id != appointment_id,
        Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
        Appointment.busy_start_at < busy_end_at,
        Appointment.busy_end_at > busy_start_at,
    )
    if doctor_id is not None:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if laser_device_key is not None:
        stmt = stmt.where(Appointment.laser_device_key == laser_device_key)
    return db.scalar(stmt.limit(1))


def _ensure_operational_category_available(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    branch_id: UUID,
    service_id: UUID,
    busy_start_at: datetime,
    busy_end_at: datetime,
) -> None:
    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace_id,
            Service.id == service_id,
        )
    )
    if service is None:
        raise StaffAppointmentEditError("Service not found or inactive.")
    if service.requires_laser_device:
        return

    conflict_id = db.scalar(
        select(Appointment.id)
        .join(
            Service,
            (Service.workspace_id == Appointment.workspace_id)
            & (Service.id == Appointment.service_id),
        )
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.branch_id == branch_id,
            Appointment.id != appointment_id,
            Appointment.is_quick_booking.is_(False),
            Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            Appointment.busy_start_at < busy_end_at,
            Appointment.busy_end_at > busy_start_at,
            Service.operational_category == service.operational_category,
        )
        .limit(1)
    )
    if conflict_id is not None:
        raise StaffAppointmentEditError(
            "Another appointment in the same service category already occupies this time."
        )


def _same_start_slot_for_current_doctor(
    db: Session,
    *,
    workspace: Workspace,
    appointment: Appointment,
    service_id: UUID,
    laser_device_key: str | None,
) -> SlotCandidate:
    """Validate an in-place correction without reapplying today's work schedule.

    An appointment can legitimately sit outside the doctor's *current* weekly hours
    when those hours were edited after the booking. Staff must still be able to
    correct the service or laser device on that appointment. For the same doctor and
    exact same start time we therefore validate the durable facts that matter now:
    service assignment (when changing service), duration, price, and doctor/device
    conflicts. A real time/doctor move still goes through normal availability below.
    """
    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.id == service_id,
            Service.is_active.is_(True),
        )
    )
    if service is None:
        raise StaffAppointmentEditError("Service not found or inactive.")

    service_changed = service_id != appointment.service_id
    doctor_service: DoctorService | None = None
    start_at = appointment.start_at.astimezone(UTC)

    if service_changed:
        doctor_service = db.scalar(
            select(DoctorService).where(
                DoctorService.workspace_id == workspace.id,
                DoctorService.doctor_id == appointment.doctor_id,
                DoctorService.service_id == service_id,
                DoctorService.is_active.is_(True),
            )
        )
        if doctor_service is None:
            raise StaffAppointmentEditError("Doctor does not provide the selected service.")

        duration_minutes = int(service.duration_minutes)
        end_at = start_at + timedelta(minutes=duration_minutes)
        busy_start_at = start_at
        busy_end_at = end_at
        if _overlapping_appointment_id(
            db,
            workspace_id=workspace.id,
            appointment_id=appointment.id,
            doctor_id=appointment.doctor_id,
            busy_start_at=busy_start_at,
            busy_end_at=busy_end_at,
        ) is not None:
            raise StaffAppointmentEditError(
                "The selected doctor has another appointment that conflicts with this service duration."
            )
    else:
        duration_minutes = int(appointment.duration_minutes)
        end_at = appointment.end_at.astimezone(UTC)
        busy_start_at = appointment.busy_start_at.astimezone(UTC)
        busy_end_at = appointment.busy_end_at.astimezone(UTC)

    normalized_device = (laser_device_key or "").strip() or None
    requires_device = bool(service.requires_laser_device)
    if requires_device and normalized_device is None:
        raise StaffAppointmentEditError("Laser device choice is required for this service.")
    if not requires_device and normalized_device is not None:
        raise StaffAppointmentEditError("A laser device cannot be selected for this service.")

    device_key: str | None = None
    device_name: str | None = None
    if requires_device:
        try:
            device_price = configured_device_price(
                db,
                workspace_id=workspace.id,
                service_id=service.id,
                device_key=normalized_device,
            )
        except InventoryOperationError as exc:
            raise StaffAppointmentEditError(str(exc)) from exc
        price_minor = int(device_price.price_minor or 0)
        currency = device_price.currency
        device_key = device_price.device_key
        device_name = device_price.device_name
        if _overlapping_appointment_id(
            db,
            workspace_id=workspace.id,
            appointment_id=appointment.id,
            laser_device_key=device_key,
            busy_start_at=busy_start_at,
            busy_end_at=busy_end_at,
        ) is not None:
            raise StaffAppointmentEditError("The selected laser device is already booked at this time.")
    elif service_changed:
        assert doctor_service is not None
        price_minor = int(
            doctor_service.custom_price_minor
            if doctor_service.custom_price_minor is not None
            else service.price_minor
        )
        currency = service.currency or appointment.currency
    else:
        price_minor = int(appointment.price_minor)
        currency = appointment.currency

    return SlotCandidate(
        branch_id=appointment.branch_id,
        doctor_id=appointment.doctor_id,
        service_id=service.id,
        start_at=start_at,
        end_at=end_at,
        busy_start_at=busy_start_at,
        busy_end_at=busy_end_at,
        duration_minutes=duration_minutes,
        price_minor=price_minor,
        currency=currency,
        laser_device_key=device_key,
        laser_device_name=device_name,
    )


def _validated_slot_for_existing_appointment(
    db: Session,
    *,
    workspace: Workspace,
    appointment: Appointment,
    service_id: UUID,
    doctor_id: UUID,
    laser_device_key: str | None,
    requested_start_at: datetime | None = None,
) -> SlotCandidate:
    """Validate a staff edit at the current or explicitly requested time."""
    branch = db.scalar(
        select(Branch).where(
            Branch.workspace_id == workspace.id,
            Branch.id == appointment.branch_id,
            Branch.is_active.is_(True),
        )
    )
    if branch is None:
        raise StaffAppointmentEditError("Appointment branch is missing or inactive.")

    timezone = resolve_timezone(workspace, branch)
    if requested_start_at is None:
        requested_start = appointment.start_at.astimezone(UTC)
    elif requested_start_at.tzinfo is None:
        requested_start = requested_start_at.replace(tzinfo=timezone).astimezone(UTC)
    else:
        requested_start = requested_start_at.astimezone(UTC)

    current_start = appointment.start_at.astimezone(UTC)
    if doctor_id == appointment.doctor_id and requested_start == current_start:
        slot = _same_start_slot_for_current_doctor(
            db,
            workspace=workspace,
            appointment=appointment,
            service_id=service_id,
            laser_device_key=laser_device_key,
        )
        if service_id != appointment.service_id:
            _ensure_operational_category_available(
                db,
                workspace_id=workspace.id,
                appointment_id=appointment.id,
                branch_id=appointment.branch_id,
                service_id=slot.service_id,
                busy_start_at=slot.busy_start_at,
                busy_end_at=slot.busy_end_at,
            )
        return slot

    booking_date = requested_start.astimezone(timezone).date()
    # Staff correction/rescheduling should not be blocked by minimum notice or
    # the fact that an old record is being repaired. Doctor hours, service/device
    # assignment and overlap rules still remain authoritative for an actual move.
    validation_now = requested_start - timedelta(days=1)
    try:
        _, slots = calculate_availability(
            db=db,
            workspace=workspace,
            branch_id=appointment.branch_id,
            service_id=service_id,
            booking_date=booking_date,
            doctor_id=doctor_id,
            exclude_appointment_id=appointment.id,
            now=validation_now,
            preloaded_branch=branch,
            laser_device_key=laser_device_key,
        )
    except BookingRuleError as exc:
        raise StaffAppointmentEditError(str(exc)) from exc

    for slot in slots:
        if slot.start_at == requested_start:
            if requested_start != current_start or service_id != appointment.service_id:
                _ensure_operational_category_available(
                    db,
                    workspace_id=workspace.id,
                    appointment_id=appointment.id,
                    branch_id=appointment.branch_id,
                    service_id=slot.service_id,
                    busy_start_at=slot.busy_start_at,
                    busy_end_at=slot.busy_end_at,
                )
            return slot
    raise StaffAppointmentEditError(
        "Requested appointment time is not available for the selected service/device with the selected doctor."
    )


def change_appointment_service(
    db: Session,
    *,
    workspace: Workspace,
    appointment_id: UUID,
    service_id: UUID,
    doctor_id: UUID | None = None,
    laser_device_key: str | None,
    start_at: datetime | None = None,
    changed_by_user_id: UUID | None,
    actor_type: ActivityActorType = "staff",
) -> Appointment:
    """Edit service/doctor/device/time in place from the staff dashboard only.

    Payment allocations stay attached to the appointment and its payment snapshot is
    recalculated against the new service price. If the appointment was reserving a
    package that no longer matches the selected service/device, that reservation is
    released and the visit is converted to ordinary billing. No new package is
    auto-selected on a manual edit.
    """
    appointment = _locked_appointment(
        db,
        workspace_id=workspace.id,
        appointment_id=appointment_id,
    )
    if appointment.status not in _EDITABLE_STATUSES:
        raise StaffAppointmentEditError(
            "Only a pending, confirmed, checked-in or in-progress appointment can be edited."
        )

    normalized_device = (laser_device_key or "").strip() or None
    selected_doctor_id = doctor_id or appointment.doctor_id
    service_changed = service_id != appointment.service_id
    doctor_changed = selected_doctor_id != appointment.doctor_id
    device_changed = normalized_device != (appointment.laser_device_key or None)
    if not service_changed and not doctor_changed and not device_changed and start_at is None:
        return appointment

    slot = _validated_slot_for_existing_appointment(
        db,
        workspace=workspace,
        appointment=appointment,
        service_id=service_id,
        doctor_id=selected_doctor_id,
        laser_device_key=normalized_device,
        requested_start_at=start_at,
    )

    old_service_id = appointment.service_id
    old_doctor_id = appointment.doctor_id
    old_device_key = appointment.laser_device_key
    old_start_at = appointment.start_at
    old_price_minor = int(appointment.price_minor)
    old_billing_context = appointment.billing_context
    old_package_id = appointment.patient_package_id
    package_released = False

    package_incompatible = service_changed
    if (
        not package_incompatible
        and device_changed
        and appointment.patient_package_id is not None
    ):
        package = db.scalar(
            select(PatientPackage).where(
                PatientPackage.workspace_id == workspace.id,
                PatientPackage.id == appointment.patient_package_id,
            )
        )
        if package is None:
            raise StaffAppointmentEditError("Appointment package is missing.")
        package_incompatible = bool(
            package.laser_device_key is not None
            and package.laser_device_key != slot.laser_device_key
        )

    if appointment.billing_context == "pulse_prepaid" and (service_changed or device_changed):
        raise StaffAppointmentEditError(
            "Pulse-billed appointments cannot change service or laser device after booking. "
            "Switch billing back to a standard visit first."
        )

    if package_incompatible and (
        appointment.patient_package_id is not None
        or appointment.billing_context == "package_prepaid"
        or appointment.package_external_id
    ):
        if appointment.patient_package_id is not None:
            try:
                release_package_usage(
                    db,
                    appointment=appointment,
                    actor_type=actor_type,
                    actor_user_id=changed_by_user_id,
                    reason=(
                        "staff_service_changed"
                        if service_changed
                        else "staff_laser_device_changed"
                    ),
                )
            except PackageOperationError as exc:
                raise StaffAppointmentEditError(str(exc)) from exc
        appointment.patient_package_id = None
        appointment.billing_context = "standard"
        appointment.package_external_id = None
        package_released = True

    appointment.service_id = slot.service_id
    appointment.doctor_id = slot.doctor_id
    appointment.start_at = slot.start_at
    appointment.end_at = slot.end_at
    appointment.busy_start_at = slot.busy_start_at
    appointment.busy_end_at = slot.busy_end_at
    appointment.duration_minutes = slot.duration_minutes
    appointment.price_minor = slot.price_minor
    appointment.currency = slot.currency
    appointment.laser_device_key = slot.laser_device_key
    appointment.laser_device_name = slot.laser_device_name
    db.flush()

    refresh_appointment_payment_snapshots(
        db,
        workspace_id=workspace.id,
        appointment_ids={appointment.id},
    )
    record_activity_event(
        db,
        workspace_id=workspace.id,
        actor_type=actor_type,
        actor_user_id=changed_by_user_id,
        action="appointment.service_changed_by_staff",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Appointment details changed by staff",
        metadata={
            "old_service_id": old_service_id,
            "new_service_id": appointment.service_id,
            "old_doctor_id": old_doctor_id,
            "new_doctor_id": appointment.doctor_id,
            "old_device_key": old_device_key,
            "new_device_key": appointment.laser_device_key,
            "old_start_at": old_start_at.isoformat(),
            "new_start_at": appointment.start_at.isoformat(),
            "old_price_minor": old_price_minor,
            "new_price_minor": int(appointment.price_minor),
            "old_billing_context": old_billing_context,
            "new_billing_context": appointment.billing_context,
            "old_package_id": old_package_id,
            "package_reservation_released": package_released,
        },
    )
    db.flush()
    return appointment
