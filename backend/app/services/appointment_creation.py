from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.lead import Lead
from app.models.workspace import Workspace
from app.services.activity import ActivityActorType, record_activity_event
from app.services.appointment_confirmation import initial_confirmation_decision
from app.services.appointment_operations import AppointmentOperationError, add_appointment_history
from app.services.booking import BookingRuleError, find_exact_slot
from app.services.inventory import InventoryOperationError, configured_device_price
from app.services.patient_packages import (
    PackageOperationError,
    reserve_package_usage,
    validate_package_for_booking,
)
from app.services.pulse_billing import PulseBillingError, validate_pulse_booking


def create_appointment_operation(
    db: Session,
    *,
    workspace: Workspace,
    patient_id: UUID,
    branch_id: UUID,
    doctor_id: UUID,
    service_id: UUID,
    requested_start_at: datetime,
    created_by_user_id: UUID | None,
    patient_package_id: UUID | None = None,
    use_pulse_balance: bool = False,
    visit_group_id: UUID | None = None,
    lead: Lead | None = None,
    source: str = "staff",
    customer_note: str | None = None,
    laser_device_key: str | None = None,
    idempotency_key: str | None = None,
    actor_type: ActivityActorType = "staff",
    now: datetime | None = None,
) -> Appointment:
    """Create one canonical appointment without owning the outer transaction."""
    if idempotency_key:
        existing = db.scalar(
            select(Appointment).where(
                Appointment.workspace_id == workspace.id,
                Appointment.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing

    try:
        configured_device_price(
            db,
            workspace_id=workspace.id,
            service_id=service_id,
            device_key=laser_device_key,
        )
        slot = find_exact_slot(
            db=db,
            workspace=workspace,
            branch_id=branch_id,
            service_id=service_id,
            doctor_id=doctor_id,
            requested_start_at=requested_start_at,
            laser_device_key=laser_device_key,
        )
    except (BookingRuleError, InventoryOperationError) as exc:
        raise AppointmentOperationError(str(exc)) from exc

    if patient_package_id is not None and use_pulse_balance:
        raise AppointmentOperationError(
            "Choose either a session package or pulse balance, not both."
        )

    patient_package = None
    if patient_package_id is not None:
        try:
            patient_package = validate_package_for_booking(
                db,
                workspace_id=workspace.id,
                package_id=patient_package_id,
                patient_id=patient_id,
                service_id=service_id,
                appointment_start_at=slot.start_at,
                laser_device_key=slot.laser_device_key,
            )
        except PackageOperationError as exc:
            raise AppointmentOperationError(str(exc)) from exc

    if use_pulse_balance:
        try:
            validate_pulse_booking(
                db,
                workspace_id=workspace.id,
                patient_id=patient_id,
                device_key=slot.laser_device_key,
                appointment_date=slot.start_at.astimezone(
                    ZoneInfo(workspace.timezone or "UTC")
                ).date(),
            )
        except PulseBillingError as exc:
            raise AppointmentOperationError(str(exc)) from exc

    occurred_at = (now or datetime.now(UTC)).astimezone(UTC)
    confirmation = initial_confirmation_decision(
        db,
        workspace_id=workspace.id,
        branch_id=branch_id,
        start_at=slot.start_at,
        now=occurred_at,
    )
    initial_status = confirmation.status
    appointment = Appointment(
        workspace_id=workspace.id,
        patient_id=patient_id,
        branch_id=branch_id,
        doctor_id=doctor_id,
        service_id=service_id,
        patient_package_id=patient_package_id,
        visit_group_id=visit_group_id,
        lead_id=lead.id if lead is not None else None,
        created_by_user_id=created_by_user_id,
        status=initial_status,
        source=source,
        start_at=slot.start_at,
        end_at=slot.end_at,
        busy_start_at=slot.busy_start_at,
        busy_end_at=slot.busy_end_at,
        duration_minutes=slot.duration_minutes,
        price_minor=slot.price_minor,
        currency=slot.currency,
        laser_device_key=slot.laser_device_key,
        laser_device_name=slot.laser_device_name,
        billing_context="pulse_prepaid" if use_pulse_balance else "standard",
        customer_note=customer_note,
        idempotency_key=idempotency_key,
        confirmed_at=confirmation.confirmed_at,
    )

    db.add(appointment)
    db.flush()
    if patient_package is not None:
        try:
            reserve_package_usage(
                db,
                appointment=appointment,
                package=patient_package,
                actor_type=actor_type,
                actor_user_id=created_by_user_id,
            )
        except PackageOperationError as exc:
            raise AppointmentOperationError(str(exc)) from exc

    add_appointment_history(
        db,
        appointment=appointment,
        changed_by_user_id=created_by_user_id,
        from_status=None,
        to_status=initial_status,
        reason="appointment_created",
    )
    if lead is not None and lead.status not in {"lost", "spam", "won"}:
        if lead.service_id is None:
            lead.service_id = service_id
        lead.status = "booked"

    record_activity_event(
        db,
        workspace_id=workspace.id,
        actor_type=actor_type,
        actor_user_id=created_by_user_id,
        action="appointment.created",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Appointment created",
        metadata={
            "status": initial_status,
            "source": appointment.source,
            "patient_package_id": appointment.patient_package_id,
            "laser_device_key": appointment.laser_device_key,
            "billing_context": appointment.billing_context,
        },
    )
    db.flush()
    return appointment
