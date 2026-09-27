from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from langchain_core.messages import BaseMessage
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.agents.v2.responder import compose_v2_customer_reply
from app.agents.v2.semantic_context import SemanticContext, build_semantic_context
from app.agents.v2.semantic_state_view import (
    with_safe_action_context,
    with_safe_read_context,
    with_safe_task_context,
)
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnOperation
from app.agents.v2.turn_interpreter import interpret_customer_turn_v2
from app.integrations.clinic.base import ClinicAdapter
from app.models.patient import Patient
from app.models.workspace import Workspace
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    persist_initial_task_intent,
    plan_active_task_progress,
)
from app.services.agent_v2.compound_turn_policy import (
    apply_compound_runtime_cursor,
    completed_compound_booking_end,
    compound_anchor_key,
    compound_write_group,
    normalize_compound_turn_plan,
    resolve_compound_followup_after_reads,
)
from app.services.agent_v2.compound_visit_preflight import preflight_compound_visit_plan
from app.services.agent_v2.grouped_booking_continuity import (
    enforce_grouped_booking_write_guard,
    explicitly_narrow_grouped_booking_task,
    grouped_booking_task_from_plan,
    restore_grouped_booking_understanding,
)
from app.services.agent_v2.grouped_write_transaction import (
    begin_group_savepoint,
    release_group_savepoint,
    rollback_group_savepoint,
)
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import build_handoff_outcome, build_step_outcome
from app.services.agent_v2.planner import (
    PlannerContext,
    PlanStep,
    ReadRequest,
    TurnPlan,
    plan_turn,
)
from app.services.agent_v2.read_executor import (
    ReadExecutionBundle,
    ReadExecutionContext,
    execute_step_reads,
)
from app.services.agent_v2.state import (
    ActiveTaskState,
    BookingTaskState,
    OptionChoice,
    OptionSnapshot,
    RescheduleTaskState,
)
from app.services.agent_v2.state_executor import (
    apply_step_state,
    finalize_step_after_state_transition,
)
from app.services.agent_v2.state_persistence import (
    PersistedActiveTask,
    cancel_active_task,
    complete_active_task,
    load_active_task,
    save_active_task,
)
from app.services.agent_v2.turn_normalization import expand_multi_service_operations
from app.services.agent_v2.write_policy import advance_step_with_write_policies

V2WriteExecutor = Callable[[PlanStep], dict[str, object]]


@dataclass(frozen=True)
class PendingV2Write:
    """Verified write boundary returned when no real executor is supplied."""

    step: PlanStep
    reads: ReadExecutionBundle


@dataclass(frozen=True)
class V2RuntimeStepTrace:
    operation_index: int
    operation_type: str
    disposition_before: str
    disposition_after: str
    read_kinds: tuple[str, ...]
    outcome: TurnOutcome | None = None
    pending_write: bool = False
    skipped: bool = False
    skip_reason: str | None = None


@dataclass(frozen=True)
class V2OrchestratedTurn:
    understanding: TiaTurnUnderstanding
    plan: TurnPlan
    traces: tuple[V2RuntimeStepTrace, ...]
    outcomes: tuple[TurnOutcome, ...]
    reply: str | None
    responder_model: str | None
    active_task: ActiveTaskState | None
    persisted_task: PersistedActiveTask | None
    pending_write: PendingV2Write | None
    verified_action_context: dict[str, object] | None = None
    pending_choice: OptionSnapshot | None = None


def _task_dict(active_task: ActiveTaskState | None) -> dict[str, Any] | None:
    return active_task.model_dump(mode="json") if active_task is not None else None


def _pending_choice_from_context(
    value: dict[str, Any] | None,
    *,
    now: datetime,
) -> OptionSnapshot | None:
    if not isinstance(value, dict):
        return None
    try:
        snapshot = OptionSnapshot.model_validate(value)
    except ValueError:
        return None
    if now >= snapshot.expires_at:
        return None
    if snapshot.purpose not in {"appointment", "appointment_target"}:
        return None
    return snapshot


def _appointment_choice_snapshot(
    *,
    step: PlanStep,
    outcome: TurnOutcome,
    now: datetime,
    turn_id: str,
) -> OptionSnapshot | None:
    if (
        step.response_goal != "ask_appointment_choice"
        or step.operation_type not in {"cancel_appointment", "confirm_appointment", "reschedule"}
    ):
        return None
    choices: list[OptionChoice] = []
    for choice in outcome.choices:
        appointment_id = choice.facts.get("appointment_id")
        if appointment_id in (None, ""):
            continue
        choices.append(
            OptionChoice(
                ref=choice.ref,
                label=choice.label,
                payload={"appointment_id": str(appointment_id)},
            )
        )
    if not choices:
        return None
    return OptionSnapshot(
        snapshot_id=f"{turn_id}:{step.operation_index}:appointment-choice",
        purpose="appointment",
        lifecycle_action=step.operation_type,
        task_version=1,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        options=choices,
    )


def _selected_appointment_snapshot(
    *,
    step: PlanStep,
    now: datetime,
    turn_id: str,
) -> OptionSnapshot | None:
    if step.operation_type != "select_active":
        return None
    action = step.facts.get("lifecycle_action")
    selected = step.facts.get("selected_option")
    if action not in {"cancel_appointment", "confirm_appointment", "reschedule"}:
        return None
    if not isinstance(selected, dict):
        return None
    payload = selected.get("payload")
    if not isinstance(payload, dict) or payload.get("appointment_id") in (None, ""):
        return None
    choice = OptionChoice(
        ref=str(selected.get("ref") or "appointment-target"),
        label=str(selected.get("label")) if selected.get("label") not in (None, "") else None,
        payload={"appointment_id": str(payload["appointment_id"])},
    )
    return OptionSnapshot(
        snapshot_id=f"{turn_id}:{step.operation_index}:appointment-target",
        purpose="appointment_target",
        lifecycle_action=action,
        task_version=1,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        options=[choice],
    )


