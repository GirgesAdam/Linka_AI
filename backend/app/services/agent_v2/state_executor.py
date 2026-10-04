from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.agents.v2.turn_contract import DateConstraint, TimeConstraint, TurnOperation
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
    apply_booking_date_change,
    apply_booking_device_change,
    apply_booking_doctor_change,
    apply_booking_package_usage_change,
    apply_booking_service_change,
    apply_booking_time_change,
    apply_reschedule_date_change,
    apply_reschedule_device_change,
    apply_reschedule_doctor_change,
    apply_reschedule_service_change,
    apply_reschedule_time_change,
    attach_option_snapshot,
    preserve_task_for_side_read,
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


def _booking_constraints(step: PlanStep, operation: TurnOperation) -> CustomerConstraints:
    facts = step.facts
    return CustomerConstraints(
        service_id=str(facts["service_id"]) if facts.get("service_id") else None,
        doctor_id=str(facts["doctor_id"]) if facts.get("doctor_id") else None,
        device_key=str(facts["device_key"]) if facts.get("device_key") else None,
        date=_date_constraint(facts.get("date") or operation.entities.date),
        time=_time_constraint(facts.get("time") or operation.entities.time),
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
    authorized = operation.type == "book" or bool(
        step.write_intent is not None
        and step.write_intent.kind == "booking"
        and step.write_intent.authorized
    )
    return BookingTaskState(
        write_authorization=_authorization(
            operation="booking",
            turn_id=turn_id,
            now=now,
            authorized=authorized,
        ),
        constraints=_booking_constraints(step, operation),
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
    if "device_key" in facts:
        updated = apply_booking_device_change(
            updated,
            device_key=str(facts["device_key"]) if facts.get("device_key") else None,
        )
    if "date" in facts:
        updated = apply_booking_date_change(updated, date=_date_constraint(facts.get("date")))
    elif operation.entities.date is not None:
        updated = apply_booking_date_change(updated, date=operation.entities.date)
    if "time" in facts:
        updated = apply_booking_time_change(updated, time=_time_constraint(facts.get("time")))
    elif operation.entities.time is not None:
        updated = apply_booking_time_change(updated, time=operation.entities.time)
    if operation.package_usage != "unspecified":
        updated = apply_booking_package_usage_change(
            updated,
            package_usage=operation.package_usage,
        )
    return updated


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
        date=_date_constraint(step.facts.get("date") or operation.entities.date),
        time=_time_constraint(step.facts.get("time") or operation.entities.time),
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
    if "date" in facts:
        updated = apply_reschedule_date_change(updated, date=_date_constraint(facts.get("date")))
    elif operation.entities.date is not None:
        updated = apply_reschedule_date_change(updated, date=operation.entities.date)
    if "time" in facts:
        updated = apply_reschedule_time_change(updated, time=_time_constraint(facts.get("time")))
    elif operation.entities.time is not None:
        updated = apply_reschedule_time_change(updated, time=operation.entities.time)
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
    if not slots or step.disposition == "write_ready":
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
    return updated.model_copy(
        update={
            "derived": updated.derived.model_copy(
                update={"availability_snapshot_id": snapshot_id}
            )
        }
    )


def _mark_selected_or_ready(state: ActiveTaskState, step: PlanStep) -> ActiveTaskState:
    if step.state_action != "select_active" and step.disposition != "write_ready":
        return state
    selected_ref = step.facts.get("selected_option_ref")
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
    elif step.state_action == "update_active":
        if isinstance(current, BookingTaskState):
            current = _apply_booking_updates(current, step=step, operation=operation)
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
    if step.write_intent.kind not in {"booking", "reschedule"}:
        return StateTransition(active_task=active_task, changed=False, reason="unrelated_write")
    if not isinstance(action_result, dict) or action_result.get("ok") is not True:
        return StateTransition(active_task=active_task, changed=False, reason="write_not_completed")
    expected = "booking" if step.write_intent.kind == "booking" else "reschedule"
    if active_task.task_type != expected:
        return StateTransition(active_task=active_task, changed=False, reason="task_write_mismatch")
    return StateTransition(active_task=None, changed=True, reason="write_completed")
