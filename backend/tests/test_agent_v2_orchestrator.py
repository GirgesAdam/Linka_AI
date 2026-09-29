from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.services.agent_v2 import orchestrator as runtime
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.planner import PlanStep, ReadRequest, TurnPlan, WriteIntent
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import BookingTaskState, CustomerConstraints, WriteAuthorization
from app.services.agent_v2.state_executor import StateTransition
from app.services.agent_v2.state_persistence import PersistedActiveTask, V2StateConflictError

_NOW = datetime(2026, 9, 12, 1, 0, 0)


def _booking_task(*, status: str = "collecting", version: int = 1) -> BookingTaskState:
    return BookingTaskState(
        status=status,
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=_NOW,
        ),
        constraints=CustomerConstraints(service_id="service-1"),
        version=version,
    )


def _understanding(operation_type: str) -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[TurnOperation(type=operation_type, entities=TurnEntities())]
    )


def _runtime_args() -> dict[str, object]:
    return {
        "db": object(),
        "workspace": SimpleNamespace(id=uuid4()),
        "patient": SimpleNamespace(id=uuid4()),
        "conversation_id": uuid4(),
        "run_id": uuid4(),
        "history": [],
        "local_now": _NOW,
        "timezone_name": "Africa/Cairo",
        "clinic_name": "Linka Test Clinic",
        "catalog": {"fixture": True},
        "turn_id": "turn-1",
    }


def _patch_semantic_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    persisted: PersistedActiveTask | None,
    understanding: TiaTurnUnderstanding,
    plan: TurnPlan,
) -> None:
    semantic_context = object()
    monkeypatch.setattr(runtime, "load_active_task", lambda *args, **kwargs: persisted)
    monkeypatch.setattr(runtime, "build_semantic_context", lambda catalog: semantic_context)
    monkeypatch.setattr(
        runtime,
        "with_safe_task_context",
        lambda context, *, active_task: context,
    )
    monkeypatch.setattr(
        runtime,
        "interpret_customer_turn_v2",
        lambda **kwargs: understanding,
    )
    monkeypatch.setattr(runtime, "plan_turn", lambda turn, context: plan)
    monkeypatch.setattr(
        runtime,
        "adapt_matching_active_task_step",
        lambda step, **kwargs: step,
    )
    monkeypatch.setattr(
        runtime,
        "persist_initial_task_intent",
        lambda step, **kwargs: step,
    )


def test_write_ready_persists_once_and_never_renders_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    understanding = _understanding("book")
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            requires_verification=False,
        ),
        state_action="start_booking",
    )
    plan = TurnPlan(steps=[step])
    _patch_semantic_pipeline(
        monkeypatch,
        persisted=None,
        understanding=understanding,
        plan=plan,
    )

    ready_task = _booking_task(status="ready", version=4)
    monkeypatch.setattr(
        runtime,
        "apply_step_state",
        lambda *args, **kwargs: StateTransition(
            active_task=ready_task,
            changed=True,
            reason="start_booking",
        ),
    )
    monkeypatch.setattr(
        runtime,
        "finalize_step_after_state_transition",
        lambda planned_step, transition: planned_step,
    )
    monkeypatch.setattr(
        runtime,
        "build_step_outcome",
        lambda *args, **kwargs: pytest.fail("write_ready must not build a success outcome"),
    )
    monkeypatch.setattr(
        runtime,
        "compose_v2_customer_reply",
        lambda **kwargs: pytest.fail("write_ready must not render a customer success reply"),
    )

    save_calls: list[dict[str, object]] = []
    persisted_after = PersistedActiveTask(
        active_task=ready_task,
        flow_id=uuid4(),
        flow_version=7,
    )

    def save_once(*args: object, **kwargs: object) -> PersistedActiveTask:
        save_calls.append(dict(kwargs))
        return persisted_after

    monkeypatch.setattr(runtime, "save_active_task", save_once)
    monkeypatch.setattr(
        runtime,
        "cancel_active_task",
        lambda *args, **kwargs: pytest.fail("write_ready must not cancel state"),
    )

    result = runtime.orchestrate_v2_turn(**_runtime_args())

    assert result.pending_write is not None
    assert result.pending_write.step.disposition == "write_ready"
    assert result.reply is None
    assert result.responder_model is None
    assert result.outcomes == ()
    assert result.active_task == ready_task
    assert result.persisted_task == persisted_after
    assert len(save_calls) == 1
    assert save_calls[0]["expected"] is None
    assert save_calls[0]["active_task"].version == 4
    assert result.traces[0].pending_write is True