def _operation_for_step(
    understanding: TiaTurnUnderstanding,
    step: PlanStep,
) -> TurnOperation:
    try:
        return understanding.operations[step.operation_index]
    except IndexError as exc:
        raise RuntimeError("V2 plan referenced an operation outside the structured turn.") from exc


def _advance_after_reads(step: PlanStep, reads: ReadExecutionBundle) -> PlanStep:
    if not step.reads:
        return step
    if step.write_intent is None and step.state_action != "start_reschedule":
        return step
    return advance_step_with_write_policies(step, reads)


def _compatibility_failure_step(
    step: PlanStep,
    reads: ReadExecutionBundle,
) -> PlanStep | None:
    """Turn a canonically proven compatibility read failure into safe continuation."""
    for result in reads.results:
        if result.kind != "availability" or result.ok:
            continue
        failure = result.payload.get("compatibility_failure")
        if not isinstance(failure, dict):
            continue
        dimension = failure.get("dimension")
        if dimension not in {"doctor", "device"}:
            continue

        facts = {
            **step.facts,
            "compatibility_failure": dict(failure),
            "doctor_id" if dimension == "doctor" else "device_key": None,
        }
        state_action = step.state_action
        if state_action == "none" and step.operation_type == "book":
            state_action = "start_booking"
        return step.model_copy(
            update={
                "disposition": "clarify",
                "write_intent": None,
                "state_action": state_action,
                "response_goal": "clarification",
                "clarification_field": str(dimension),
                "facts": facts,
            }
        )
    return None


