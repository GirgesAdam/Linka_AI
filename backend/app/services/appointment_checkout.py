from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    require_tia_workspace_domain_write,
)
from app.models.activity_event import ActivityEvent
from app.models.appointment import Appointment
from app.services.activity import record_activity_event
from app.services.appointment_operations import (
    AppointmentOperationError,
    update_operational_status_operation,
)
from app.services.payments import (
    PaymentOperationError,
    get_appointment_payment_summary,
    record_payment,
    set_appointment_discount,
)
from app.services.pulse_billing import (
    PulseBillingError,
    checkout_appointment_pulses,
    get_appointment_pulse_settlement,
)


class AppointmentCheckoutError(ValueError):
    pass


def checkout_appointment(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    amount_minor: int,
    payment_method: str,
    discount_minor: int,
    external_reference: str | None,
    pulse_mode: str,
    pulse_pack_offer_id: UUID | None,
    created_by_user_id: UUID | None,
    idempotency_key: str | None,
) -> None:
    if amount_minor < 0:
        raise AppointmentCheckoutError("Checkout amount cannot be negative.")
    # Checkout is both a payment write and (for elapsed open visits) an
    # operational status write. Both authority checks precede all mutations.
    try:
        for domain in ("payments", "appointments"):
            require_tia_workspace_domain_write(
                db,
                workspace_id=workspace_id,
                domain=domain,
            )
    except ClinicIntegrationAuthorityError as exc:
        raise AppointmentCheckoutError(str(exc)) from exc

    appointment = db.scalar(
        select(Appointment)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.id == appointment_id,
        )
        .with_for_update()
    )
    if appointment is None:
        raise AppointmentCheckoutError("Appointment not found.")

    # The appointment FOR UPDATE serializes concurrent checkout retries.
    # The existing activity-events table stores the intent receipt in the same
    # transaction as payments, entitlement consumption, and status history.
    intent = {
        "amount_minor": amount_minor,
        "payment_method": payment_method,
        "discount_minor": discount_minor,
        "external_reference": external_reference,
        "pulse_mode": pulse_mode,
        "pulse_pack_offer_id": str(pulse_pack_offer_id) if pulse_pack_offer_id else None,
    }
    if idempotency_key:
        previous = db.scalar(
            select(ActivityEvent).where(
                ActivityEvent.workspace_id == workspace_id,
                ActivityEvent.action == "appointment.checkout",
                ActivityEvent.entity_type == "appointment",
                ActivityEvent.entity_id == appointment_id,
                ActivityEvent.metadata_json["idempotency_key"].astext == idempotency_key,
            )
        )
        if previous is not None:
            if any((previous.metadata_json or {}).get(key) != value for key, value in intent.items()):
                raise AppointmentCheckoutError(
                    "Idempotency key was already used for a different checkout."
                )
            return

    if appointment.status not in {"pending", "confirmed", "checked_in", "in_progress", "completed"}:
        raise AppointmentCheckoutError(
            f"Checkout is not allowed for an appointment in '{appointment.status}' status."
        )
    now = datetime.now(UTC)
    is_open_visit = appointment.status != "completed"
    is_future = now < appointment.start_at
    if is_future and amount_minor == 0:
        raise AppointmentCheckoutError(
            "The appointment cannot be completed before its start time."
        )
    if appointment.status in {"checked_in", "in_progress"} and is_future:
        raise AppointmentCheckoutError(
            "A legacy in-progress appointment cannot be checked out before its start time."
        )
    if appointment.status == "completed" and pulse_mode != "none":
        raise AppointmentCheckoutError(
            "Pulse settlement cannot be changed after appointment completion."
        )

    if pulse_mode != "none":
        try:
            checkout_appointment_pulses(
                db,
                workspace_id=workspace_id,
                appointment_id=appointment_id,
                mode=pulse_mode,
                offer_id=pulse_pack_offer_id,
                changed_by_user_id=created_by_user_id,
                idempotency_key=idempotency_key,
            )
        except PulseBillingError as exc:
            raise AppointmentCheckoutError(str(exc)) from exc
    elif appointment.billing_context == "pulse_prepaid":
        settlement = get_appointment_pulse_settlement(
            db,
            workspace_id=workspace_id,
            appointment_id=appointment_id,
        )
        if settlement is None or settlement.resolution == "pending":
            raise AppointmentCheckoutError(
                "Resolve the pulse usage before recording this appointment payment."
            )

    try:
        if amount_minor > 0:
            record_payment(
                db,
                workspace_id=workspace_id,
                appointment_id=appointment_id,
                amount_minor=amount_minor,
                payment_method=payment_method,
                discount_minor=discount_minor,
                external_reference=external_reference,
                created_by_user_id=created_by_user_id,
                idempotency_key=idempotency_key,
            )
        else:
            set_appointment_discount(
                db,
                workspace_id=workspace_id,
                appointment_id=appointment_id,
                discount_minor=discount_minor,
                created_by_user_id=created_by_user_id,
            )
            summary = get_appointment_payment_summary(
                db,
                workspace_id=workspace_id,
                appointment_id=appointment_id,
            )
            if summary.balance_minor > 0:
                raise AppointmentCheckoutError(
                    "This checkout still has an outstanding balance. Enter a payment amount."
                )
    except PaymentOperationError as exc:
        raise AppointmentCheckoutError(str(exc)) from exc

    # Future visits may accept a legitimate prepayment, but stay booked.
    # Already-completed visits can accept further payments without repeating
    # the completion side effects or consuming more package sessions.
    if is_open_visit and not is_future:
        try:
            update_operational_status_operation(
                db,
                workspace_id=workspace_id,
                appointment_id=appointment_id,
                target_status="completed",
                changed_by_user_id=created_by_user_id,
                reason="appointment_checkout_completed",
                now=now,
            )
        except AppointmentOperationError as exc:
            raise AppointmentCheckoutError(str(exc)) from exc

    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff" if created_by_user_id else "system",
        actor_user_id=created_by_user_id,
        action="appointment.checkout",
        entity_type="appointment",
        entity_id=appointment_id,
        summary="Appointment checkout saved",
        metadata={**intent, "idempotency_key": idempotency_key},
    )