def test_unchanged_side_read_does_not_persist_task_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _booking_task(version=3)
    persisted = PersistedActiveTask(active_task=task, flow_id=uuid4(), flow_version=11)
    understanding = _understanding("social")
    step = PlanStep(
        operation_index=0,
        operation_type="social",
        disposition="respond",
        state_action="none",
        response_goal="social_ack",
    )
    _patch_semantic_pipeline(
        monkeypatch,
        persisted=persisted,
        understanding=understanding,
        plan=TurnPlan(steps=[step]),
    )
    monkeypatch.setattr(
        runtime,
        "apply_step_state",
        lambda *args, **kwargs: StateTransition(
            active_task=task,
            changed=False,
            reason="side_read_preserved",
        ),
    )
    monkeypatch.setattr(
        runtime,
        "finalize_step_after_state_transition",
        lambda planned_step, transition: planned_step,
    )
    outcome = TurnOutcome(status="answered", response_goal="social_ack")
    monkeypatch.setattr(runtime, "build_step_outcome", lambda *args, **kwargs: outcome)
    monkeypatch.setattr(
        runtime,
        "compose_v2_customer_reply",
        lambda **kwargs: ("أهلًا بيك", "test-model"),
    )
    monkeypatch.setattr(
        runtime,
        "save_active_task",
        lambda *args, **kwargs: pytest.fail("unchanged state must not be saved again"),
    )
    monkeypatch.setattr(
        runtime,
        "cancel_active_task",
        lambda *args, **kwargs: pytest.fail("side read must not cancel active state"),
    )

    result = runtime.orchestrate_v2_turn(**_runtime_args())

    assert result.active_task == task
    assert result.persisted_task == persisted
    assert result.pending_write is None
    assert result.reply == "أهلًا بيك"
    assert result.outcomes == (outcome,)


def test_explicit_cancel_closes_persisted_task_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _booking_task(version=5)
    persisted = PersistedActiveTask(active_task=task, flow_id=uuid4(), flow_version=13)
    understanding = _understanding("cancel_active")
    step = PlanStep(
        operation_index=0,
        operation_type="cancel_active",
        disposition="state_update",
        state_action="cancel_active",
        response_goal="clarification",
    )
    _patch_semantic_pipeline(
        monkeypatch,
        persisted=persisted,
        understanding=understanding,
        plan=TurnPlan(steps=[step]),
    )
    monkeypatch.setattr(
        runtime,
        "apply_step_state",
        lambda *args, **kwargs: StateTransition(
            active_task=None,
            changed=True,
            reason="cancel_active",
        ),
    )
    cancelled_step = step.model_copy(
        update={
            "response_goal": "active_task_cancelled",
            "facts": {"active_task_cancelled": True},
        }
    )
    monkeypatch.setattr(
        runtime,
        "finalize_step_after_state_transition",
        lambda planned_step, transition: cancelled_step,
    )
    outcome = TurnOutcome(status="answered", response_goal="active_task_cancelled")
    monkeypatch.setattr(runtime, "build_step_outcome", lambda *args, **kwargs: outcome)
    monkeypatch.setattr(
        runtime,
        "compose_v2_customer_reply",
        lambda **kwargs: ("تمام، ألغيت الطلب الحالي.", "test-model"),
    )
    monkeypatch.setattr(
        runtime,
        "save_active_task",
        lambda *args, **kwargs: pytest.fail("cancelled task must not be saved"),
    )

    cancel_calls: list[dict[str, object]] = []

    def cancel_once(*args: object, **kwargs: object) -> None:
        cancel_calls.append(dict(kwargs))

    monkeypatch.setattr(runtime, "cancel_active_task", cancel_once)

    result = runtime.orchestrate_v2_turn(**_runtime_args())

    assert result.active_task is None
    assert result.persisted_task is None
    assert result.pending_write is None
    assert result.reply == "تمام، ألغيت الطلب الحالي."
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["expected"] == persisted


def test_stale_persistence_conflict_propagates_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    understanding = _understanding("book")
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            requires_verification=False,
        ),
        state_action="start_booking",
    )
    _patch_semantic_pipeline(
        monkeypatch,
        persisted=None,
        understanding=understanding,
        plan=TurnPlan(steps=[step]),
    )
    ready_task = _booking_task(status="ready", version=2)
    monkeypatch.setattr(
        runtime,
        "apply_step_state",
        lambda *args, **kwargs: StateTransition(
            active_task=ready_task,
            changed=True,
            reason="start_booking",
        ),
    )
    monkeypatch.setattr(
        runtime,
        "finalize_step_after_state_transition",
        lambda planned_step, transition: planned_step,
    )
    monkeypatch.setattr(
        runtime,
        "compose_v2_customer_reply",
        lambda **kwargs: pytest.fail("conflicted state must never render a reply"),
    )

    attempts = 0

    def stale_save(*args: object, **kwargs: object) -> PersistedActiveTask:
        nonlocal attempts
        attempts += 1
        raise V2StateConflictError("stale flow version")

    monkeypatch.setattr(runtime, "save_active_task", stale_save)

    with pytest.raises(V2StateConflictError, match="stale flow version"):
        runtime.orchestrate_v2_turn(**_runtime_args())

    assert attempts == 1


