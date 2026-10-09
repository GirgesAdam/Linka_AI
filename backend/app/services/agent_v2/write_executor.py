from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    require_tia_patient_fields_writable,
    require_tia_workspace_domain_write,
)
from app.models.patient import Patient
from app.models.workspace import Workspace
from app.services.activity import record_activity_event
from app.services.agent_v2.grouped_visit_operations import (
    cancel_visit_group_operation,
    reschedule_visit_group_operation,
)
from app.services.agent_v2.package_booking_policy import (
    BookingPackagePolicyError,
    resolve_booking_package,
)
from app.services.agent_v2.planner import _EXPLICIT_TIME_AUTHORITY_PARAM, PlanStep
from app.services.appointment_creation import create_appointment_operation
from app.services.appointment_operations import (
    AppointmentCancellationOverrideRequired,
    AppointmentOperationError,
    AppointmentOperationForbidden,
    AppointmentServiceChangeRequiresHuman,
    cancel_appointment_operation,
    confirm_appointment_operation,
    reschedule_appointment_operation,
)
from app.services.crm_tasks import CRMTaskError, create_crm_task
from app.services.package_offers import PackageOfferError, purchase_package_offer
from app.services.patient_packages import PackageOperationError
from app.services.pulse_billing import (
    PulseBillingError,
    purchase_pulse_pack_offer,
)


class WriteExecutionError(ValueError):
    pass


def _required(parameters: dict[str, object], key: str) -> object:
    value = parameters.get(key)
    if value is None or value == "":
        raise WriteExecutionError(f"Verified write is missing {key}.")
    return value


def _uuid(parameters: dict[str, object], key: str) -> UUID:
    value = _required(parameters, key)
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise WriteExecutionError(f"Verified write has an invalid {key}.") from exc


def _optional_uuid(parameters: dict[str, object], key: str) -> UUID | None:
    value = parameters.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise WriteExecutionError(f"Verified write has an invalid {key}.") from exc


def _uuid_sequence(
    parameters: dict[str, object],
    key: str,
) -> tuple[UUID, ...]:
    raw = parameters.get(key)
    if not isinstance(raw, (list, tuple)):
        return ()
    values: list[UUID] = []
    for item in raw:
        try:
            values.append(item if isinstance(item, UUID) else UUID(str(item)))
        except (TypeError, ValueError) as exc:
            raise WriteExecutionError(
                f"Verified write has an invalid {key}."
            ) from exc
    return tuple(values)


