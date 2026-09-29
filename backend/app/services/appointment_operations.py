from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.appointment_status_history import AppointmentStatusHistory
from app.models.automation_job import AutomationJob
from app.models.payment_transaction import PaymentAllocation
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.activity import ActivityActorType, record_activity_event
from app.services.booking import BookingRuleError, find_exact_slot, get_effective_booking_settings
from app.services.campaign_attribution import transfer_campaign_booking_conversion
from app.services.patient_packages import (
    PackageOperationError,
    consume_visit_package_usages,
    release_visit_package_usages,
    transfer_package_usage,
)
from app.services.payments import reallocate_appointment_payments_on_reschedule
from app.services.pulse_billing import (
    PulseBillingError,
    release_appointment_pulse_usage,
    require_pulse_settlement_before_completion,
)

AppointmentOperationAction = Literal[
    "confirm",
    "reschedule",
    "cancel",
    "complete",
    "no_show",
]


class AppointmentOperationError(ValueError):
    pass


class AppointmentOperationNotFound(AppointmentOperationError):
    pass


class AppointmentCancellationOverrideRequired(AppointmentOperationError):
    pass


class AppointmentOperationForbidden(AppointmentOperationError):
    pass


class AppointmentServiceChangeRequiresHuman(AppointmentOperationError):
    pass


def service_change_requires_human(
    *,
    payment_status: str,
    amount_paid_minor: int | None,
    billing_context: str,
    patient_package_id: UUID | None,
    package_external_id: str | None,
    has_payment_allocation: bool,
) -> bool:
    """Fail closed when changing service could alter money or package entitlement."""
    return bool(
        patient_package_id is not None
        or billing_context in {"package_prepaid", "pulse_prepaid"}
        or package_external_id
        or payment_status not in {"unknown", "unpaid"}
        or int(amount_paid_minor or 0) > 0
        or has_payment_allocation
    )