def _verified_no_availability(reads: ReadExecutionBundle | None) -> bool:
    """True only for a successful availability read that canonically found zero options."""
    if reads is None:
        return False
    availability_results = [result for result in reads.results if result.kind == "availability"]
    if not availability_results:
        return False
    for result in availability_results:
        if not result.ok:
            return False
        count = result.payload.get("matching_slot_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
        if count > 0:
            return False
    return True


def _continuation_condition_satisfied(
    operation: TurnOperation,
    *,
    previous_reads: ReadExecutionBundle | None,
) -> bool:
    condition = getattr(operation, "continuation_condition", "always")
    if condition == "always":
        return True
    if condition == "if_previous_no_availability":
        return _verified_no_availability(previous_reads)
    raise RuntimeError(f"Unsupported continuation condition: {condition}")


def _completed_action_context(
    step: PlanStep,
    action_result: dict[str, object],
) -> dict[str, object] | None:
    """Capture only canonical facts needed for the immediately following turn."""
    intent = step.write_intent
    if (
        step.disposition != "write_ready"
        or intent is None
        or action_result.get("ok") is not True
    ):
        return None

    parameters = dict(intent.parameters)
    if intent.kind == "buy_pulse_pack":
        device_key = parameters.get("device_key")
        if device_key in (None, ""):
            return None
        context: dict[str, object] = {
            "operation_type": "buy_pulse_pack",
            "device_key": str(device_key),
        }
        pulse_count = parameters.get("pulse_count")
        if isinstance(pulse_count, int) and not isinstance(pulse_count, bool) and pulse_count > 0:
            context["pulse_count"] = pulse_count
        return context

    if intent.kind == "booking":
        required = ("service_id", "doctor_id", "start_at")
        if any(parameters.get(key) in (None, "") for key in required):
            return None
        appointment_id = action_result.get("appointment_id")
        status = action_result.get("status")
        if appointment_id in (None, "") or status not in {"pending", "confirmed"}:
            return None
        context = {
            "operation_type": "book",
            "appointment_id": str(appointment_id),
            "service_id": str(parameters["service_id"]),
            "doctor_id": str(parameters["doctor_id"]),
            "start_at": str(parameters["start_at"]),
            "status": str(status),
            "package_usage": str(parameters.get("package_usage") or "unspecified"),
        }
        if parameters.get("device_key") not in (None, ""):
            context["device_key"] = str(parameters["device_key"])
        for key in ("date", "time"):
            if isinstance(parameters.get(key), dict):
                context[key] = dict(parameters[key])
        return context

    if intent.kind == "reschedule":
        appointment_id = action_result.get("appointment_id")
        previous_appointment_id = action_result.get("previous_appointment_id")
        status = action_result.get("status")
        required = ("service_id", "doctor_id", "start_at")
        if (
            appointment_id in (None, "")
            or previous_appointment_id in (None, "")
            or status not in {"pending", "confirmed"}
            or any(parameters.get(key) in (None, "") for key in required)
        ):
            return None
        context = {
            "operation_type": "reschedule",
            "appointment_id": str(appointment_id),
            "previous_appointment_id": str(previous_appointment_id),
            "service_id": str(parameters["service_id"]),
            "doctor_id": str(parameters["doctor_id"]),
            "start_at": str(parameters["start_at"]),
            "status": str(status),
        }
        if parameters.get("device_key") not in (None, ""):
            context["device_key"] = str(parameters["device_key"])
        for key in ("date", "time"):
            if isinstance(parameters.get(key), dict):
                context[key] = dict(parameters[key])
        return context

    if intent.kind == "cancel_appointment":
        appointment_id = action_result.get("appointment_id")
        if appointment_id in (None, "") or action_result.get("status") != "cancelled":
            return None
        return {
            "operation_type": "cancel_appointment",
            "appointment_id": str(appointment_id),
            "status": "cancelled",
        }
    return None


def _exact_requested_start_at(step: PlanStep, *, timezone_name: str) -> datetime | None:
    date = step.facts.get("date")
    time = step.facts.get("time")
    if not isinstance(date, dict) or not isinstance(time, dict):
        return None
    if date.get("mode") != "exact" or time.get("mode") != "exact":
        return None
    start_date = date.get("start_date")
    start_time = time.get("start_time")
    if not isinstance(start_date, str) or not isinstance(start_time, str):
        return None
    try:
        local = datetime.fromisoformat(f"{start_date}T{start_time}")
        return local.replace(tzinfo=ZoneInfo(timezone_name))
    except (ValueError, KeyError):
        return None


def _same_recent_booking(
    step: PlanStep,
    recent_action: dict[str, object],
    *,
    timezone_name: str,
) -> bool:
    if (
        step.operation_type != "book"
        or recent_action.get("operation_type") != "book"
        or recent_action.get("status") not in {"pending", "confirmed"}
    ):
        return False
    expected_start = _exact_requested_start_at(step, timezone_name=timezone_name)
    recent_start_raw = recent_action.get("start_at")
    if expected_start is None or not isinstance(recent_start_raw, str):
        return False
    try:
        recent_start = datetime.fromisoformat(recent_start_raw)
    except ValueError:
        return False
    if recent_start.tzinfo is None or recent_start.utcoffset() is None:
        return False
    if expected_start.astimezone(recent_start.tzinfo) != recent_start:
        return False

    def same(key: str) -> bool:
        current = step.facts.get(key)
        recent = recent_action.get(key)
        return str(current) == str(recent) if current not in (None, "") or recent not in (None, "") else True

    return (
        same("service_id")
        and same("doctor_id")
        and same("device_key")
        and str(step.facts.get("package_usage") or "unspecified")
        == str(recent_action.get("package_usage") or "unspecified")
    )


def _same_recent_reschedule(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object],
    *,
    timezone_name: str,
) -> bool:
    if (
        step.operation_type != "reschedule"
        or recent_action.get("operation_type") != "reschedule"
        or recent_action.get("status") not in {"pending", "confirmed"}
    ):
        return False
    if operation.selection is not None:
        return False
    appointment = operation.entities.appointment
    if appointment is not None and (
        appointment.ref is not None
        or appointment.text not in (None, "")
        or appointment.candidate_refs
    ):
        return False

    expected_start = _exact_requested_start_at(step, timezone_name=timezone_name)
    recent_start_raw = recent_action.get("start_at")
    if expected_start is None or not isinstance(recent_start_raw, str):
        return False
    try:
        recent_start = datetime.fromisoformat(recent_start_raw)
    except ValueError:
        return False
    if recent_start.tzinfo is None or recent_start.utcoffset() is None:
        return False
    if expected_start.astimezone(recent_start.tzinfo) != recent_start:
        return False

    def compatible(key: str) -> bool:
        current = step.facts.get(key)
        recent = recent_action.get(key)
        return current in (None, "") or str(current) == str(recent)

    if not all(compatible(key) for key in ("service_id", "doctor_id", "device_key")):
        return False

    source = step.facts.get("source_appointment")
    if isinstance(source, dict):
        source_id = source.get("appointment_id")
        if source_id not in (None, "") and str(source_id) != str(
            recent_action.get("appointment_id")
        ):
            return False
        for key in ("service_id", "doctor_id", "device_key"):
            value = source.get(key)
            if value not in (None, "") and str(value) != str(recent_action.get(key)):
                return False
    return True


def _recent_reschedule_validation_request(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object] | None,
    *,
    timezone_name: str,
) -> ReadRequest | None:
    if not isinstance(recent_action, dict):
        return None
    if not _same_recent_reschedule(
        step,
        operation,
        recent_action,
        timezone_name=timezone_name,
    ):
        return None
    appointment_id = recent_action.get("appointment_id")
    if appointment_id in (None, ""):
        return None
    return ReadRequest(
        kind="appointments",
        parameters={"appointment_id": str(appointment_id)},
    )


def _canonical_recent_reschedule_is_current(
    recent_action: dict[str, object] | None,
    reads: ReadExecutionBundle,
) -> bool:
    if not isinstance(recent_action, dict):
        return False
    appointment_id = recent_action.get("appointment_id")
    if appointment_id in (None, "") or reads.verification.appointment_match_count != 1:
        return False
    for result in reads.results:
        if result.kind != "appointments" or not result.ok:
            continue
        rows = result.payload.get("appointments")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("appointment_id")) != str(appointment_id):
                continue
            if row.get("status") not in {"pending", "confirmed"}:
                return False
            for row_key, context_key in (
                ("service_id", "service_id"),
                ("doctor_id", "doctor_id"),
                ("laser_device_key", "device_key"),
            ):
                expected = recent_action.get(context_key)
                current = row.get(row_key)
                if expected in (None, ""):
                    if current not in (None, ""):
                        return False
                elif str(current) != str(expected):
                    return False
            current_start = row.get("start_at")
            recent_start = recent_action.get("start_at")
            if not isinstance(current_start, str) or not isinstance(recent_start, str):
                return False
            try:
                current_dt = datetime.fromisoformat(current_start)
                recent_dt = datetime.fromisoformat(recent_start)
            except ValueError:
                return False
            if (
                current_dt.tzinfo is None
                or current_dt.utcoffset() is None
                or recent_dt.tzinfo is None
                or recent_dt.utcoffset() is None
                or current_dt != recent_dt
            ):
                return False
            return True
    return False