def _datetime(parameters: dict[str, object], key: str) -> datetime:
    value = _required(parameters, key)
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise WriteExecutionError(f"Verified write has an invalid {key}.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WriteExecutionError(f"Verified write {key} must include a timezone offset.")
    return parsed


def _explicit_time_mismatch(
    parameters: dict[str, object],
    *,
    start_at: datetime,
    timezone_name: str,
) -> bool:
    expected = str(parameters.get(_EXPLICIT_TIME_AUTHORITY_PARAM) or "").strip()[:5]
    if not expected:
        return False
    try:
        clinic_start = start_at.astimezone(ZoneInfo(timezone_name))
    except (ValueError, TypeError):
        return True
    return clinic_start.strftime("%H:%M") != expected


def _failure(
    *,
    write_kind: str,
    code: str,
    detail: str,
    requires_human: bool = False,
) -> dict[str, object]:
    return {
        "ok": False,
        "write_kind": write_kind,
        "error_code": code,
        "detail": detail,
        "requires_human": requires_human,
    }


def execute_write_ready_step(
    db: Session,
    *,
    workspace: Workspace,
    patient: Patient,
    step: PlanStep,
    idempotency_key: str | None = None,
    commit: bool = True,
) -> dict[str, object]:
    """Execute one already-verified V2 write. Raw customer text is intentionally absent.

    Isolated callers retain the historical commit-owning behavior. Live turns pass
    ``commit=False`` so the write runs inside a savepoint while the caller owns the
    outer transaction containing state and outbound persistence.
    """
    intent = step.write_intent
    if step.disposition != "write_ready" or intent is None or not intent.authorized:
        return _failure(
            write_kind=intent.kind if intent is not None else "unknown",
            code="write_not_ready",
            detail="The planned write is not authorized and verified for execution.",
        )

    parameters = dict(intent.parameters)
    try:
        write_scope = nullcontext() if commit else db.begin_nested()
        with write_scope:
            if intent.kind in {
                "booking",
                "confirm_appointment",
                "cancel_appointment",
                "reschedule",
            }:
                require_tia_workspace_domain_write(
                    db,
                    workspace_id=workspace.id,
                    domain="appointments",
                )

            if intent.kind == "booking":
                if patient.status == "blocked":
                    return _failure(
                        write_kind=intent.kind,
                        code="patient_blocked",
                        detail="Blocked patients cannot receive new appointments.",
                    )
                service_id = _uuid(parameters, "service_id")
                start_at = _datetime(parameters, "start_at")
                if _explicit_time_mismatch(
                    parameters, start_at=start_at, timezone_name=getattr(workspace, "timezone", "Africa/Cairo")
                ):
                    return _failure(
                        write_kind=intent.kind,
                        code="explicit_time_mismatch",
                        detail="Verified appointment time does not match the customer's explicit HH:MM request.",
                    )
                device_key = (
                    str(parameters["device_key"]) if parameters.get("device_key") else None
                )
                package_usage = str(parameters.get("package_usage") or "unspecified")
                requested_package_id = _optional_uuid(parameters, "package_id")
                package_resolution = resolve_booking_package(
                    db,
                    workspace=workspace,
                    patient=patient,
                    service_id=service_id,
                    start_at=start_at,
                    device_key=device_key,
                    package_usage=package_usage,
                    requested_package_id=requested_package_id,
                )
                appointment = create_appointment_operation(
                    db,
                    workspace=workspace,
                    patient_id=patient.id,
                    branch_id=_uuid(parameters, "branch_id"),
                    doctor_id=_uuid(parameters, "doctor_id"),
                    service_id=service_id,
                    requested_start_at=start_at,
                    created_by_user_id=None,
                    patient_package_id=package_resolution.package_id,
                    # Hard Agent boundary: checkout/Pulse billing is always Reception-owned.
                    # Ignore any stale/legacy pulse_usage parameter that may still exist in an
                    # old persisted state or option snapshot.
                    use_pulse_balance=False,
                    visit_group_id=_optional_uuid(parameters, "visit_group_id"),
                    source="ai",
                    laser_device_key=device_key,
                    idempotency_key=idempotency_key,
                    actor_type="ai",
                )
                result: dict[str, object] = {
                    "ok": True,
                    "write_kind": intent.kind,
                    "appointment_id": str(appointment.id),
                    "status": appointment.status,
                }
                if package_resolution.package_used:
                    result["package_used"] = True
                    if package_resolution.package_name:
                        result["package_name"] = package_resolution.package_name
            elif intent.kind == "confirm_appointment":
                appointment = confirm_appointment_operation(
                    db,
                    workspace_id=workspace.id,
                    appointment_id=_uuid(parameters, "appointment_id"),
                    changed_by_user_id=None,
                    patient_id=patient.id,
                    actor_type="ai",
                )
                result = {
                    "ok": True,
                    "write_kind": intent.kind,
                    "appointment_id": str(appointment.id),
                    "status": appointment.status,
                }

            elif intent.kind == "cancel_appointment":
                appointment_ids = _uuid_sequence(parameters, "appointment_ids")
                visit_group_id = _optional_uuid(parameters, "visit_group_id")
                if len(appointment_ids) > 1 and visit_group_id is not None:
                    appointments = cancel_visit_group_operation(
                        db,
                        workspace=workspace,
                        patient_id=patient.id,
                        visit_group_id=visit_group_id,
                        appointment_ids=appointment_ids,
                    )
                    result = {
                        "ok": True,
                        "write_kind": intent.kind,
                        "appointment_ids": [str(item.id) for item in appointments],
                        "visit_group_id": str(visit_group_id),
                        "status": "cancelled",
                    }
                else:
                    appointment = cancel_appointment_operation(
                        db,
                        workspace=workspace,
                        appointment_id=_uuid(parameters, "appointment_id"),
                        changed_by_user_id=None,
                        patient_id=patient.id,
                        reason="customer_requested_cancellation",
                        override_policy=False,
                        actor_is_admin=False,
                        actor_type="ai",
                    )
                    result = {
                        "ok": True,
                        "write_kind": intent.kind,
                        "appointment_id": str(appointment.id),
                        "status": appointment.status,
                    }

            elif intent.kind == "reschedule":
                appointment_ids = _uuid_sequence(parameters, "appointment_ids")
                visit_group_id = _optional_uuid(parameters, "visit_group_id")
                raw_components = parameters.get("reschedule_components")
                components = (
                    [dict(item) for item in raw_components if isinstance(item, dict)]
                    if isinstance(raw_components, list)
                    else []
                )
                if (
                    len(appointment_ids) > 1
                    and visit_group_id is not None
                    and components
                ):
                    moved = reschedule_visit_group_operation(
                        db,
                        workspace=workspace,
                        patient_id=patient.id,
                        visit_group_id=visit_group_id,
                        appointment_ids=appointment_ids,
                        components=components,
                        idempotency_key=idempotency_key,
                    )
                    result = {
                        "ok": True,
                        "write_kind": intent.kind,
                        "appointment_ids": [
                            str(replacement.id) for replacement, _ in moved
                        ],
                        "previous_appointment_ids": [
                            str(previous.id) for _, previous in moved
                        ],
                        "visit_group_id": str(visit_group_id),
                        "status": moved[0][0].status if moved else "rescheduled",
                    }
                else:
                    requested_start_at = _datetime(parameters, "start_at")
                    if _explicit_time_mismatch(
                        parameters,
                        start_at=requested_start_at,
                        timezone_name=getattr(workspace, "timezone", "Africa/Cairo"),
                    ):
                        return _failure(
                            write_kind=intent.kind,
                            code="explicit_time_mismatch",
                            detail="Verified reschedule time does not match the customer's explicit HH:MM request.",
                        )
                    replacement, previous = reschedule_appointment_operation(
                        db,
                        workspace=workspace,
                        appointment_id=_uuid(parameters, "appointment_id"),
                        requested_start_at=requested_start_at,
                        changed_by_user_id=None,
                        branch_id=_uuid(parameters, "branch_id"),
                        doctor_id=_uuid(parameters, "doctor_id"),
                        service_id=_uuid(parameters, "service_id"),
                        laser_device_key=(
                            str(parameters["device_key"])
                            if parameters.get("device_key")
                            else None
                        ),
                        patient_id=patient.id,
                        idempotency_key=idempotency_key,
                        actor_type="ai",
                    )
                    result = {
                        "ok": True,
                        "write_kind": intent.kind,
                        "appointment_id": str(replacement.id),
                        "previous_appointment_id": str(previous.id),
                        "status": replacement.status,
                    }
            elif intent.kind == "buy_package":
                package = purchase_package_offer(
                    db,
                    workspace_id=workspace.id,
                    patient_id=patient.id,
                    offer_id=_uuid(parameters, "package_offer_id"),
                    amount_paid_minor=0,
                    payment_method="unknown",
                    created_by_user_id=None,
                    idempotency_key=idempotency_key,
                    actor_type="ai",
                )
                result = {
                    "ok": True,
                    "write_kind": intent.kind,
                    "patient_package_id": str(package.id),
                    "status": package.status,
                    "amount_paid_minor": 0,
                }
            elif intent.kind == "buy_pulse_pack":
                pack = purchase_pulse_pack_offer(
                    db,
                    workspace_id=workspace.id,
                    patient_id=patient.id,
                    offer_id=_uuid(parameters, "pulse_pack_offer_id"),
                    amount_paid_minor=0,
                    payment_method="unknown",
                    created_by_user_id=None,
                    idempotency_key=idempotency_key,
                    actor_type="ai",
                )
                result = {
                    "ok": True,
                    "write_kind": intent.kind,
                    "patient_pulse_pack_id": str(pack.id),
                    "status": pack.status,
                    "amount_paid_minor": 0,
                    "sale_price_minor": int(pack.sale_price_minor),
                    "currency": pack.currency,
                }
            elif intent.kind == "follow_up":
                due_at = _datetime(parameters, "follow_up_at_local")
                task = create_crm_task(
                    db,
                    workspace_id=workspace.id,
                    patient_id=patient.id,
                    title="Customer follow-up",
                    due_at=due_at,
                    task_type="follow_up",
                    priority="normal",
                    created_by_user_id=None,
                    source="ai",
                    execution_mode="ai",
                    dedupe_key=idempotency_key,
                    commit=False,
                )
                result = {
                    "ok": True,
                    "write_kind": intent.kind,
                    "crm_task_id": str(task.id),
                    "status": task.status,
                    "due_at": task.due_at.isoformat(),
                }
            elif intent.kind == "marketing_update":
                require_tia_patient_fields_writable(
                    db,
                    workspace_id=workspace.id,
                    patient_id=patient.id,
                    fields={"marketing_consent"},
                )
                consent = bool(_required(parameters, "marketing_consent"))
                patient.marketing_consent = consent
                patient.marketing_consent_at = datetime.now(UTC) if consent else None
                record_activity_event(
                    db,
                    workspace_id=workspace.id,
                    actor_type="ai",
                    actor_user_id=None,
                    action="patient.marketing_consent_updated",
                    entity_type="patient",
                    entity_id=patient.id,
                    summary="Patient marketing consent updated",
                    metadata={"marketing_consent": consent},
                )
                db.flush()
                result = {
                    "ok": True,
                    "write_kind": intent.kind,
                    "patient_id": str(patient.id),
                    "marketing_consent": consent,
                }
            else:
                return _failure(
                    write_kind=intent.kind,
                    code="unsupported_write_kind",
                    detail=f"V2 real-write execution is not enabled for {intent.kind}.",
                )

        if commit:
            db.commit()
        return result
    except BookingPackagePolicyError as exc:
        if commit:
            db.rollback()
        return _failure(
            write_kind=intent.kind,
            code="package_unavailable",
            detail=str(exc),
        )
    except ClinicIntegrationAuthorityError as exc:
        if commit:
            db.rollback()
        return _failure(
            write_kind=intent.kind,
            code="authority_conflict",
            detail=str(exc),
            requires_human=True,
        )
    except (
        AppointmentCancellationOverrideRequired,
        AppointmentOperationForbidden,
        AppointmentServiceChangeRequiresHuman,
    ) as exc:
        if commit:
            db.rollback()
        return _failure(
            write_kind=intent.kind,
            code="staff_review_required",
            detail=str(exc),
            requires_human=True,
        )
    except (
        AppointmentOperationError,
        CRMTaskError,
        PackageOfferError,
        PackageOperationError,
        PulseBillingError,
        WriteExecutionError,
        IntegrityError,
    ) as exc:
        if commit:
            db.rollback()
        return _failure(
            write_kind=intent.kind,
            code="write_failed",
            detail=str(exc),
        )