def test_mixed_safe_read_and_ordinary_handoff_both_reach_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="pulse_info",
                entities=TurnEntities(),
                requested_pulse_details=["balance"],
                execution_intent="informational",
            ),
            TurnOperation(type="human_support", entities=TurnEntities()),
        ]
    )
    read_step = PlanStep(
        operation_index=0,
        operation_type="pulse_info",
        disposition="read",
        reads=[ReadRequest(kind="pulse_balance")],
        response_goal="pulse_information",
    )
    handoff_step = PlanStep(
        operation_index=1,
        operation_type="human_support",
        disposition="handoff",
        response_goal="handoff",
        facts={"category": "customer_request", "priority": "normal"},
    )
    plan = TurnPlan(
        steps=[read_step, handoff_step],
        handoff_category="customer_request",
        handoff_priority="normal",
    )
    _patch_semantic_pipeline(
        monkeypatch,
        persisted=None,
        understanding=understanding,
        plan=plan,
    )
    monkeypatch.setattr(
        runtime,
        "preflight_compound_visit_plan",
        lambda plan, **kwargs: plan,
    )

    read_calls: list[str] = []

    def execute_reads(step, _context):
        read_calls.extend(item.kind for item in step.reads)
        return ReadExecutionBundle(
            results=[
                ReadResult(
                    kind="pulse_balance",
                    ok=True,
                    payload={"balances": [{"device_key": "candela_gentle", "pulses_remaining": 500}]},
                )
            ]
        )

    monkeypatch.setattr(runtime, "execute_step_reads", execute_reads)
    monkeypatch.setattr(
        runtime,
        "apply_step_state",
        lambda active_task, **kwargs: StateTransition(
            active_task=active_task,
            changed=False,
            reason="read_or_handoff",
        ),
    )
    monkeypatch.setattr(
        runtime,
        "finalize_step_after_state_transition",
        lambda planned_step, transition: planned_step,
    )

    read_outcome = TurnOutcome(
        status="answered",
        response_goal="pulse_information",
        facts={"pulse_balances": [{"device_key": "candela_gentle", "pulses_remaining": 500}]},
    )
    handoff_outcome = TurnOutcome(
        status="handoff",
        response_goal="handoff",
        facts={"category": "customer_request", "priority": "normal"},
    )

    def build_outcome(step, **kwargs):
        return handoff_outcome if step.disposition == "handoff" else read_outcome

    monkeypatch.setattr(runtime, "build_step_outcome", build_outcome)
    monkeypatch.setattr(
        runtime,
        "compose_v2_customer_reply",
        lambda **kwargs: (
            "متبقي 500 Pulse، وتفاصيل الحساب يأكدها الاستقبال.",
            "test-model",
        ),
    )

    result = runtime.orchestrate_v2_turn(**_runtime_args())

    assert read_calls == ["pulse_balance"]
    assert result.outcomes == (read_outcome, handoff_outcome)
    assert result.reply == "متبقي 500 Pulse، وتفاصيل الحساب يأكدها الاستقبال."
    assert sum(outcome.status == "handoff" for outcome in result.outcomes) == 1


def test_orchestrator_source_keeps_write_and_transaction_boundaries() -> None:
    source = runtime.__file__
    assert source is not None
    text = open(source, encoding="utf-8").read()

    assert ".commit(" not in text
    assert "complete_state_after_action" not in text
    assert "execute_booking" not in text
    assert "execute_reschedule" not in text
    assert "re.compile(" not in text
    assert "import re" not in text


def _availability_bundle(count: int, *, ok: bool = True) -> ReadExecutionBundle:
    if not ok:
        return ReadExecutionBundle(
            results=[
                ReadResult(
                    kind="availability",
                    ok=False,
                    error_code="provider_error",
                )
            ]
        )
    slots = [
        {
            "start_local": f"2026-09-17T{18 + index:02d}:00:00+03:00",
        }
        for index in range(count)
    ]
    return ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=True,
                payload={
                    "slots": slots,
                    "matching_slot_count": count,
                    "checked_dates": ["2026-09-17"],
                },
            )
        ]
    )