def _reschedule_acknowledgment_step(step: PlanStep) -> PlanStep:
    return step.model_copy(
        update={
            "disposition": "respond",
            "reads": [],
            "write_intent": None,
            "state_action": "none",
            "response_goal": "social_ack",
            "clarification_field": None,
            "facts": _acknowledgment_facts("reschedule"),
        }
    )


def _bare_recent_cancellation(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object],
) -> bool:
    if (
        step.operation_type != "cancel_appointment"
        or recent_action.get("operation_type") != "cancel_appointment"
        or recent_action.get("status") != "cancelled"
    ):
        return False
    explicit_target = step.facts.get("appointment_id")
    if explicit_target not in (None, ""):
        return str(explicit_target) == str(recent_action.get("appointment_id"))
    if operation.selection is not None:
        return False
    entities = operation.entities
    return all(
        value is None
        for value in (
            entities.appointment,
            entities.service,
            entities.doctor,
            entities.device,
            entities.date,
            entities.time,
        )
    )


def _acknowledgment_facts(action: str, *, same_booking: bool = False) -> dict[str, object]:
    return {
        "acknowledgment": {
            "action": action,
            "already_completed": True,
            **({"same_booking": True} if same_booking else {}),
        }
    }


def _recent_booking_validation_request(
    step: PlanStep,
    recent_action: dict[str, object] | None,
    *,
    timezone_name: str,
) -> ReadRequest | None:
    """Return the targeted canonical read required before a duplicate-booking ack."""
    if not isinstance(recent_action, dict):
        return None
    if not _same_recent_booking(step, recent_action, timezone_name=timezone_name):
        return None
    appointment_id = recent_action.get("appointment_id")
    if appointment_id in (None, ""):
        return None
    return ReadRequest(
        kind="appointments",
        parameters={"appointment_id": str(appointment_id)},
    )


def _canonical_recent_booking_is_current(
    recent_action: dict[str, object] | None,
    reads: ReadExecutionBundle,
) -> bool:
    """Require the recent booking to still match canonical active appointment truth."""
    if not isinstance(recent_action, dict):
        return False
    appointment_id = recent_action.get("appointment_id")
    if appointment_id in (None, "") or reads.verification.appointment_match_count != 1:
        return False

    def same_value(current: object, expected: object) -> bool:
        if expected in (None, ""):
            return current in (None, "")
        return str(current) == str(expected)

    for result in reads.results:
        if result.kind != "appointments" or not result.ok:
            continue
        rows = result.payload.get("appointments")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("appointment_id")) != str(appointment_id):
                continue
            if row.get("status") not in {"pending", "confirmed"}:
                return False
            if not same_value(row.get("service_id"), recent_action.get("service_id")):
                return False
            if not same_value(row.get("doctor_id"), recent_action.get("doctor_id")):
                return False
            if not same_value(row.get("laser_device_key"), recent_action.get("device_key")):
                return False
            current_start = row.get("start_at")
            recent_start = recent_action.get("start_at")
            if not isinstance(current_start, str) or not isinstance(recent_start, str):
                return False
            try:
                current_dt = datetime.fromisoformat(current_start)
                recent_dt = datetime.fromisoformat(recent_start)
            except ValueError:
                return False
            if (
                current_dt.tzinfo is None
                or current_dt.utcoffset() is None
                or recent_dt.tzinfo is None
                or recent_dt.utcoffset() is None
                or current_dt != recent_dt
            ):
                return False
            return True
    return False


def _active_reschedule_target_validation_request(
    step: PlanStep,
    active_task: ActiveTaskState | None,
) -> ReadRequest | None:
    if not isinstance(active_task, RescheduleTaskState):
        return None
    if step.operation_type != "reschedule":
        return None
    target_id = active_task.target.appointment_id
    for request in step.reads:
        if (
            request.kind == "appointments"
            and str(request.parameters.get("appointment_id") or "") == str(target_id)
        ):
            return request
    return None


def _canonical_reschedule_target_is_non_actionable(
    request: ReadRequest | None,
    reads: ReadExecutionBundle,
) -> bool:
    if request is None or request.kind != "appointments":
        return False
    target_id = request.parameters.get("appointment_id")
    if target_id in (None, "") or reads.verification.appointment_match_count != 0:
        return False
    appointment_results = [
        result for result in reads.results if result.kind == "appointments"
    ]
    if len(appointment_results) != 1 or not appointment_results[0].ok:
        return False
    rows = appointment_results[0].payload.get("appointments")
    return isinstance(rows, list) and not rows


def _invalidated_reschedule_context(
    active_task: ActiveTaskState | None,
) -> dict[str, object] | None:
    if not isinstance(active_task, RescheduleTaskState):
        return None
    return {
        "operation_type": "reschedule_invalidated",
        "target_appointment_id": active_task.target.appointment_id,
        "replacement": active_task.replacement.model_dump(mode="json"),
    }


def _same_invalidated_reschedule_continuation(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object],
) -> bool:
    if (
        recent_action.get("operation_type") != "reschedule_invalidated"
        or not operation.continues_previous
        or step.operation_type not in {"book", "reschedule"}
    ):
        return False
    raw_replacement = recent_action.get("replacement")
    if not isinstance(raw_replacement, dict):
        return False

    def compatible(current: object, previous: object) -> bool:
        if current in (None, "", {}, []):
            return True
        if isinstance(current, dict):
            if not isinstance(previous, dict):
                return False
            return all(
                compatible(value, previous.get(key))
                for key, value in current.items()
            )
        return previous not in (None, "", {}, []) and current == previous

    return all(
        compatible(step.facts.get(key), raw_replacement.get(key))
        for key in ("service_id", "doctor_id", "device_key", "date", "time")
    )


