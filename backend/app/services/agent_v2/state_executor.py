from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.agents.v2.turn_contract import ClockSource, DateConstraint, TimeConstraint, TurnOperation
from app.services.agent_v2.multi_service_booking import (
    common_laser_device_keys,
    laser_service_ids,
    select_main_service_id,
)
from app.services.agent_v2.planner import PlanStep
from app.services.agent_v2.read_executor import ReadExecutionBundle
from app.services.agent_v2.state import (
    ActiveTaskState,
    BookingTaskState,
    CustomerConstraints,
    OptionChoice,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_rules import (
    TIME_CORRECTION_AUTHORITY_FACT,
    apply_booking_branch_change,
    apply_booking_date_change,
    apply_booking_device_change,
    apply_booking_doctor_change,
    apply_booking_package_usage_change,
    apply_booking_requested_services_change,
    apply_booking_service_change,
    apply_booking_time_change,
    apply_reschedule_branch_change,
    apply_reschedule_date_change,
    apply_reschedule_device_change,
    apply_reschedule_doctor_change,
    apply_reschedule_service_change,
    apply_reschedule_time_change,
    attach_option_snapshot,
    bind_verified_booking_package,
    invalidate_booking_package_selection,
    invalidate_booking_slot_selection,
    preserve_task_for_side_read,
    record_booking_commercial_basis_presented,
    record_booking_selected_slot,
    restore_booking_slot_snapshot,
)

_OPTION_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class StateTransition:
    active_task: ActiveTaskState | None
    changed: bool
    reason: str


def _date_constraint(value: object) -> DateConstraint | None:
    if value is None:
        return None
    if isinstance(value, DateConstraint):
        return value
    if isinstance(value, dict):
        return DateConstraint.model_validate(value)
    return None


def _time_constraint(value: object) -> TimeConstraint | None:
    if value is None:
        return None
    if isinstance(value, TimeConstraint):
        return value
    if isinstance(value, dict):
        return TimeConstraint.model_validate(value)
    return None


def _state_time_constraint(
    value: object,
    *,
    semantic: TimeConstraint | None,
) -> TimeConstraint | None:
    """Carry only unresolved 10/11 tie-break semantics into durable task state."""
    constraint = _time_constraint(value if value is not None else semantic)
    if constraint is None or semantic is None:
        return constraint
    if (
        constraint.start_time_ambiguity != "twelve_hour"
        or semantic.start_time_ambiguity != "twelve_hour"
        or constraint.start_time != semantic.start_time
        or semantic.spoken_period_hint not in {"am", "pm"}
    ):
        return constraint
    try:
        hour = int(str(constraint.start_time).split(":", 1)[0])
    except (TypeError, ValueError):
        return constraint
    if hour not in {10, 11}:
        return constraint
    return constraint.model_copy(
        update={"spoken_period_hint": semantic.spoken_period_hint}
    )


def _clock_source(value: object, *, fallback: ClockSource = "unspecified") -> ClockSource:
    if value == "explicit_period":  # Legacy persisted input only.
        return "bare_spoken_12h"
    if value in {"literal_24h", "bare_spoken_12h", "inherited", "unspecified"}:
        return value  # type: ignore[return-value]
    return fallback


def _time_business_signature(value: TimeConstraint | None) -> tuple[object, ...] | None:
    if value is None:
        return None
    return (
        value.mode,
        value.start_time,
        value.end_time,
        value.start_time_ambiguity,
        value.end_time_ambiguity,
    )


def _time_fact_has_backend_authority(facts: dict[str, Any]) -> bool:
    return bool(
        facts.get("verified_availability_reference") is True
        or facts.get("bare_clock_resolution_status") == "resolved"
        or facts.get("_backend_time_state_action") == "clear_past"
    )


def _authoritative_time_update(
    existing: TimeConstraint | None,
    *,
    existing_clock_source: ClockSource,
    facts: dict[str, Any],
    operation: TurnOperation,
) -> tuple[bool, TimeConstraint | None, ClockSource]:
    """Classify time mutation authority instead of trusting fact presence.

    Progression facts may echo durable constraints after normal serialization,
    which intentionally excludes ``spoken_period_hint``.  Such echoes must not
    overwrite richer durable state.  Explicit semantic time, verified backend
    selections/resolution, and deterministic past-time clears remain authoritative.
    """
    if "time" not in facts and operation.entities.time is None:
        return False, existing, existing_clock_source

    incoming = _state_time_constraint(
        facts.get("time") if "time" in facts else operation.entities.time,
        semantic=operation.entities.time,
    )
    incoming_source = _clock_source(
        facts.get("time_clock_source"),
        fallback=operation.clock_source,
    )

    explicit_semantic = operation.entities.time is not None
    backend_authoritative = _time_fact_has_backend_authority(facts)
    if explicit_semantic or backend_authoritative:
        return True, incoming, incoming_source

    # Defensive equivalence guard: an inherited echo can be byte-different only
    # because excluded semantic metadata was stripped.  Keep the durable object.
    if _time_business_signature(incoming) == _time_business_signature(existing):
        return False, existing, existing_clock_source

    # Unknown, unproven time changes fail closed.  A materially different fact is
    # not mutation authority merely because a planner/backend step carried `time`.
    return False, existing, existing_clock_source


def _authorization(
    *,
    operation: str,
    turn_id: str,
    now: datetime,
    authorized: bool,
) -> WriteAuthorization:
    return WriteAuthorization(
        operation=operation,
        authorized=authorized,
        source_turn_id=turn_id if authorized else None,
        granted_at=now if authorized else None,
    )


def _promote_booking_authorization_for_explicit_continuation(
    state: BookingTaskState,
    *,
    operation: TurnOperation,
    turn_id: str,
    now: datetime,
) -> BookingTaskState:
    if state.write_authorization.authorized:
        return state
    if (
        operation.type != "book"
        or operation.execution_intent != "execute"
        or operation.active_task_relationship != "continue"
        or set(operation.active_task_explicit_fields) == {"time"}
        or set(operation.active_task_corrected_fields) == {"time"}
    ):
        return state
    return state.model_copy(
        update={
            "write_authorization": _authorization(
                operation="booking",
                turn_id=turn_id,
                now=now,
                authorized=True,
            ),
            "version": state.version + 1,
        }
    )


def _booking_constraints(step: PlanStep, operation: TurnOperation) -> CustomerConstraints:
    facts = step.facts
    return CustomerConstraints(
        service_id=str(facts["service_id"]) if facts.get("service_id") else None,
        doctor_id=str(facts["doctor_id"]) if facts.get("doctor_id") else None,
        branch_id=str(facts["branch_id"]) if facts.get("branch_id") else None,
        device_key=str(facts["device_key"]) if facts.get("device_key") else None,
        date=_date_constraint(facts.get("date") or operation.entities.date),
        time=_state_time_constraint(facts.get("time"), semantic=operation.entities.time),
        time_clock_source=_clock_source(
            facts.get("time_clock_source"),
            fallback=operation.clock_source,
        ),
        package_usage=operation.package_usage,
        pulse_usage="unspecified",
    )


def _new_booking_state(
    *,
    step: PlanStep,
    operation: TurnOperation,
    turn_id: str,
    now: datetime,
) -> BookingTaskState:
    authorized = bool(
        operation.type == "book"
        and operation.execution_intent == "execute"
    ) or bool(
        step.write_intent is not None
        and step.write_intent.kind == "booking"
        and step.write_intent.authorized
    )
    constraints = _booking_constraints(step, operation)
    requested = [
        str(item)
        for item in step.facts.get("requested_service_ids", [])
        if item not in (None, "")
    ] if isinstance(step.facts.get("requested_service_ids"), list) else []
    service_devices = (
        {constraints.service_id: constraints.device_key}
        if requested and constraints.service_id and constraints.device_key
        else {}
    )
    return BookingTaskState(
        write_authorization=_authorization(
            operation="booking",
            turn_id=turn_id,
            now=now,
            authorized=authorized,
        ),
        constraints=constraints,
        requested_service_ids=requested,
        service_device_keys=service_devices,
    )


def _apply_booking_updates(
    state: BookingTaskState,
    *,
    step: PlanStep,
    operation: TurnOperation,
) -> BookingTaskState:
    facts = step.facts
    updated = state
    if "service_id" in facts:
        updated = apply_booking_service_change(
            updated,
            service_id=str(facts["service_id"]) if facts.get("service_id") else None,
        )
    if "doctor_id" in facts:
        updated = apply_booking_doctor_change(
            updated,
            doctor_id=str(facts["doctor_id"]) if facts.get("doctor_id") else None,
        )
    if "branch_id" in facts:
        updated = apply_booking_branch_change(
            updated,
            branch_id=str(facts["branch_id"]) if facts.get("branch_id") else None,
        )
    if "device_key" in facts:
        updated = apply_booking_device_change(
            updated,
            device_key=str(facts["device_key"]) if facts.get("device_key") else None,
        )
    if "date" in facts:
        updated = apply_booking_date_change(updated, date=_date_constraint(facts.get("date")))
    elif operation.entities.date is not None:
        updated = apply_booking_date_change(updated, date=operation.entities.date)
    if facts.get(TIME_CORRECTION_AUTHORITY_FACT) == "conflict":
        updated = apply_booking_time_change(
            updated,
            time=None,
            clock_source="unspecified",
        )
    else:
        apply_time, next_time, next_clock_source = _authoritative_time_update(
            updated.constraints.time,
            existing_clock_source=updated.constraints.time_clock_source,
            facts=facts,
            operation=operation,
        )
        if apply_time:
            updated = apply_booking_time_change(
                updated,
                time=next_time,
                clock_source=next_clock_source,
            )
    if operation.package_usage != "unspecified":
        updated = apply_booking_package_usage_change(
            updated,
            package_usage=operation.package_usage,
        )
    requested = facts.get("requested_service_ids")
    if isinstance(requested, list):
        updated = apply_booking_requested_services_change(
            updated,
            service_ids=[str(item) for item in requested if item not in (None, "")],
        )
    if (
        updated.requested_service_ids
        and updated.constraints.service_id
        and updated.constraints.device_key
    ):
        updated = updated.model_copy(
            update={
                "service_device_keys": {
                    **updated.service_device_keys,
                    updated.constraints.service_id: updated.constraints.device_key,
                }
            }
        )
    return updated


def reconcile_booking_service_set_state(
    state: BookingTaskState,
    *,
    catalog: dict[str, Any],
) -> BookingTaskState:
    """Re-derive booking authority from the current canonical requested service set."""
    if not state.requested_service_ids:
        return state

    current = state
    laser_ids = laser_service_ids(catalog, current.requested_service_ids)
    common_devices = common_laser_device_keys(catalog, current.requested_service_ids)
    selected_device = current.constraints.device_key
    if not laser_ids:
        if selected_device is not None:
            current = apply_booking_device_change(current, device_key=None)
        selected_device = None
        visit_devices: dict[str, str] = {}
    elif selected_device is not None and selected_device not in common_devices:
        replacement_device = common_devices[0] if len(common_devices) == 1 else None
        current = apply_booking_device_change(current, device_key=replacement_device)
        selected_device = replacement_device
        visit_devices = (
            {service_id: replacement_device for service_id in laser_ids}
            if replacement_device is not None
            else {}
        )
    elif selected_device is not None:
        visit_devices = {service_id: selected_device for service_id in laser_ids}
    elif len(common_devices) == 1:
        selected_device = common_devices[0]
        current = apply_booking_device_change(current, device_key=selected_device)
        visit_devices = {service_id: selected_device for service_id in laser_ids}
    else:
        visit_devices = {}

    main_service_id = select_main_service_id(
        catalog,
        current.requested_service_ids,
        device_key=selected_device,
    )
    if current.constraints.service_id != main_service_id:
        current = apply_booking_service_change(current, service_id=main_service_id)
        if selected_device is not None:
            current = apply_booking_device_change(current, device_key=selected_device)

    if current.service_device_keys != visit_devices:
        current = current.model_copy(update={"service_device_keys": visit_devices})
    return current


def _unique_appointment_target(reads: ReadExecutionBundle | None) -> RescheduleTarget | None:
    if reads is None:
        return None
    for result in reads.results:
        if result.kind != "appointments":
            continue
        rows = result.payload.get("appointments")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            continue
        row = rows[0]
        appointment_id = row.get("appointment_id")
        if appointment_id in (None, ""):
            continue
        payment_context = {
            key: row[key]
            for key in (
                "payment_status",
                "amount_paid_minor",
                "payment_method",
                "billing_context",
                "patient_package_id",
                "package_external_id",
            )
            if row.get(key) not in (None, "")
        }
        return RescheduleTarget(
            appointment_id=str(appointment_id),
            service_id=str(row["service_id"]) if row.get("service_id") else None,
            doctor_id=str(row["doctor_id"]) if row.get("doctor_id") else None,
            device_key=(
                str(row["laser_device_key"])
                if row.get("laser_device_key")
                else str(row["device_key"])
                if row.get("device_key")
                else None
            ),
            start_local=str(row["start_local"]) if row.get("start_local") else None,
            payment_context=payment_context,
        )
    return None


def _new_reschedule_state(
    *,
    step: PlanStep,
    operation: TurnOperation,
    reads: ReadExecutionBundle | None,
    turn_id: str,
    now: datetime,
) -> RescheduleTaskState | None:
    target = _unique_appointment_target(reads)
    if target is None:
        return None
    replacement = CustomerConstraints(
        service_id=str(step.facts.get("service_id") or target.service_id)
        if step.facts.get("service_id") or target.service_id
        else None,
        doctor_id=str(step.facts.get("doctor_id") or target.doctor_id)
        if step.facts.get("doctor_id") or target.doctor_id
        else None,
        device_key=str(step.facts.get("device_key") or target.device_key)
        if step.facts.get("device_key") or target.device_key
        else None,
        branch_id=(
            str(step.facts.get("branch_id"))
            if step.facts.get("branch_id") not in (None, "")
            else None
        ),
        date=_date_constraint(step.facts.get("date") or operation.entities.date),
        time=(
            None
            if step.facts.get(TIME_CORRECTION_AUTHORITY_FACT) == "conflict"
            else _state_time_constraint(
                step.facts.get("time"),
                semantic=operation.entities.time,
            )
        ),
        time_clock_source=(
            "unspecified"
            if step.facts.get(TIME_CORRECTION_AUTHORITY_FACT) == "conflict"
            else _clock_source(
                step.facts.get("time_clock_source"),
                fallback=operation.clock_source,
            )
        ),
        package_usage=operation.package_usage,
        pulse_usage="unspecified",
    )
    return RescheduleTaskState(
        write_authorization=_authorization(
            operation="reschedule",
            turn_id=turn_id,
            now=now,
            authorized=True,
        ),
        target=target,
        replacement=replacement,
    )


def _apply_reschedule_updates(
    state: RescheduleTaskState,
    *,
    step: PlanStep,
    operation: TurnOperation,
) -> RescheduleTaskState:
    facts = step.facts
    updated = state
    if "service_id" in facts:
        updated = apply_reschedule_service_change(
            updated,
            service_id=str(facts["service_id"]) if facts.get("service_id") else None,
        )
    if "doctor_id" in facts:
        updated = apply_reschedule_doctor_change(
            updated,
            doctor_id=str(facts["doctor_id"]) if facts.get("doctor_id") else None,
        )
    if "device_key" in facts:
        updated = apply_reschedule_device_change(
            updated,
            device_key=str(facts["device_key"]) if facts.get("device_key") else None,
        )
    if "branch_id" in facts:
        updated = apply_reschedule_branch_change(
            updated,
            branch_id=str(facts["branch_id"]) if facts.get("branch_id") else None,
        )
    if "date" in facts:
        updated = apply_reschedule_date_change(updated, date=_date_constraint(facts.get("date")))
    elif operation.entities.date is not None:
        updated = apply_reschedule_date_change(updated, date=operation.entities.date)
    if facts.get(TIME_CORRECTION_AUTHORITY_FACT) == "conflict":
        updated = apply_reschedule_time_change(
            updated,
            time=None,
            clock_source="unspecified",
        )
    else:
        apply_time, next_time, next_clock_source = _authoritative_time_update(
            updated.replacement.time,
            existing_clock_source=updated.replacement.time_clock_source,
            facts=facts,
            operation=operation,
        )
        if apply_time:
            updated = apply_reschedule_time_change(
                updated,
                time=next_time,
                clock_source=next_clock_source,
            )
    return updated


def _availability_slots(reads: ReadExecutionBundle | None) -> list[dict[str, object]]:
    if reads is None:
        return []
    for result in reads.results:
        if result.kind != "availability":
            continue
        rows = result.payload.get("slots")
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def _availability_alternative_slots(
    reads: ReadExecutionBundle | None,
) -> list[dict[str, object]]:
    if reads is None:
        return []
    for result in reads.results:
        if result.kind != "availability":
            continue
        rows = result.payload.get("nearest_alternative_slots")
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def _slot_payload(row: dict[str, object]) -> dict[str, object]:
    start_local = str(row.get("start_local") or "")
    start_time = str(row.get("start_time_24h") or "")[:5]
    if not start_time and len(start_local) >= 16:
        start_time = start_local[11:16]
    payload: dict[str, object] = {}
    mappings = (
        ("branch_id", "branch_id"),
        ("service_id", "service_id"),
        ("doctor_id", "doctor_id"),
        ("laser_device_key", "device_key"),
        ("device_key", "device_key"),
        ("start_at", "start_at"),
        ("start_local", "start_local"),
        ("doctor_name", "doctor_name"),
        ("laser_device_name", "laser_device_name"),
        ("service_name", "service_name"),
    )
    for source, target in mappings:
        if row.get(source) not in (None, "") and target not in payload:
            payload[target] = row[source]
    if "start_at" not in payload and start_local:
        payload["start_at"] = start_local
    if start_time:
        payload["start_time_24h"] = start_time
    return payload


def _booking_slot_payload(
    row: dict[str, object],
    *,
    state: ActiveTaskState,
    step: PlanStep,
) -> dict[str, object]:
    payload = _slot_payload(row)
    if isinstance(state, BookingTaskState):
        payload["package_usage"] = state.constraints.package_usage
        if step.facts.get("package_id") not in (None, ""):
            payload["package_id"] = step.facts["package_id"]
    return payload


def _slot_label(payload: dict[str, object]) -> str:
    parts = [
        str(payload.get("start_time_24h") or ""),
        str(payload.get("doctor_name") or ""),
        str(payload.get("laser_device_name") or ""),
    ]
    return " · ".join(part for part in parts if part) or "موعد متاح"


def _attach_availability_snapshot(
    state: ActiveTaskState,
    *,
    step: PlanStep,
    reads: ReadExecutionBundle | None,
    now: datetime,
    turn_id: str,
) -> ActiveTaskState:
    slots = _availability_slots(reads)
    if (
        not slots
        and step.facts.get("verified_booking_slot_selection") is True
        and step.response_goal == "requested_time_unavailable"
    ):
        slots = _availability_alternative_slots(reads)
    if not slots or step.disposition == "write_ready" or step.response_goal == "booking_proposal":
        return state
    if step.response_goal in {"answer_price", "package_information"} and len(slots) == 1:
        # Exact/unique availability verified alongside commercial truth does not
        # need a customer choice snapshot; keep the task resumable for confirmation.
        return state
    purpose = "booking_slot" if state.task_type == "booking" else "reschedule_slot"
    snapshot_id = f"{turn_id}:{step.operation_index}:{state.version + 1}"
    choices = [
        OptionChoice(
            ref=f"slot-{index}",
            label=_slot_label(payload),
            payload=payload,
        )
        for index, payload in enumerate(
            (_booking_slot_payload(row, state=state, step=step) for row in slots),
            start=1,
        )
    ]
    updated = attach_option_snapshot(
        state,
        snapshot_id=snapshot_id,
        purpose=purpose,
        choices=choices,
        created_at=now,
        expires_at=now + _OPTION_TTL,
    )
    if isinstance(updated, BookingTaskState):
        return updated
    return updated.model_copy(
        update={
            "derived": updated.derived.model_copy(
                update={"availability_snapshot_id": snapshot_id}
            )
        }
    )


def _preserve_verified_window_snapshot(
    before: BookingTaskState,
    current: BookingTaskState,
    *,
    step: PlanStep,
    now: datetime,
) -> BookingTaskState:
    if step.facts.get("verified_availability_window_narrowing") is not True:
        return current
    snapshot = before.option_snapshot
    if snapshot is None or not snapshot.is_current(task_version=before.version, now=now):
        return current
    doctor_id = step.facts.get("doctor_id")
    device_key = step.facts.get("device_key")
    if doctor_id in (None, "") and device_key in (None, ""):
        return current
    choices = []
    for choice in snapshot.options:
        payload = choice.payload
        if doctor_id not in (None, "") and str(payload.get("doctor_id") or "") != str(doctor_id):
            continue
        payload_device = payload.get("device_key") or payload.get("laser_device_key")
        if device_key not in (None, "") and str(payload_device or "") != str(device_key):
            continue
        choices.append(choice)
    if not choices:
        return current
    snapshot_id = f"{snapshot.snapshot_id}:narrow:{current.version}"
    narrowed = snapshot.model_copy(
        update={
            "snapshot_id": snapshot_id,
            "task_version": current.version,
            "options": choices,
        }
    )
    return restore_booking_slot_snapshot(
        current,
        snapshot=narrowed,
    )

def _mark_selected_or_ready(state: ActiveTaskState, step: PlanStep) -> ActiveTaskState:
    verified_reference_selection = (
        step.facts.get("verified_availability_reference") is True
        and step.facts.get("selected_option_ref") not in (None, "")
    )
    if (
        step.state_action != "select_active"
        and step.disposition != "write_ready"
        and not verified_reference_selection
    ):
        return state
    selected_ref = step.facts.get("selected_option_ref")
    if isinstance(state, BookingTaskState):
        if step.facts.get("choice_purpose") == "package":
            selected_option = step.facts.get("selected_option")
            payload = selected_option.get("payload") if isinstance(selected_option, dict) else None
            if not isinstance(payload, dict):
                return state
            package_id = payload.get("package_id")
            service_id = payload.get("service_id")
            device_key = payload.get("device_key")
            if package_id in (None, ""):
                return state
            if (
                state.constraints.service_id is not None
                and str(service_id or "") != str(state.constraints.service_id)
            ):
                return state
            if (
                state.constraints.device_key is not None
                and str(device_key or "") != str(state.constraints.device_key)
            ):
                return state
            return bind_verified_booking_package(
                state,
                package_id=str(package_id),
                device_key=str(device_key) if device_key not in (None, "") else None,
            )
        if selected_ref in (None, ""):
            return state
        if (
            (
                step.facts.get("verified_booking_slot_selection") is True
                or step.facts.get("booking_proposal_required") is True
            )
            and step.response_goal != "booking_proposal"
        ):
            return state
        return record_booking_selected_slot(
            state,
            selected_slot_ref=str(selected_ref),
        )
    status = "ready" if step.disposition == "write_ready" else state.status
    derived = state.derived.model_copy(
        update={
            "selected_slot_ref": str(selected_ref) if selected_ref else state.derived.selected_slot_ref
        }
    )
    return state.model_copy(update={"status": status, "derived": derived})


def apply_step_state(
    active_task: ActiveTaskState | None,
    *,
    step: PlanStep,
    operation: TurnOperation,
    reads: ReadExecutionBundle | None,
    now: datetime,
    turn_id: str,
    catalog: dict[str, Any] | None = None,
) -> StateTransition:
    """Apply one deterministic V2 workflow transition without persistence or writes."""
    before = active_task

    if step.state_action == "cancel_active":
        return StateTransition(active_task=None, changed=before is not None, reason="cancel_active")

    current = active_task
    if step.state_action == "replace_active":
        if operation.type == "book":
            current = _new_booking_state(
                step=step,
                operation=operation,
                turn_id=turn_id,
                now=now,
            )
        elif operation.type == "reschedule":
            current = _new_reschedule_state(
                step=step,
                operation=operation,
                reads=reads,
                turn_id=turn_id,
                now=now,
            )
        else:
            raise RuntimeError("replace_active requires a book or reschedule operation.")
    elif step.state_action == "start_booking":
        grouped_service_ids = (
            {
                component.service_id
                for component in current.grouped.components
                if component.service_id is not None
            }
            if isinstance(current, BookingTaskState) and current.grouped is not None
            else set()
        )
        step_service_id = str(step.facts.get("service_id") or "")
        if not step_service_id or step_service_id not in grouped_service_ids:
            current = _new_booking_state(step=step, operation=operation, turn_id=turn_id, now=now)
    elif step.state_action == "start_reschedule":
        current = _new_reschedule_state(
            step=step,
            operation=operation,
            reads=reads,
            turn_id=turn_id,
            now=now,
        )
        if current is None:
            return StateTransition(active_task=before, changed=False, reason="reschedule_target_unverified")
    elif step.state_action == "select_active":
        if isinstance(current, BookingTaskState):
            current = _apply_booking_updates(current, step=step, operation=operation)
    elif step.state_action == "update_active":
        if isinstance(current, BookingTaskState):
            before_booking = current
            current = _apply_booking_updates(current, step=step, operation=operation)
            current = _promote_booking_authorization_for_explicit_continuation(
                current,
                operation=operation,
                turn_id=turn_id,
                now=now,
            )
            if operation.type == "book" and operation.execution_intent == "informational":
                current = current.model_copy(
                    update={
                        "write_authorization": _authorization(
                            operation="booking",
                            turn_id=turn_id,
                            now=now,
                            authorized=False,
                        )
                    }
                )
            current = _preserve_verified_window_snapshot(
                before_booking,
                current,
                step=step,
                now=now,
            )
        elif isinstance(current, RescheduleTaskState):
            current = _apply_reschedule_updates(current, step=step, operation=operation)
        else:
            return StateTransition(active_task=None, changed=False, reason="no_active_task")
    elif step.state_action == "none":
        if current is None:
            return StateTransition(active_task=None, changed=False, reason="no_state_action")
        return StateTransition(
            active_task=preserve_task_for_side_read(current),
            changed=False,
            reason="side_read_preserved",
        )

    if current is None:
        return StateTransition(active_task=None, changed=before is not None, reason="state_cleared")

    if isinstance(current, BookingTaskState) and catalog is not None:
        current = reconcile_booking_service_set_state(current, catalog=catalog)

    commercial_read_verified = False
    if reads is not None and isinstance(current, BookingTaskState):
        if step.response_goal == "answer_price":
            commercial_read_verified = any(
                result.ok
                and (
                    result.kind == "service_catalog"
                    or (
                        result.kind == "service_catalog_set"
                        and isinstance(result.payload.get("variants"), list)
                        and bool(result.payload["variants"])
                    )
                )
                for result in reads.results
            )
        elif step.response_goal == "package_information":
            package_result = next(
                (
                    result
                    for result in reads.results
                    if result.kind == "customer_packages" and result.ok
                ),
                None,
            )
            rows = package_result.payload.get("packages") if package_result is not None else None
            commercial_read_verified = isinstance(rows, list) and any(
                isinstance(row, dict)
                and row.get("effective_status") == "active"
                and isinstance(row.get("sessions_remaining"), int)
                and row["sessions_remaining"] > 0
                for row in rows
            )

    if (
        isinstance(current, BookingTaskState)
        and current.constraints.package_usage == "use_existing"
        and step.facts.get("booking_commercial_basis_step") is True
        and reads is not None
    ):
        package_result = next(
            (
                result
                for result in reads.results
                if result.kind == "customer_packages" and result.ok
            ),
            None,
        )
        if package_result is not None:
            rows = package_result.payload.get("packages")
            candidates = (
                [
                    dict(row)
                    for row in rows
                    if isinstance(row, dict)
                    and (row.get("id") or row.get("package_id")) not in (None, "")
                ]
                if isinstance(rows, list)
                else []
            )
            if len(candidates) == 1:
                row = candidates[0]
                package_id = str(row.get("id") or row.get("package_id"))
                device_key = row.get("laser_device_key") or row.get("device_key")
                current = bind_verified_booking_package(
                    current,
                    package_id=package_id,
                    device_key=(
                        str(device_key) if device_key not in (None, "") else None
                    ),
                )
            else:
                current = invalidate_booking_package_selection(current)
                if len(candidates) > 1:
                    choices = [
                        OptionChoice(
                            ref=f"package-{index}",
                            label=str(row.get("name") or f"Package {index}"),
                            payload={
                                "package_id": str(row.get("id") or row.get("package_id")),
                                "service_id": str(row.get("service_id")),
                                **(
                                    {
                                        "device_key": str(
                                            row.get("laser_device_key")
                                            or row.get("device_key")
                                        )
                                    }
                                    if (
                                        row.get("laser_device_key")
                                        or row.get("device_key")
                                    )
                                    not in (None, "")
                                    else {}
                                ),
                            },
                        )
                        for index, row in enumerate(candidates, start=1)
                    ]
                    selected = attach_option_snapshot(
                        current,
                        snapshot_id=(
                            f"{turn_id}:{step.operation_index}:package:{current.version + 1}"
                        ),
                        purpose="package",
                        choices=choices,
                        created_at=now,
                        expires_at=now + _OPTION_TTL,
                    )
                    if isinstance(selected, BookingTaskState):
                        current = selected

    if isinstance(current, BookingTaskState) and commercial_read_verified:
        commercial_key = step.facts.get("commercial_basis_key")
        if current.constraints.package_usage == "use_existing":
            commercial_key = (
                f"{current.constraints.service_id}|{current.constraints.device_key or 'none'}|package"
                if current.constraints.service_id is not None
                else None
            )
        elif not isinstance(commercial_key, str) or not commercial_key:
            commercial_key = (
                f"{current.constraints.service_id}|{current.constraints.device_key or 'none'}|standalone"
                if current.constraints.service_id is not None
                else None
            )
        if commercial_key is not None:
            current = record_booking_commercial_basis_presented(
                current,
                commercial_key=commercial_key,
            )

    if (
        isinstance(current, BookingTaskState)
        and step.facts.get("verified_booking_slot_selection") is True
        and step.response_goal != "booking_proposal"
    ):
        current = invalidate_booking_slot_selection(current)

    current = _attach_availability_snapshot(
        current,
        step=step,
        reads=reads,
        now=now,
        turn_id=turn_id,
    )
    current = _mark_selected_or_ready(current, step)
    return StateTransition(
        active_task=current,
        changed=current != before,
        reason=step.state_action,
    )


def finalize_step_after_state_transition(
    step: PlanStep,
    transition: StateTransition,
) -> PlanStep:
    """Encode state mutation truth into the responder-facing step after Python applies it."""
    if step.state_action != "cancel_active":
        return step
    if transition.changed and transition.active_task is None:
        if step.facts.get("canonical_target_non_actionable") is True:
            return step.model_copy(
                update={
                    "response_goal": "clarification",
                    "facts": {
                        **step.facts,
                        "active_task_cancelled": True,
                        "active_task_invalidated": True,
                    },
                }
            )
        return step.model_copy(
            update={
                "response_goal": "active_task_cancelled",
                "facts": {**step.facts, "active_task_cancelled": True},
            }
        )
    return step.model_copy(
        update={
            "response_goal": "clarification",
            "facts": {**step.facts, "active_task_cancelled": False},
        }
    )


def complete_state_after_action(
    active_task: ActiveTaskState | None,
    *,
    step: PlanStep,
    action_result: dict[str, object] | None,
) -> StateTransition:
    """Clear a completed booking/reschedule task only after a verified successful write result."""
    if active_task is None or step.write_intent is None:
        return StateTransition(active_task=active_task, changed=False, reason="no_active_write_task")
    if step.write_intent.kind not in {"booking", "booking_proposal", "reschedule"}:
        return StateTransition(active_task=active_task, changed=False, reason="unrelated_write")
    if not isinstance(action_result, dict) or action_result.get("ok") is not True:
        return StateTransition(active_task=active_task, changed=False, reason="write_not_completed")
    expected = (
        "booking"
        if step.write_intent.kind in {"booking", "booking_proposal"}
        else "reschedule"
    )
    if active_task.task_type != expected:
        return StateTransition(active_task=active_task, changed=False, reason="task_write_mismatch")
    return StateTransition(active_task=None, changed=True, reason="write_completed")