def _conditional_availability_understanding(
    *,
    fallback_condition: str = "if_previous_no_availability",
    include_independent: bool = False,
) -> TiaTurnUnderstanding:
    operations = [
        TurnOperation(
            type="availability",
            entities=TurnEntities(),
            continuation_condition="always",
        ),
        TurnOperation(
            type="availability",
            entities=TurnEntities(),
            continuation_condition=fallback_condition,
        ),
    ]
    if include_independent:
        operations.append(TurnOperation(type="clinic_info", entities=TurnEntities()))
    return TiaTurnUnderstanding(operations=operations)


def _conditional_availability_plan(*, include_independent: bool = False) -> TurnPlan:
    steps = [
        PlanStep(
            operation_index=0,
            operation_type="availability",
            disposition="read",
            reads=[ReadRequest(kind="availability")],
            response_goal="present_availability",
        ),
        PlanStep(
            operation_index=1,
            operation_type="availability",
            disposition="read",
            reads=[ReadRequest(kind="availability")],
            response_goal="present_availability",
        ),
    ]
    if include_independent:
        steps.append(
            PlanStep(
                operation_index=2,
                operation_type="clinic_info",
                disposition="read",
                reads=[ReadRequest(kind="clinic_info")],
                response_goal="answer_clinic_info",
            )
        )
    return TurnPlan(steps=steps)


def _run_conditional_availability(
    monkeypatch: pytest.MonkeyPatch,
    *,
    first: ReadExecutionBundle,
    fallback: ReadExecutionBundle | None,
    fallback_condition: str = "if_previous_no_availability",
    include_independent: bool = False,
):
    understanding = _conditional_availability_understanding(
        fallback_condition=fallback_condition,
        include_independent=include_independent,
    )
    plan = _conditional_availability_plan(include_independent=include_independent)
    _patch_semantic_pipeline(
        monkeypatch,
        persisted=None,
        understanding=understanding,
        plan=plan,
    )

    read_calls: list[int] = []
    outcome_calls: list[int] = []

    def execute_reads(step, _context):
        read_calls.append(step.operation_index)
        if step.operation_index == 0:
            return first
        if step.operation_index == 1:
            assert fallback is not None
            return fallback
        return ReadExecutionBundle(
            results=[
                ReadResult(
                    kind="clinic_info",
                    ok=True,
                    payload={"clinic": {"name": "Linka Test Clinic"}},
                )
            ]
        )

    monkeypatch.setattr(runtime, "execute_step_reads", execute_reads)
    monkeypatch.setattr(
        runtime,
        "apply_step_state",
        lambda *args, **kwargs: StateTransition(
            active_task=None,
            changed=False,
            reason="read_only",
        ),
    )
    monkeypatch.setattr(
        runtime,
        "finalize_step_after_state_transition",
        lambda planned_step, transition: planned_step,
    )

    def build_outcome(step, *, reads=None, **kwargs):
        outcome_calls.append(step.operation_index)
        if step.operation_type == "availability":
            result = next(result for result in reads.results if result.kind == "availability")
            count = result.payload.get("matching_slot_count") if result.ok else None
            return TurnOutcome(
                status="answered",
                response_goal="present_availability",
                facts={
                    "availability": {
                        "available_option_count": count,
                        "read_ok": result.ok,
                    }
                },
            )
        return TurnOutcome(
            status="answered",
            response_goal="answer_clinic_info",
            facts={"clinic_name": "Linka Test Clinic"},
        )

    monkeypatch.setattr(runtime, "build_step_outcome", build_outcome)
    monkeypatch.setattr(
        runtime,
        "compose_v2_customer_reply",
        lambda **kwargs: ("grounded reply", "test-model"),
    )

    result = runtime.orchestrate_v2_turn(**_runtime_args())
    return result, read_calls, outcome_calls


def test_conditional_fallback_skipped_when_primary_availability_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, read_calls, outcome_calls = _run_conditional_availability(
        monkeypatch,
        first=_availability_bundle(1),
        fallback=_availability_bundle(1),
    )

    assert read_calls == [0]
    assert outcome_calls == [0]
    assert len(result.outcomes) == 1
    assert result.traces[1].operation_index == 1
    assert result.traces[1].skipped is True
    assert result.traces[1].disposition_after == "skipped"
    assert result.traces[1].skip_reason == (
        "continuation_condition_false:if_previous_no_availability"
    )