def _invalidated_reschedule_continuation_step(step: PlanStep) -> PlanStep:
    return step.model_copy(
        update={
            "disposition": "respond",
            "reads": [],
            "write_intent": None,
            "state_action": "none",
            "response_goal": "clarification",
            "clarification_field": None,
            "facts": {
                "active_task_invalidated": True,
                "canonical_target_non_actionable": True,
                "stale_continuation_blocked": True,
            },
        }
    )


def _invalidated_reschedule_step(step: PlanStep) -> PlanStep:
    return step.model_copy(
        update={
            "disposition": "respond",
            "reads": [],
            "write_intent": None,
            "state_action": "cancel_active",
            "response_goal": "clarification",
            "clarification_field": None,
            "facts": {
                **step.facts,
                "active_task_invalidated": True,
                "canonical_target_non_actionable": True,
            },
        }
    )


def _booking_acknowledgment_step(step: PlanStep) -> PlanStep:
    return step.model_copy(
        update={
            "disposition": "respond",
            "reads": [],
            "write_intent": None,
            "state_action": "none",
            "response_goal": "social_ack",
            "clarification_field": None,
            "facts": _acknowledgment_facts("booking", same_booking=True),
        }
    )


def _normalize_recent_action_acknowledgments(
    plan: TurnPlan,
    understanding: TiaTurnUnderstanding,
    *,
    recent_action: dict[str, object] | None,
    timezone_name: str,
) -> TurnPlan:
    if not isinstance(recent_action, dict):
        return plan
    normalized: list[PlanStep] = []
    changed = False
    for step in plan.steps:
        operation = _operation_for_step(understanding, step)
        ack_facts: dict[str, object] | None = None
        # Booking acknowledgments are resolved later, after a targeted canonical
        # appointment read. Recent action context is continuity evidence, not
        # current lifecycle truth.
        if _bare_recent_cancellation(step, operation, recent_action):
            ack_facts = _acknowledgment_facts("cancel_appointment")
        if _same_invalidated_reschedule_continuation(step, operation, recent_action):
            normalized.append(_invalidated_reschedule_continuation_step(step))
            changed = True
            continue
        if ack_facts is None:
            normalized.append(step)
            continue
        normalized.append(
            step.model_copy(
                update={
                    "disposition": "respond",
                    "reads": [],
                    "write_intent": None,
                    "state_action": "none",
                    "response_goal": "social_ack",
                    "clarification_field": None,
                    "facts": ack_facts,
                }
            )
        )
        changed = True
    return plan.model_copy(update={"steps": normalized}) if changed else plan


def _terminal_handoff_plan(plan: TurnPlan) -> bool:
    """Safety/standalone handoffs remain terminal; mixed safe reads may run first."""
    return plan.handoff_category is not None and all(
        step.disposition == "handoff" for step in plan.steps
    )


def _preserve_active_task_on_handoff(plan: TurnPlan) -> bool:
    return any(
        step.disposition == "handoff"
        and step.facts.get("preserve_active_task") is True
        for step in plan.steps
    )


def _persist_final_task(
    *,
    db: Session,
    workspace_id: UUID,
    conversation_id: UUID,
    patient_id: UUID,
    run_id: UUID,
    initial: PersistedActiveTask | None,
    final_task: ActiveTaskState | None,
    cancelled_existing_task: bool,
    cancelled_existing_task_reason: str | None,
    completed_existing_task_result: dict[str, object] | None,
) -> PersistedActiveTask | None:
    initial_task = initial.active_task if initial is not None else None
    if final_task == initial_task and completed_existing_task_result is None:
        return initial

    if final_task is None:
        if initial is not None:
            if completed_existing_task_result is not None:
                complete_active_task(
                    db,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    patient_id=patient_id,
                    expected=initial,
                    run_id=run_id,
                    result=completed_existing_task_result,
                )
            elif cancelled_existing_task:
                cancel_active_task(
                    db,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    patient_id=patient_id,
                    expected=initial,
                    run_id=run_id,
                    reason=(
                        cancelled_existing_task_reason
                        or "customer_cancelled_active_task"
                    ),
                )
            else:
                raise RuntimeError(
                    "V2 runtime may clear persisted state only through an explicit cancellation "
                    "or a completed terminal write."
                )
        return None

    return save_active_task(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        patient_id=patient_id,
        active_task=final_task,
        run_id=run_id,
        expected=initial,
    )