def appointment_allowed_actions(
    *,
    appointment_status: str,
    start_at: datetime,
    now: datetime | None = None,
) -> tuple[AppointmentOperationAction, ...]:
    """Return UI hints for the canonical appointment state machine.

    These hints are not authorization. Every write path revalidates the same
    transition under a database row lock before mutating state.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    actions: list[AppointmentOperationAction] = []

    if appointment_status == "pending":
        if now < start_at:
            actions.extend(("confirm", "reschedule", "cancel"))
        else:
            actions.extend(("complete", "no_show"))
    elif appointment_status == "confirmed":
        if now < start_at:
            actions.extend(("reschedule", "cancel"))
        else:
            actions.extend(("complete", "no_show"))
    elif appointment_status in {"checked_in", "in_progress"}:
        # Legacy states stay readable but are no longer created by Linka. Allow
        # existing rows to be closed without reintroducing those workflow steps.
        if now >= start_at:
            actions.extend(("complete", "no_show"))

    return tuple(actions)


def cancellation_override_required(
    *,
    appointment_status: str,
    start_at: datetime,
    cancellation_notice_minutes: int,
    now: datetime | None = None,
) -> bool:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if appointment_status not in {"pending", "confirmed"}:
        return False
    if now >= start_at:
        return False
    return start_at - now < timedelta(minutes=cancellation_notice_minutes)


def _locked_appointment(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    patient_id: UUID | None = None,
) -> Appointment:
    stmt = select(Appointment).where(
        Appointment.workspace_id == workspace_id,
        Appointment.id == appointment_id,
    )
    if patient_id is not None:
        stmt = stmt.where(Appointment.patient_id == patient_id)
    appointment = db.scalar(stmt.with_for_update())
    if appointment is None:
        raise AppointmentOperationNotFound("Appointment not found.")
    return appointment


def add_appointment_history(
    db: Session,
    *,
    appointment: Appointment,
    changed_by_user_id: UUID | None,
    from_status: str | None,
    to_status: str,
    reason: str | None = None,
    metadata: dict | None = None,
) -> None:
    db.add(
        AppointmentStatusHistory(
            workspace_id=appointment.workspace_id,
            appointment_id=appointment.id,
            changed_by_user_id=changed_by_user_id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            metadata_json=metadata or {},
        )
    )


def cancel_pending_appointment_jobs(
    db: Session,
    *,
    appointment: Appointment,
    reason: str,
    now: datetime,
) -> None:
    jobs = db.scalars(
        select(AutomationJob).where(
            AutomationJob.workspace_id == appointment.workspace_id,
            AutomationJob.appointment_id == appointment.id,
            AutomationJob.job_kind == "appointment_rule",
            AutomationJob.status.in_(("queued", "failed")),
        )
    )
    for job in jobs:
        job.status = "cancelled"
        job.completed_at = now
        job.next_attempt_at = None
        job.locked_at = None
        job.result_json = {"reason": reason}


def confirm_appointment_operation(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    changed_by_user_id: UUID | None,
    patient_id: UUID | None = None,
    reason: str = "appointment_confirmed",
    actor_type: ActivityActorType = "staff",
    now: datetime | None = None,
) -> Appointment:
    appointment = _locked_appointment(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
        patient_id=patient_id,
    )
    if appointment.status == "confirmed":
        return appointment
    if appointment.status != "pending":
        raise AppointmentOperationError(
            f"Cannot confirm an appointment with status '{appointment.status}'."
        )

    now = (now or datetime.now(UTC)).astimezone(UTC)
    old_status = appointment.status
    appointment.status = "confirmed"
    appointment.confirmed_at = now
    add_appointment_history(
        db,
        appointment=appointment,
        changed_by_user_id=changed_by_user_id,
        from_status=old_status,
        to_status="confirmed",
        reason=reason,
    )
    record_activity_event(
        db,
        workspace_id=appointment.workspace_id,
        actor_type=actor_type,
        actor_user_id=changed_by_user_id,
        action="appointment.confirmed",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Appointment confirmed",
        metadata={"from_status": old_status, "to_status": "confirmed"},
    )
    db.flush()
    return appointment


def cancel_appointment_operation(
    db: Session,
    *,
    workspace: Workspace,
    appointment_id: UUID,
    changed_by_user_id: UUID | None,
    reason: str,
    patient_id: UUID | None = None,
    override_policy: bool = False,
    actor_is_admin: bool = False,
    actor_type: ActivityActorType = "staff",
    now: datetime | None = None,
) -> Appointment:
    appointment = _locked_appointment(
        db,
        workspace_id=workspace.id,
        appointment_id=appointment_id,
        patient_id=patient_id,
    )
    if appointment.status == "cancelled":
        return appointment
    if appointment.status not in {"pending", "confirmed"}:
        raise AppointmentOperationError(
            f"Cannot cancel an appointment with status '{appointment.status}'."
        )

    now = (now or datetime.now(UTC)).astimezone(UTC)
    if now >= appointment.start_at:
        raise AppointmentOperationError(
            "An appointment cannot be cancelled after its start time. Use the operational status instead."
        )

    settings = get_effective_booking_settings(db, workspace.id)
    if cancellation_override_required(
        appointment_status=appointment.status,
        start_at=appointment.start_at,
        cancellation_notice_minutes=settings.cancellation_notice_minutes,
        now=now,
    ):
        if not override_policy:
            raise AppointmentCancellationOverrideRequired(
                "Cancellation is inside the configured notice window. An admin override is required."
            )
        if not actor_is_admin:
            raise AppointmentOperationForbidden(
                "Only an admin can override the cancellation notice policy."
            )

    old_status = appointment.status
    appointment.status = "cancelled"
    appointment.cancelled_at = now
    appointment.cancellation_reason = reason.strip()
    add_appointment_history(
        db,
        appointment=appointment,
        changed_by_user_id=changed_by_user_id,
        from_status=old_status,
        to_status="cancelled",
        reason=appointment.cancellation_reason,
        metadata={"override_policy": bool(override_policy)},
    )
    cancel_pending_appointment_jobs(
        db,
        appointment=appointment,
        reason="appointment_cancelled",
        now=now,
    )
    try:
        release_visit_package_usages(
            db,
            appointment=appointment,
            actor_type=actor_type,
            actor_user_id=changed_by_user_id,
            reason="appointment_cancelled",
        )
    except PackageOperationError as exc:
        raise AppointmentOperationError(str(exc)) from exc
    release_appointment_pulse_usage(
        db,
        appointment=appointment,
        changed_by_user_id=changed_by_user_id,
        reason="appointment_cancelled",
    )
    record_activity_event(
        db,
        workspace_id=appointment.workspace_id,
        actor_type=actor_type,
        actor_user_id=changed_by_user_id,
        action="appointment.cancelled",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Appointment cancelled",
        metadata={
            "from_status": old_status,
            "to_status": "cancelled",
            "override_policy": bool(override_policy),
        },
    )
    db.flush()
    return appointment


def reschedule_appointment_operation(
    db: Session,
    *,
    workspace: Workspace,
    appointment_id: UUID,
    requested_start_at: datetime,
    changed_by_user_id: UUID | None,
    branch_id: UUID | None = None,
    doctor_id: UUID | None = None,
    service_id: UUID | None = None,
    laser_device_key: str | None = None,
    patient_id: UUID | None = None,
    reason: str = "appointment_rescheduled",
    idempotency_key: str | None = None,
    actor_type: ActivityActorType = "staff",
    now: datetime | None = None,
    exclude_appointment_ids: tuple[UUID, ...] = (),
    minimum_notice_minutes_override: int | None = None,
) -> tuple[Appointment, Appointment]:
    if idempotency_key:
        existing = db.scalar(
            select(Appointment).where(
                Appointment.workspace_id == workspace.id,
                Appointment.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            previous = db.get(Appointment, existing.rescheduled_from_appointment_id)
            if previous is None:
                raise AppointmentOperationError(
                    "Idempotent reschedule replacement is missing its previous appointment."
                )
            return existing, previous

    current = _locked_appointment(
        db,
        workspace_id=workspace.id,
        appointment_id=appointment_id,
        patient_id=patient_id,
    )
    if current.status not in {"pending", "confirmed"}:
        raise AppointmentOperationError(
            "Only pending or confirmed appointments can be rescheduled; "
            f"current status is '{current.status}'."
        )
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if now >= current.start_at:
        raise AppointmentOperationError(
            "An appointment cannot be rescheduled after its start time. Use the operational status instead."
        )

    new_branch_id = branch_id or current.branch_id
    new_doctor_id = doctor_id or current.doctor_id
    new_service_id = service_id or current.service_id
    target_service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.id == new_service_id,
            Service.is_active.is_(True),
        )
    )
    if target_service is None:
        raise AppointmentOperationError("Replacement service not found or inactive.")
    new_laser_device_key = (
        laser_device_key or current.laser_device_key
        if bool(getattr(target_service, "requires_laser_device", False))
        else None
    )
    service_changed = new_service_id != current.service_id
    device_changed = new_laser_device_key != current.laser_device_key
    if service_changed or device_changed:
        has_payment_allocation = (
            db.scalar(
                select(PaymentAllocation.id)
                .where(
                    PaymentAllocation.workspace_id == workspace.id,
                    PaymentAllocation.appointment_id == current.id,
                )
                .limit(1)
            )
            is not None
        )
        if service_change_requires_human(
            payment_status=current.payment_status,
            amount_paid_minor=current.amount_paid_minor,
            billing_context=current.billing_context,
            patient_package_id=current.patient_package_id,
            package_external_id=current.package_external_id,
            has_payment_allocation=has_payment_allocation,
        ):
            raise AppointmentServiceChangeRequiresHuman(
                "Changing the service or laser device on this appointment needs staff review because "
                "payment or package state is attached to the booking."
            )

    try:
        slot = find_exact_slot(
            db=db,
            workspace=workspace,
            branch_id=new_branch_id,
            service_id=new_service_id,
            doctor_id=new_doctor_id,
            requested_start_at=requested_start_at,
            exclude_appointment_id=current.id,
            exclude_appointment_ids=exclude_appointment_ids,
            laser_device_key=new_laser_device_key,
            minimum_notice_minutes_override=minimum_notice_minutes_override,
        )
    except BookingRuleError as exc:
        raise AppointmentOperationError(str(exc)) from exc

    old_status = current.status
    old_start = current.start_at
    old_end = current.end_at

    replacement = Appointment(
        workspace_id=workspace.id,
        patient_id=current.patient_id,
        branch_id=new_branch_id,
        doctor_id=new_doctor_id,
        service_id=new_service_id,
        patient_package_id=current.patient_package_id,
        visit_group_id=current.visit_group_id,
        lead_id=current.lead_id,
        created_by_user_id=changed_by_user_id,
        rescheduled_from_appointment_id=current.id,
        status=old_status,
        source=current.source,
        start_at=slot.start_at,
        end_at=slot.end_at,
        busy_start_at=slot.busy_start_at,
        busy_end_at=slot.busy_end_at,
        duration_minutes=slot.duration_minutes,
        price_minor=slot.price_minor,
        currency=slot.currency,
        laser_device_key=slot.laser_device_key,
        laser_device_name=slot.laser_device_name,
        payment_status=current.payment_status,
        amount_paid_minor=current.amount_paid_minor,
        payment_method=current.payment_method,
        billing_context=current.billing_context,
        package_external_id=current.package_external_id,
        customer_note=current.customer_note,
        idempotency_key=idempotency_key,
        confirmed_at=now if old_status == "confirmed" else None,
    )

    current.status = "rescheduled"
    db.flush()
    db.add(replacement)
    db.flush()
    reallocate_appointment_payments_on_reschedule(
        db,
        workspace_id=workspace.id,
        from_appointment_id=current.id,
        to_appointment_id=replacement.id,
    )
    try:
        transfer_package_usage(
            db,
            from_appointment=current,
            to_appointment=replacement,
        )
    except PackageOperationError as exc:
        raise AppointmentOperationError(str(exc)) from exc
    release_appointment_pulse_usage(
        db,
        appointment=current,
        changed_by_user_id=changed_by_user_id,
        reason="appointment_rescheduled",
    )
    transfer_campaign_booking_conversion(
        db,
        workspace_id=workspace.id,
        from_appointment_id=current.id,
        to_appointment_id=replacement.id,
    )

    add_appointment_history(
        db,
        appointment=current,
        changed_by_user_id=changed_by_user_id,
        from_status=old_status,
        to_status="rescheduled",
        reason=reason or "appointment_rescheduled",
        metadata={
            "replacement_appointment_id": str(replacement.id),
            "old_start_at": old_start.isoformat(),
            "old_end_at": old_end.isoformat(),
            "new_start_at": replacement.start_at.isoformat(),
            "new_end_at": replacement.end_at.isoformat(),
            "old_service_id": str(current.service_id),
            "new_service_id": str(replacement.service_id),
            "service_changed": service_changed,
            "old_laser_device_key": current.laser_device_key,
            "new_laser_device_key": replacement.laser_device_key,
            "laser_device_changed": device_changed,
        },
    )
    add_appointment_history(
        db,
        appointment=replacement,
        changed_by_user_id=changed_by_user_id,
        from_status=None,
        to_status=old_status,
        reason="rescheduled_from_previous_appointment",
        metadata={"previous_appointment_id": str(current.id)},
    )
    cancel_pending_appointment_jobs(
        db,
        appointment=current,
        reason="appointment_rescheduled",
        now=now,
    )
    record_activity_event(
        db,
        workspace_id=current.workspace_id,
        actor_type=actor_type,
        actor_user_id=changed_by_user_id,
        action="appointment.rescheduled",
        entity_type="appointment",
        entity_id=current.id,
        summary="Appointment rescheduled",
        metadata={
            "replacement_appointment_id": replacement.id,
            "from_status": old_status,
            "old_start_at": old_start,
            "new_start_at": replacement.start_at,
            "old_service_id": current.service_id,
            "new_service_id": replacement.service_id,
            "service_changed": service_changed,
            "old_laser_device_key": current.laser_device_key,
            "new_laser_device_key": replacement.laser_device_key,
            "laser_device_changed": device_changed,
        },
    )
    db.flush()
    return replacement, current


def update_operational_status_operation(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    target_status: str,
    changed_by_user_id: UUID | None,
    patient_id: UUID | None = None,
    reason: str | None = None,
    actor_type: ActivityActorType = "staff",
    now: datetime | None = None,
) -> Appointment:
    appointment = _locked_appointment(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
        patient_id=patient_id,
    )
    allowed_transitions: dict[str, set[str]] = {
        "confirmed": {"completed", "no_show"},
        "pending": {"completed", "no_show"},
        # Backward compatibility only: legacy rows can be closed, but no new
        # checked-in/in-progress transitions are accepted anywhere.
        "checked_in": {"completed", "no_show"},
        "in_progress": {"completed", "no_show"},
    }
    if target_status not in allowed_transitions.get(appointment.status, set()):
        raise AppointmentOperationError(
            f"Cannot change appointment status from '{appointment.status}' to '{target_status}'."
        )

    now = (now or datetime.now(UTC)).astimezone(UTC)
    if target_status in {"completed", "no_show"} and now < appointment.start_at:
        raise AppointmentOperationError(
            "An appointment cannot be completed or marked no-show before its start time."
        )

    if target_status == "completed":
        try:
            require_pulse_settlement_before_completion(db, appointment=appointment)
        except PulseBillingError as exc:
            raise AppointmentOperationError(str(exc)) from exc

    old_status = appointment.status
    appointment.status = target_status
    if target_status == "completed":
        appointment.completed_at = now
    elif target_status == "no_show":
        appointment.no_show_at = now

    default_reasons = {
        "completed": "appointment_completed",
        "no_show": "patient_no_show",
    }
    add_appointment_history(
        db,
        appointment=appointment,
        changed_by_user_id=changed_by_user_id,
        from_status=old_status,
        to_status=target_status,
        reason=(reason.strip() if reason and reason.strip() else default_reasons.get(target_status)),
    )
    if target_status in {"completed", "no_show"}:
        cancel_pending_appointment_jobs(
            db,
            appointment=appointment,
            reason=f"appointment_{target_status}",
            now=now,
        )
        try:
            if target_status == "completed":
                # A released historical PackageUsage may remain after the package
                # itself was cancelled/refunded. Only currently package-backed
                # appointments are allowed to consume entitlement.
                consume_visit_package_usages(
                    db,
                    appointment=appointment,
                    used_at=now,
                    actor_type=actor_type,
                    actor_user_id=changed_by_user_id,
                )
            else:
                release_visit_package_usages(
                    db,
                    appointment=appointment,
                    actor_type=actor_type,
                    actor_user_id=changed_by_user_id,
                    reason="patient_no_show",
                )
                release_appointment_pulse_usage(
                    db,
                    appointment=appointment,
                    changed_by_user_id=changed_by_user_id,
                    reason="patient_no_show",
                )
        except PackageOperationError as exc:
            raise AppointmentOperationError(str(exc)) from exc
    record_activity_event(
        db,
        workspace_id=appointment.workspace_id,
        actor_type=actor_type,
        actor_user_id=changed_by_user_id,
        action=f"appointment.{target_status}",
        entity_type="appointment",
        entity_id=appointment.id,
        summary=(
            "Appointment completed" if target_status == "completed" else "Appointment marked no-show"
        ),
        metadata={"from_status": old_status, "to_status": target_status},
    )
    db.flush()
    return appointment