def test_conditional_fallback_executes_after_verified_no_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, read_calls, outcome_calls = _run_conditional_availability(
        monkeypatch,
        first=_availability_bundle(0),
        fallback=_availability_bundle(1),
    )

    assert read_calls == [0, 1]
    assert outcome_calls == [0, 1]
    assert len(result.outcomes) == 2
    assert all(trace.skipped is False for trace in result.traces)


def test_conditional_fallback_executes_when_both_windows_have_no_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, read_calls, outcome_calls = _run_conditional_availability(
        monkeypatch,
        first=_availability_bundle(0),
        fallback=_availability_bundle(0),
    )

    assert read_calls == [0, 1]
    assert outcome_calls == [0, 1]
    assert len(result.outcomes) == 2


def test_unconditional_multi_availability_reads_both_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, read_calls, outcome_calls = _run_conditional_availability(
        monkeypatch,
        first=_availability_bundle(1),
        fallback=_availability_bundle(1),
        fallback_condition="always",
    )

    assert read_calls == [0, 1]
    assert outcome_calls == [0, 1]
    assert len(result.outcomes) == 2
    assert not any(trace.skipped for trace in result.traces)


def test_technical_availability_failure_does_not_activate_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, read_calls, outcome_calls = _run_conditional_availability(
        monkeypatch,
        first=_availability_bundle(0, ok=False),
        fallback=_availability_bundle(1),
    )

    assert read_calls == [0]
    assert outcome_calls == [0]
    assert len(result.outcomes) == 1
    assert result.traces[1].skipped is True


def test_skipped_conditional_operation_does_not_drop_independent_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, read_calls, outcome_calls = _run_conditional_availability(
        monkeypatch,
        first=_availability_bundle(1),
        fallback=_availability_bundle(1),
        include_independent=True,
    )

    assert read_calls == [0, 2]
    assert outcome_calls == [0, 2]
    assert len(result.outcomes) == 2
    assert result.traces[1].operation_index == 1
    assert result.traces[1].skipped is True
    assert result.traces[2].operation_index == 2
    assert result.traces[2].skipped is False


def test_doctor_compatibility_failure_preserves_booking_context_and_clears_doctor() -> None:
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        reads=[ReadRequest(kind="availability")],
        write_intent=WriteIntent(kind="booking", authorized=True, parameters={}),
        state_action="none",
        response_goal="present_availability",
        facts={
            "service_id": "service-1",
            "doctor_id": "doctor-bad",
            "date": {"mode": "exact", "start_date": "2026-09-30"},
            "time": {"mode": "exact", "start_time": "14:00"},
        },
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=False,
                error_code="doctor_service_incompatible",
                payload={
                    "compatibility_failure": {
                        "dimension": "doctor",
                        "service_name": "Hydrafacial",
                        "requested_name": "Doctor Bad",
                        "compatible_options": ["Doctor Good"],
                    }
                },
            )
        ]
    )

    advanced = runtime._compatibility_failure_step(step, reads)

    assert advanced is not None
    assert advanced.disposition == "clarify"
    assert advanced.clarification_field == "doctor"
    assert advanced.state_action == "start_booking"
    assert advanced.write_intent is None
    assert advanced.facts["service_id"] == "service-1"
    assert advanced.facts["date"] == {"mode": "exact", "start_date": "2026-09-30"}
    assert advanced.facts["time"] == {"mode": "exact", "start_time": "14:00"}
    assert advanced.facts["doctor_id"] is None


def test_device_compatibility_failure_preserves_booking_context_and_clears_device() -> None:
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        reads=[ReadRequest(kind="availability")],
        write_intent=WriteIntent(kind="booking", authorized=True, parameters={}),
        state_action="update_active",
        response_goal="present_availability",
        facts={
            "service_id": "service-1",
            "doctor_id": "doctor-1",
            "device_key": "device-bad",
            "date": {"mode": "exact", "start_date": "2026-09-30"},
            "time": {"mode": "exact", "start_time": "14:00"},
        },
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=False,
                error_code="device_service_incompatible",
                payload={
                    "compatibility_failure": {
                        "dimension": "device",
                        "service_name": "Laser",
                        "requested_name": "Device Bad",
                        "compatible_options": ["Device Good"],
                    }
                },
            )
        ]
    )

    advanced = runtime._compatibility_failure_step(step, reads)

    assert advanced is not None
    assert advanced.disposition == "clarify"
    assert advanced.clarification_field == "device"
    assert advanced.state_action == "update_active"
    assert advanced.write_intent is None
    assert advanced.facts["service_id"] == "service-1"
    assert advanced.facts["doctor_id"] == "doctor-1"
    assert advanced.facts["device_key"] is None