def orchestrate_v2_turn(
    *,
    db: Session,
    workspace: Workspace,
    patient: Patient,
    conversation_id: UUID,
    run_id: UUID,
    history: list[BaseMessage],
    local_now: datetime,
    timezone_name: str,
    clinic_name: str,
    catalog: dict[str, Any] | None = None,
    adapter: ClinicAdapter | None = None,
    turn_id: str | None = None,
    write_executor: V2WriteExecutor | None = None,
    recent_read_context: dict[str, Any] | None = None,
    recent_action_context: dict[str, Any] | None = None,
    pending_choice_context: dict[str, Any] | None = None,
) -> V2OrchestratedTurn:
    """Run one stateful V2 turn with an optional verified-write executor.

    With no executor this preserves the isolated/shadow boundary and returns the first
    ``PendingV2Write`` without mutating clinic business data. Production may supply a
    real executor; successful writes are converted to verified outcomes and compound
    operations continue in order. The caller owns the outer database transaction.
    """
    persisted = load_active_task(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation_id,
        patient_id=patient.id,
        run_id=run_id,
    )
    initial_task = persisted.active_task if persisted is not None else None
    pending_choice = _pending_choice_from_context(
        pending_choice_context,
        now=local_now,
    )

    canonical_catalog = catalog or build_clinic_catalog(db, workspace)
    semantic_context: SemanticContext = build_semantic_context(canonical_catalog)
    task_context_kwargs: dict[str, Any] = {
        "active_task": _task_dict(initial_task),
    }
    if pending_choice is not None:
        task_context_kwargs["pending_choice"] = pending_choice.model_dump(mode="json")
    semantic_context = with_safe_task_context(
        semantic_context,
        **task_context_kwargs,
    )
    semantic_context = with_safe_read_context(
        semantic_context,
        read_context=recent_read_context,
    )
    semantic_context = with_safe_action_context(
        semantic_context,
        action_context=recent_action_context,
    )
    understanding = interpret_customer_turn_v2(
        history=history,
        semantic_context=semantic_context,
        timezone_name=timezone_name,
        local_now=local_now,
    )
    understanding, semantic_visit_groups = expand_multi_service_operations(
        understanding,
        semantic_context=semantic_context,
    )
    initial_booking_task = (
        initial_task if isinstance(initial_task, BookingTaskState) else None
    )
    narrowed_booking_task = explicitly_narrow_grouped_booking_task(
        understanding,
        active_task=initial_booking_task,
        context=semantic_context,
    )
    understanding, restored_visit_groups = restore_grouped_booking_understanding(
        understanding,
        active_task=initial_booking_task,
        context=semantic_context,
    )
    semantic_visit_groups = {
        **semantic_visit_groups,
        **restored_visit_groups,
    }
    planner_active_task: ActiveTaskState | None = narrowed_booking_task or initial_task
    plan = plan_turn(
        understanding,
        PlannerContext(
            semantic_context=semantic_context,
            active_task=planner_active_task,
            now=local_now,
            pending_choice=pending_choice,
        ),
    )
    plan = normalize_compound_turn_plan(
        plan,
        catalog=canonical_catalog,
        operation_visit_groups=semantic_visit_groups,
    )
    plan = _normalize_recent_action_acknowledgments(
        plan,
        understanding,
        recent_action=recent_action_context,
        timezone_name=timezone_name,
    )

    if _terminal_handoff_plan(plan):
        outcome = build_handoff_outcome(plan)
        preserve_active_task = _preserve_active_task_on_handoff(plan)
        if persisted is not None and not preserve_active_task:
            cancel_active_task(
                db,
                workspace_id=workspace.id,
                conversation_id=conversation_id,
                patient_id=patient.id,
                expected=persisted,
                run_id=run_id,
                reason="human_handoff_requested",
            )
        reply, model = compose_v2_customer_reply(
            clinic_name=clinic_name,
            timezone_name=timezone_name,
            local_now=local_now,
            history=history,
            outcomes=[outcome],
        )
        return V2OrchestratedTurn(
            understanding=understanding,
            plan=plan,
            traces=(),
            outcomes=(outcome,),
            reply=reply,
            responder_model=model,
            active_task=initial_task if preserve_active_task else None,
            persisted_task=persisted if preserve_active_task else None,
            pending_write=None,
        )

    read_context = ReadExecutionContext(
        db=db,
        workspace=workspace,
        patient=patient,
        now=local_now,
        catalog=canonical_catalog,
        adapter=adapter,
    )
    resolved_turn_id = turn_id or str(run_id)
    stable_visit_group_id = str(
        uuid5(NAMESPACE_URL, f"tia-v2-visit:{workspace.id}:{resolved_turn_id}")
    )
    plan = preflight_compound_visit_plan(
        plan,
        context=read_context,
        timezone_name=timezone_name,
        visit_group_id=stable_visit_group_id,
    )
    grouped_booking_task = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=initial_booking_task,
        now=local_now,
        turn_id=resolved_turn_id,
    )
    plan = enforce_grouped_booking_write_guard(
        plan,
        active_task=initial_booking_task,
        candidate_task=grouped_booking_task,
        explicit_narrowing=narrowed_booking_task is not None,
    )
    current_task: ActiveTaskState | None = (
        grouped_booking_task or narrowed_booking_task or initial_task
    )
    traces: list[V2RuntimeStepTrace] = []
    outcomes: list[TurnOutcome] = []
    pending_write: PendingV2Write | None = None
    outgoing_pending_choice: OptionSnapshot | None = None
    cancelled_existing_task = False
    cancelled_existing_task_reason: str | None = None
    completed_existing_task_result: dict[str, object] | None = None
    completed_action_context: dict[str, object] | None = None
    compound_cursors: dict[str, datetime] = {}
    operation_reads: dict[int, ReadExecutionBundle] = {}
    grouped_positions: dict[str, list[int]] = {}
    for position, grouped_step in enumerate(plan.steps):
        group = compound_write_group(grouped_step)
        if group is not None:
            grouped_positions.setdefault(group, []).append(position)
    active_group_key: str | None = None
    active_group_tx = None
    active_group_outcome_start = 0
    active_group_trace_start = 0

    for step_position, planned_step in enumerate(plan.steps):
        anchor_key = compound_anchor_key(planned_step)
        planned_step = apply_compound_runtime_cursor(
            planned_step,
            previous_end_at=compound_cursors.get(anchor_key) if anchor_key is not None else None,
            timezone_name=timezone_name,
        )
        operation = _operation_for_step(understanding, planned_step)
        if not _continuation_condition_satisfied(
            operation,
            previous_reads=operation_reads.get(planned_step.operation_index - 1),
        ):
            traces.append(
                V2RuntimeStepTrace(
                    operation_index=planned_step.operation_index,
                    operation_type=planned_step.operation_type,
                    disposition_before=planned_step.disposition,
                    disposition_after="skipped",
                    read_kinds=(),
                    skipped=True,
                    skip_reason=(
                        "continuation_condition_false:"
                        f"{getattr(operation, 'continuation_condition', 'always')}"
                    ),
                )
            )
            continue

        effective_step = adapt_matching_active_task_step(
            planned_step,
            operation=operation,
            active_task=current_task,
            context=semantic_context,
        )
        effective_step = persist_initial_task_intent(
            effective_step,
            operation=operation,
            context=semantic_context,
        )
        step_group_key = compound_write_group(effective_step) or compound_write_group(planned_step)

        if effective_step.state_action == "update_active":
            initial_transition = apply_step_state(
                current_task,
                step=effective_step,
                operation=operation,
                reads=None,
                now=local_now,
                turn_id=resolved_turn_id,
            )
            current_task = initial_transition.active_task
            if current_task is not None:
                effective_step = plan_active_task_progress(
                    current_task,
                    operation_index=effective_step.operation_index,
                    context=semantic_context,
                )

        target_validation = _active_reschedule_target_validation_request(
            effective_step,
            current_task,
        )
        target_validation_reads = ReadExecutionBundle()
        if target_validation is not None:
            target_validation_step = effective_step.model_copy(
                update={"reads": [target_validation]}
            )
            target_validation_reads = execute_step_reads(
                target_validation_step,
                read_context,
            )

        recent_booking_validation = _recent_booking_validation_request(
            effective_step,
            recent_action_context,
            timezone_name=timezone_name,
        )
        recent_reschedule_validation = _recent_reschedule_validation_request(
            effective_step,
            operation,
            recent_action_context,
            timezone_name=timezone_name,
        )
        validation_request = recent_booking_validation or recent_reschedule_validation
        validation_reads = ReadExecutionBundle()
        if validation_request is not None:
            validation_step = effective_step.model_copy(
                update={"reads": [validation_request]}
            )
            validation_reads = execute_step_reads(validation_step, read_context)

        if _canonical_reschedule_target_is_non_actionable(
            target_validation,
            target_validation_reads,
        ):
            reads = target_validation_reads
            invalidated_context = _invalidated_reschedule_context(current_task)
            if invalidated_context is not None:
                completed_action_context = invalidated_context
            advanced = _invalidated_reschedule_step(effective_step)
        elif (
            recent_booking_validation is not None
            and _canonical_recent_booking_is_current(
                recent_action_context,
                validation_reads,
            )
        ):
            reads = validation_reads
            advanced = _booking_acknowledgment_step(effective_step)
        elif (
            recent_reschedule_validation is not None
            and _canonical_recent_reschedule_is_current(
                recent_action_context,
                validation_reads,
            )
        ):
            reads = validation_reads
            advanced = _reschedule_acknowledgment_step(effective_step)
        else:
            normal_reads = (
                execute_step_reads(effective_step, read_context)
                if effective_step.reads
                else ReadExecutionBundle()
            )
            compatibility_advanced = _compatibility_failure_step(
                effective_step,
                normal_reads,
            )
            if compatibility_advanced is None:
                compound_advanced, normal_reads, compound_handled = (
                    resolve_compound_followup_after_reads(
                        effective_step,
                        normal_reads,
                    )
                )
                advanced = (
                    compound_advanced
                    if compound_handled
                    else _advance_after_reads(effective_step, normal_reads)
                )
            else:
                advanced = compatibility_advanced
            reads = (
                ReadExecutionBundle(
                    results=[*validation_reads.results, *normal_reads.results],
                    verification=normal_reads.verification,
                )
                if validation_reads.results
                else normal_reads
            )

        operation_reads[effective_step.operation_index] = reads
        transition = apply_step_state(
            current_task,
            step=advanced,
            operation=operation,
            reads=reads if reads.results else None,
            now=local_now,
            turn_id=resolved_turn_id,
        )
        advanced = finalize_step_after_state_transition(advanced, transition)
        if (
            advanced.state_action == "cancel_active"
            and transition.changed
            and persisted is not None
            and transition.active_task is None
        ):
            cancelled_existing_task = True
            if advanced.facts.get("canonical_target_non_actionable") is True:
                cancelled_existing_task_reason = (
                    "canonical_reschedule_target_non_actionable"
                )
        current_task = transition.active_task

        if advanced.disposition == "write_ready":
            if write_executor is None:
                pending_write = PendingV2Write(step=advanced, reads=reads)
                traces.append(
                    V2RuntimeStepTrace(
                        operation_index=advanced.operation_index,
                        operation_type=advanced.operation_type,
                        disposition_before=planned_step.disposition,
                        disposition_after=advanced.disposition,
                        read_kinds=tuple(result.kind for result in reads.results),
                        pending_write=True,
                    )
                )
                break

            if step_group_key is not None and active_group_tx is None:
                active_group_key = step_group_key
                active_group_outcome_start = len(outcomes)
                active_group_trace_start = len(traces)
                active_group_tx = begin_group_savepoint(db)

            action_result = write_executor(advanced)
            outcome = build_step_outcome(
                advanced,
                turn=understanding,
                semantic_context=semantic_context,
                reads=reads if reads.results else None,
                action_result=action_result,
                active_task_summary=_task_dict(current_task),
            )
            traces.append(
                V2RuntimeStepTrace(
                    operation_index=advanced.operation_index,
                    operation_type=advanced.operation_type,
                    disposition_before=planned_step.disposition,
                    disposition_after=advanced.disposition,
                    read_kinds=tuple(result.kind for result in reads.results),
                    outcome=outcome,
                )
            )
            outcomes.append(outcome)

            if outcome.status == "completed":
                action_context = _completed_action_context(advanced, action_result)
                if action_context is not None:
                    completed_action_context = action_context
                if (
                    step_group_key is not None
                    and active_group_key == step_group_key
                    and grouped_positions.get(step_group_key)
                    and step_position == grouped_positions[step_group_key][-1]
                    and active_group_tx is not None
                ):
                    release_group_savepoint(active_group_tx)
                    active_group_tx = None
                    active_group_key = None
                write_kind = advanced.write_intent.kind if advanced.write_intent is not None else None
                if write_kind == "booking" and anchor_key is not None:
                    verified_end = completed_compound_booking_end(reads)
                    if verified_end is not None:
                        compound_cursors[anchor_key] = verified_end
                terminal_task_write = step_group_key is None or (
                    bool(grouped_positions.get(step_group_key))
                    and step_position == grouped_positions[step_group_key][-1]
                )
                if (
                    terminal_task_write
                    and current_task is not None
                    and current_task.task_type == write_kind
                ):
                    if (
                        persisted is not None
                        and persisted.active_task.task_type == current_task.task_type
                    ):
                        completed_existing_task_result = dict(action_result)
                    current_task = None
                continue

            if outcome.status == "handoff":
                if current_task is not None:
                    cancelled_existing_task = persisted is not None
                    current_task = None
            if step_group_key is not None and active_group_key == step_group_key and active_group_tx is not None:
                rollback_group_savepoint(active_group_tx)
                active_group_tx = None
                active_group_key = None
                outcomes = outcomes[:active_group_outcome_start]
                traces = traces[:active_group_trace_start]
                outcomes.append(outcome)
                traces.append(
                    V2RuntimeStepTrace(
                        operation_index=advanced.operation_index,
                        operation_type=advanced.operation_type,
                        disposition_before=planned_step.disposition,
                        disposition_after=advanced.disposition,
                        read_kinds=tuple(result.kind for result in reads.results),
                        outcome=outcome,
                    )
                )
            break

        outcome = build_step_outcome(
            advanced,
            turn=understanding,
            semantic_context=semantic_context,
            reads=reads if reads.results else None,
            action_result=None,
            active_task_summary=None,
        )
        if step_group_key is not None and active_group_key == step_group_key and active_group_tx is not None:
            rollback_group_savepoint(active_group_tx)
            active_group_tx = None
            active_group_key = None
            outcomes = outcomes[:active_group_outcome_start]
            traces = traces[:active_group_trace_start]
        traces.append(
            V2RuntimeStepTrace(
                operation_index=advanced.operation_index,
                operation_type=advanced.operation_type,
                disposition_before=planned_step.disposition,
                disposition_after=advanced.disposition,
                read_kinds=tuple(result.kind for result in reads.results),
                outcome=outcome,
            )
        )
        outcomes.append(outcome)
        selected_snapshot = _selected_appointment_snapshot(
            step=advanced,
            now=local_now,
            turn_id=resolved_turn_id,
        )
        choice_snapshot = _appointment_choice_snapshot(
            step=advanced,
            outcome=outcome,
            now=local_now,
            turn_id=resolved_turn_id,
        )
        outgoing_pending_choice = selected_snapshot or choice_snapshot or outgoing_pending_choice
        if outcome.status == "handoff":
            outgoing_pending_choice = None
            if current_task is not None:
                cancelled_existing_task = persisted is not None
                current_task = None
            break

    if active_group_tx is not None:
        rollback_group_savepoint(active_group_tx)
        outcomes = outcomes[:active_group_outcome_start]
        traces = traces[:active_group_trace_start]
        active_group_tx = None
        active_group_key = None

    persisted_after = _persist_final_task(
        db=db,
        workspace_id=workspace.id,
        conversation_id=conversation_id,
        patient_id=patient.id,
        run_id=run_id,
        initial=persisted,
        final_task=current_task,
        cancelled_existing_task=cancelled_existing_task,
        cancelled_existing_task_reason=cancelled_existing_task_reason,
        completed_existing_task_result=completed_existing_task_result,
    )

    if pending_write is not None:
        return V2OrchestratedTurn(
            understanding=understanding,
            plan=plan,
            traces=tuple(traces),
            outcomes=tuple(outcomes),
            reply=None,
            responder_model=None,
            active_task=current_task,
            persisted_task=persisted_after,
            pending_write=pending_write,
            pending_choice=outgoing_pending_choice,
        )

    if not outcomes:
        raise RuntimeError("V2 runtime produced neither a customer outcome nor a pending write.")

    reply, model = compose_v2_customer_reply(
        clinic_name=clinic_name,
        timezone_name=timezone_name,
        local_now=local_now,
        history=history,
        outcomes=outcomes,
    )
    return V2OrchestratedTurn(
        understanding=understanding,
        plan=plan,
        traces=tuple(traces),
        outcomes=tuple(outcomes),
        reply=reply,
        responder_model=model,
        active_task=current_task,
        persisted_task=persisted_after,
        pending_write=None,
        verified_action_context=completed_action_context,
        pending_choice=outgoing_pending_choice,
    )