from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage

from app.agents.structured_output import StructuredOutputError
from app.agents.v2.availability_reference_interpreter import (
    ReferenceDecision,
    ReferenceInterpretation,
)
from app.agents.v2.turn_contract import (
    DateConstraint,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2 import orchestrator as runtime
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.planner import (
    PlanStep,
    ReadRequest,
    TurnPlan,
    VerificationFacts,
    WriteIntent,
)
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    OptionChoice,
    OptionSnapshot,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import StateTransition
from app.services.agent_v2.state_persistence import PersistedActiveTask, V2StateConflictError
from app.services.agent_v2.test_harness import V2FixtureEnvironment

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



def test_structured_interpretation_failure_never_reaches_planner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    understanding = _understanding("social")
    _patch_semantic_pipeline(
        monkeypatch,
        persisted=None,
        understanding=understanding,
        plan=TurnPlan(),
    )

    def fail_interpreter(**_kwargs):
        raise StructuredOutputError("invalid turn semantics")

    monkeypatch.setattr(runtime, "interpret_customer_turn_v2", fail_interpreter)
    monkeypatch.setattr(
        runtime,
        "plan_turn",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("planner must not run")),
    )

    with pytest.raises(runtime.V2TurnInterpretationStructuredOutputError):
        runtime.orchestrate_v2_turn(**_runtime_args())


def test_pricing_side_read_preserves_existing_pending_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _booking_task(version=3)
    persisted = PersistedActiveTask(active_task=task, flow_id=uuid4(), flow_version=11)
    understanding = TiaTurnUnderstanding(operations=[TurnOperation(type="pricing", entities=TurnEntities(), execution_intent="informational")])
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="respond",
        state_action="none",
        response_goal="answer_price",
    )
    _patch_semantic_pipeline(
        monkeypatch,
        persisted=persisted,
        understanding=understanding,
        plan=TurnPlan(steps=[step]),
    )
    monkeypatch.setattr(runtime, "with_safe_task_context", lambda context, **kwargs: context)
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
    outcome = TurnOutcome(status="answered", response_goal="answer_price")
    monkeypatch.setattr(runtime, "build_step_outcome", lambda *args, **kwargs: outcome)
    monkeypatch.setattr(
        runtime,
        "compose_v2_customer_reply",
        lambda **kwargs: ("price reply", "test-model"),
    )
    monkeypatch.setattr(runtime, "save_active_task", lambda *a, **k: pytest.fail("no save"))
    monkeypatch.setattr(runtime, "cancel_active_task", lambda *a, **k: pytest.fail("no cancel"))

    pending = OptionSnapshot(
        snapshot_id="appointment-choice",
        purpose="appointment",
        lifecycle_action="reschedule",
        task_version=1,
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=15),
        options=[OptionChoice(ref="A1", payload={"appointment_id": "appointment-1"})],
    )
    args = _runtime_args()
    args["pending_choice_context"] = pending.model_dump(mode="json")
    result = runtime.orchestrate_v2_turn(**args)

    assert result.active_task == task
    assert result.pending_choice == pending


def test_availability_update_does_not_carry_unrelated_pending_choice() -> None:
    assert runtime._is_pure_informational_side_question(TiaTurnUnderstanding(operations=[TurnOperation(type="pricing", entities=TurnEntities(), execution_intent="informational")])) is True
    assert runtime._is_pure_informational_side_question(TiaTurnUnderstanding(operations=[TurnOperation(type="clinic_info", entities=TurnEntities(), execution_intent="informational")])) is True
    assert runtime._is_pure_informational_side_question(_understanding("availability")) is False
    assert runtime._is_pure_informational_side_question(_understanding("book")) is False



def test_h3b_grouped_time_reference_uses_visit_level_orchestrator_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = BookingTaskState(
        status="awaiting_choice",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id="service-b", branch_id="branch-1"),
        requested_service_ids=["service-a", "service-b"],
        option_snapshot=OptionSnapshot(
            snapshot_id="old-bikini-only",
            purpose="booking_slot",
            task_version=2,
            created_at=_NOW,
            expires_at=_NOW + timedelta(minutes=15),
            options=[
                OptionChoice(
                    ref="slot-old",
                    label="20:00",
                    payload={
                        "service_id": "service-a",
                        "branch_id": "branch-1",
                        "doctor_id": "doctor-1",
                        "device_key": "device-1",
                        "laser_device_key": "device-1",
                        "start_local": "2026-09-12T20:00:00+03:00",
                        "start_time_24h": "20:00",
                    },
                )
            ],
        ),
        version=2,
    )
    persisted = PersistedActiveTask(active_task=task, flow_id=uuid4(), flow_version=4)
    catalog = {
        "services": [
            {
                "id": "service-a",
                "name": "Service A",
                "operational_category": "laser",
                "requires_laser_device": True,
                "duration_minutes": 15,
                "laser_devices": [
                    {
                        "device_key": "device-1",
                        "device_name": "Device 1",
                        "price_minor": 50_000,
                        "duration_minutes": 15,
                        "configured": True,
                    }
                ],
            },
            {
                "id": "service-b",
                "name": "Service B",
                "operational_category": "laser",
                "requires_laser_device": True,
                "duration_minutes": 20,
                "laser_devices": [
                    {
                        "device_key": "device-1",
                        "device_name": "Device 1",
                        "price_minor": 70_000,
                        "duration_minutes": 20,
                        "configured": True,
                    }
                ],
            },
        ],
        "doctors": [
            {
                "id": "doctor-1",
                "name": "Doctor 1",
                "service_ids": ["service-a", "service-b"],
                "branch_ids": ["branch-1"],
            }
        ],
        "branches": [{"id": "branch-1", "name": "Clinic"}],
        "appointments": [],
    }
    reference_context = {
        "operation_type": "availability",
        "requested_service_ids": ["service-a", "service-b"],
        "multi_service_single_appointment": True,
        "date": {"mode": "exact", "start_date": "2026-09-12", "end_date": None},
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "index": 1,
                "concrete": True,
                "service_id": "service-a",
                "start_time_24h": "20:00",
                "start_local": "2026-09-12T20:00:00+03:00",
                "slot": {
                    "service_id": "service-a",
                    "branch_id": "branch-1",
                    "doctor_id": "doctor-1",
                    "laser_device_key": "device-1",
                    "start_local": "2026-09-12T20:00:00+03:00",
                },
            },
            {
                "option_ref": "opt_2",
                "index": 2,
                "concrete": True,
                "service_id": "service-b",
                "start_time_24h": "20:00",
                "start_local": "2026-09-12T20:00:00+03:00",
                "slot": {
                    "service_id": "service-b",
                    "branch_id": "branch-1",
                    "doctor_id": "doctor-1",
                    "laser_device_key": "device-1",
                    "start_local": "2026-09-12T20:00:00+03:00",
                    "start_time_24h": "20:00",
                },
            },
        ],
    }
    monkeypatch.setattr(runtime, "load_active_task", lambda *args, **kwargs: persisted)
    monkeypatch.setattr(
        runtime,
        "interpret_availability_reference_turn",
        lambda **kwargs: ReferenceInterpretation(
            decision=ReferenceDecision(
                action="select_presented_option",
                option_ref="opt_1",
                exact_time=None,
                selection_scope="none",
            ),
            model_name="deterministic-test",
        ),
    )
    monkeypatch.setattr(
        runtime,
        "interpret_customer_turn_v2",
        lambda **kwargs: pytest.fail("grouped reference selection must not call the full interpreter"),
    )
    captured: list[TurnPlan] = []

    def capture_preflight(plan: TurnPlan, **_kwargs: object) -> TurnPlan:
        captured.append(plan)
        return plan

    monkeypatch.setattr(runtime, "preflight_compound_visit_plan", capture_preflight)
    fresh_slot = {
        "service_id": "service-b",
        "branch_id": "branch-1",
        "doctor_id": "doctor-1",
        "laser_device_key": "device-1",
        "start_at": "2026-09-12T20:00:00+03:00",
        "end_at": "2026-09-12T20:20:00+03:00",
        "start_local": "2026-09-12T20:00:00+03:00",
        "start_time_24h": "20:00",
        "doctor_name": "Doctor 1",
        "laser_device_name": "Device 1",
    }
    monkeypatch.setattr(
        runtime,
        "execute_step_reads",
        lambda *args, **kwargs: ReadExecutionBundle(
            results=[ReadResult(kind="availability", ok=True, payload={"slots": [fresh_slot]})],
            verification=VerificationFacts(
                exact_slot_match_count=1,
                verified_parameters={
                    "service_id": "service-b",
                    "branch_id": "branch-1",
                    "doctor_id": "doctor-1",
                    "device_key": "device-1",
                    "start_at": "2026-09-12T20:00:00+03:00",
                },
            ),
        ),
    )
    monkeypatch.setattr(runtime, "resolve_additional_services", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        runtime,
        "compose_v2_customer_reply",
        lambda **kwargs: ("visit-level selection", "deterministic-test"),
    )
    saved: list[BookingTaskState] = []

    def save_task(*args: object, **kwargs: object) -> PersistedActiveTask:
        active = kwargs["active_task"]
        assert isinstance(active, BookingTaskState)
        saved.append(active)
        return PersistedActiveTask(active_task=active, flow_id=persisted.flow_id, flow_version=5)

    monkeypatch.setattr(runtime, "save_active_task", save_task)

    args = _runtime_args()
    args.update(
        {
            "catalog": catalog,
            "history": [HumanMessage(content="?????? 8 ???? ??????")],
            "availability_reference_context": reference_context,
        }
    )
    result = runtime.orchestrate_v2_turn(**args)

    assert captured
    preflight_plan = captured[0]
    assert len(preflight_plan.steps) == 1
    step = preflight_plan.steps[0]
    assert step.facts["requested_service_ids"] == ["service-a", "service-b"]
    assert step.facts["doctor_id"] == "doctor-1"
    assert step.facts["device_key"] == "device-1"
    assert step.write_intent is not None
    assert step.write_intent.kind == "booking"
    assert step.write_intent.authorized is True
    assert step.write_intent.requires_verification is True
    assert step.facts["booking_proposal_required"] is True
    assert step.facts["verified_booking_slot_selection"] is True
    assert step.clarification_field != "service"
    assert step.response_goal != "answer_price"
    assert any(read.kind == "availability" for read in step.reads)
    assert result.pending_write is None
    assert result.reference_action == "select_presented_option"
    assert result.selected_option_ref == "opt_1"
    assert result.full_interpreter_called is False
    assert isinstance(result.active_task, BookingTaskState)
    assert result.active_task.requested_service_ids == ["service-a", "service-b"]
    assert result.active_task.status == "awaiting_confirmation"
    assert result.active_task.derived.selected_slot_ref == "opt_1"
    assert result.active_task.proposal is not None
    assert result.active_task.proposal.service_ids == ["service-a", "service-b"]
    assert result.active_task.proposal.doctor_id == "doctor-1"
    assert result.active_task.proposal.device_key == "device-1"
    assert result.active_task.proposal.start_at.isoformat() == "2026-09-12T20:00:00+03:00"
    assert result.active_task.constraints.doctor_id == "doctor-1"
    assert result.active_task.constraints.device_key == "device-1"
    assert result.active_task.constraints.date is not None
    assert result.active_task.constraints.date.start_date == "2026-09-12"
    assert result.active_task.constraints.time is not None
    assert result.active_task.constraints.time.start_time == "20:00"
    assert result.active_task.write_authorization.authorized is False
    assert result.outcomes[0].response_goal == "booking_proposal"
    assert saved and saved[-1].requested_service_ids == ["service-a", "service-b"]





def _run_ra_g6_unpresented_exact_time_case(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exact_time_requested: bool = True,
    continuation_available: bool = True,
    device_auto_resolved: bool = False,
    stale_reference_selection: bool = False,
    narrow_exact_time: str | None = None,
):
    task = BookingTaskState(
        status="awaiting_slot",
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-initial",
            granted_at=_NOW,
        ),
        constraints=CustomerConstraints(
            service_id="service-a",
            doctor_id="doctor-1",
            branch_id="branch-1",
            device_key="device-1",
            date=DateConstraint(mode="exact", start_date="2026-09-12"),
            time=TimeConstraint(mode="after", start_time="10:00"),
        ),
        requested_service_ids=["service-a", "service-b"],
        service_device_keys={"service-a": "device-1", "service-b": "device-1"},
        option_snapshot=OptionSnapshot(
            snapshot_id="slot-10",
            purpose="booking_slot",
            task_version=2,
            created_at=_NOW,
            expires_at=_NOW + timedelta(minutes=15),
            options=[
                OptionChoice(
                    ref="opt_10",
                    label="10:00",
                    payload={
                        "service_id": "service-a",
                        "branch_id": "branch-1",
                        "doctor_id": "doctor-1",
                        "device_key": "device-1",
                        "laser_device_key": "device-1",
                        "start_local": "2026-09-12T10:00:00+03:00",
                        "start_time_24h": "10:00",
                    },
                )
            ],
        ),
        version=2,
    )
    persisted = PersistedActiveTask(active_task=task, flow_id=uuid4(), flow_version=4)
    catalog = {
        "services": [
            {
                "id": "service-a",
                "name": "Service A",
                "operational_category": "laser",
                "requires_laser_device": True,
                "duration_minutes": 25,
                "laser_devices": [{
                    "device_key": "device-1",
                    "device_name": "Device 1",
                    "price_minor": 50_000,
                    "duration_minutes": 25,
                    "configured": True,
                }],
            },
            {
                "id": "service-b",
                "name": "Service B",
                "operational_category": "laser",
                "requires_laser_device": True,
                "duration_minutes": 20,
                "laser_devices": [{
                    "device_key": "device-1",
                    "device_name": "Device 1",
                    "price_minor": 70_000,
                    "duration_minutes": 20,
                    "configured": True,
                }],
            },
        ],
        "doctors": [{
            "id": "doctor-1",
            "name": "Doctor 1",
            "service_ids": ["service-a", "service-b"],
            "branch_ids": ["branch-1"],
        }],
        "branches": [{"id": "branch-1", "name": "Clinic"}],
        "appointments": [],
    }
    reference_context = {
        "operation_type": "availability",
        "requested_service_ids": ["service-a", "service-b"],
        "multi_service_single_appointment": True,
        "date": {"mode": "exact", "start_date": "2026-09-12", "end_date": None},
        "availability_reference_options": [{
            "option_ref": "opt_10",
            "index": 1,
            "concrete": True,
            "service_id": "service-a",
            "start_time_24h": "10:00",
            "start_local": "2026-09-12T10:00:00+03:00",
            "slot": {
                "service_id": "service-a",
                "branch_id": "branch-1",
                "doctor_id": "doctor-1",
                "laser_device_key": "device-1",
                "start_local": "2026-09-12T10:00:00+03:00",
                "start_time_24h": "10:00",
            },
        }],
    }
    monkeypatch.setattr(runtime, "load_active_task", lambda *args, **kwargs: persisted)
    monkeypatch.setattr(
        runtime,
        "interpret_availability_reference_turn",
        lambda **kwargs: ReferenceInterpretation(
            decision=ReferenceDecision(
                action=("select_presented_option" if stale_reference_selection else "new_search"),
                option_ref="stale-option" if stale_reference_selection else None,
                exact_time=narrow_exact_time,
                selection_scope="none",
            ),
            model_name="deterministic-test",
        ),
    )
    interpreted = TiaTurnUnderstanding(
        operations=[TurnOperation(
            type="continue_active",
            entities=TurnEntities(time=TimeConstraint(
                mode="exact" if exact_time_requested else "after",
                start_time="20:00",
                start_time_ambiguity="none",
            )),
            execution_intent="execute",
            active_task_relationship="continue",
            continues_previous=True,
            grouped_booking_action="preserve_group",
        )]
    )
    monkeypatch.setattr(runtime, "interpret_customer_turn_v2", lambda **kwargs: interpreted)
    commercial_facts = {
        "service_id": "service-a",
        "requested_service_ids": ["service-a", "service-b"],
        "doctor_id": "doctor-1",
        "branch_id": "branch-1",
        "device_key": "device-1",
        "date": {"mode": "exact", "start_date": "2026-09-12", "end_date": None},
        "time": {
            "mode": "exact" if exact_time_requested else "after",
            "start_time": "20:00",
            "end_time": None,
        },
        "multi_service_single_appointment": True,
        "booking_commercial_basis_step": True,
        "commercial_basis_key": "service-a|device-1|standalone",
        "booking_next_field": "booking",
        "exact_time_requested": exact_time_requested,
    }
    if device_auto_resolved:
        commercial_facts["device_auto_resolved"] = True
    commercial_step = PlanStep(
        operation_index=0,
        operation_type="continue_active",
        disposition="read",
        reads=[
            ReadRequest(kind="service_catalog", parameters={"service_id": "service-a", "device_key": "device-1"}),
            ReadRequest(kind="availability", parameters={
                "service_id": "service-a",
                "branch_id": "branch-1",
                "doctor_id": "doctor-1",
                "device_key": "device-1",
                "date": {"mode": "exact", "start_date": "2026-09-12", "end_date": None},
                "time": {"mode": "exact", "start_time": "20:00", "end_time": None},
            }),
        ],
        state_action="update_active",
        response_goal="answer_price",
        facts=commercial_facts,
    )
    monkeypatch.setattr(runtime, "plan_turn", lambda *args, **kwargs: TurnPlan(steps=[commercial_step]))
    if device_auto_resolved:
        real_progress = runtime.plan_active_task_progress
        progress_calls = 0

        def progress_with_auto_resolve(*args: object, **kwargs: object) -> PlanStep:
            nonlocal progress_calls
            progress_calls += 1
            if progress_calls == 1:
                return commercial_step
            return real_progress(*args, **kwargs)

        monkeypatch.setattr(runtime, "plan_active_task_progress", progress_with_auto_resolve)
    monkeypatch.setattr(runtime, "expand_multi_service_operations", lambda turn, **kwargs: (turn, {}))
    monkeypatch.setattr(runtime, "restore_grouped_booking_understanding", lambda turn, **kwargs: (turn, {}))
    monkeypatch.setattr(runtime, "preflight_compound_visit_plan", lambda plan, **kwargs: plan)
    monkeypatch.setattr(runtime, "adapt_matching_active_task_step", lambda step, **kwargs: step)
    monkeypatch.setattr(runtime, "persist_initial_task_intent", lambda step, **kwargs: step)
    monkeypatch.setattr(runtime, "resolve_additional_services", lambda *args, **kwargs: [])
    monkeypatch.setattr(runtime, "compose_v2_customer_reply", lambda **kwargs: ("proposal reply", "deterministic-test"))

    slot = {
        "service_id": "service-a",
        "branch_id": "branch-1",
        "doctor_id": "doctor-1",
        "laser_device_key": "device-1",
        "start_at": "2026-09-12T20:00:00+03:00",
        "end_at": "2026-09-12T20:20:00+03:00",
        "start_local": "2026-09-12T20:00:00+03:00",
        "start_time_24h": "20:00",
        "doctor_name": "Doctor 1",
        "laser_device_name": "Device 1",
    }
    read_calls: list[list[str]] = []

    def execute_reads(step: PlanStep, *_args: object, **_kwargs: object) -> ReadExecutionBundle:
        kinds = [request.kind for request in step.reads]
        read_calls.append(kinds)
        if any(kind in {"service_catalog", "service_catalog_set"} for kind in kinds):
            catalog_kind = (
                "service_catalog_set" if "service_catalog_set" in kinds else "service_catalog"
            )
            catalog_payload = (
                {
                    "variants": [
                        {"device_name": "Device 1", "total_price_minor": 100_000, "currency": "EGP"}
                    ]
                }
                if catalog_kind == "service_catalog_set"
                else {"services": []}
            )
            available_slots = [slot] if len(read_calls) == 1 or continuation_available else []
            return ReadExecutionBundle(
                results=[
                    ReadResult(kind=catalog_kind, ok=True, payload=catalog_payload),
                    ReadResult(kind="availability", ok=True, payload={"slots": available_slots}),
                ],
                verification=VerificationFacts(
                    exact_slot_match_count=len(available_slots),
                    verified_parameters=(
                        {
                            "service_id": "service-a",
                            "branch_id": "branch-1",
                            "doctor_id": "doctor-1",
                            "device_key": "device-1",
                            "start_at": "2026-09-12T20:00:00+03:00",
                        }
                        if available_slots
                        else {}
                    ),
                ),
            )
        continuation_slots = [slot] if continuation_available else []
        return ReadExecutionBundle(
            results=[ReadResult(kind="availability", ok=True, payload={"slots": continuation_slots})],
            verification=VerificationFacts(
                exact_slot_match_count=len(continuation_slots),
                verified_parameters=(
                    {
                        "service_id": "service-a",
                        "branch_id": "branch-1",
                        "doctor_id": "doctor-1",
                        "device_key": "device-1",
                        "start_at": "2026-09-12T20:00:00+03:00",
                    }
                    if continuation_slots else {}
                ),
            ),
        )

    monkeypatch.setattr(runtime, "execute_step_reads", execute_reads)
    saved: list[BookingTaskState] = []

    def save_task(*args: object, **kwargs: object) -> PersistedActiveTask:
        active = kwargs["active_task"]
        assert isinstance(active, BookingTaskState)
        saved.append(active)
        return PersistedActiveTask(
            active_task=active,
            flow_id=persisted.flow_id,
            flow_version=persisted.flow_version + 1,
        )

    monkeypatch.setattr(runtime, "save_active_task", save_task)
    args = _runtime_args()
    args.update({
        "catalog": catalog,
        "history": [
            HumanMessage(
                content=("20:00" if narrow_exact_time is not None else "الساعة 8 مساء مناسبة")
            )
        ],
        "availability_reference_context": None if stale_reference_selection else reference_context,
    })
    result = runtime.orchestrate_v2_turn(**args)
    return result, read_calls, saved


def test_ra_g6_unpresented_exact_time_produces_write_ready_booking_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, read_calls, saved = _run_ra_g6_unpresented_exact_time_case(monkeypatch)
    assert result.reference_action == "new_search"
    assert result.selected_option_ref is None
    assert result.full_interpreter_called is True
    assert read_calls == [
        ["service_catalog_set", "availability"],
        ["service_catalog_set", "availability"],
    ]
    assert result.pending_write is not None
    assert result.pending_write.step.disposition == "write_ready"
    assert result.pending_write.step.write_intent is not None
    assert result.pending_write.step.write_intent.kind == "booking_proposal"
    assert result.pending_write.step.response_goal == "booking_completed"
    assert isinstance(result.active_task, BookingTaskState)
    assert result.active_task.requested_service_ids == ["service-a", "service-b"]
    assert result.active_task.status == "awaiting_confirmation"
    assert result.active_task.proposal is not None
    assert result.active_task.proposal.service_ids == ["service-a", "service-b"]
    assert result.active_task.proposal.doctor_id == "doctor-1"
    assert result.active_task.proposal.device_key == "device-1"
    assert result.active_task.proposal.start_at.isoformat() == "2026-09-12T20:00:00+03:00"
    assert result.active_task.write_authorization.authorized is True
    assert saved and saved[-1].proposal is not None


def test_unpresented_exact_clock_new_search_delegates_to_full_interpreter_and_write_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, read_calls, _saved = _run_ra_g6_unpresented_exact_time_case(
        monkeypatch,
        narrow_exact_time="20:00",
    )

    assert result.reference_action == "new_search"
    assert result.full_interpreter_called is True
    assert result.pending_write is not None
    assert result.pending_write.step.write_intent is not None
    assert result.pending_write.step.write_intent.kind == "booking_proposal"
    assert read_calls == [
        ["service_catalog_set", "availability"],
        ["service_catalog_set", "availability"],
    ]


def test_ra_g6_unpresented_exact_time_fails_closed_when_fresh_continuation_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, read_calls, _saved = _run_ra_g6_unpresented_exact_time_case(
        monkeypatch,
        continuation_available=False,
    )
    assert read_calls == [
        ["service_catalog_set", "availability"],
        ["service_catalog_set", "availability"],
    ]
    assert result.pending_write is None
    assert isinstance(result.active_task, BookingTaskState)
    assert result.active_task.proposal is None
    assert result.outcomes[-1].response_goal == "requested_time_unavailable"


def test_stale_active_option_reference_falls_back_to_full_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _read_calls, _saved = _run_ra_g6_unpresented_exact_time_case(
        monkeypatch,
        stale_reference_selection=True,
    )

    assert result.full_interpreter_called is True
    assert result.plan.steps[0].disposition == "read"
    assert result.active_task is not None
    assert result.active_task.proposal is not None


def test_ra_g6_non_exact_commercial_step_does_not_auto_continue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, read_calls, _saved = _run_ra_g6_unpresented_exact_time_case(
        monkeypatch,
        exact_time_requested=False,
    )
    assert read_calls == [["service_catalog_set", "availability"]]
    assert result.pending_write is None
    assert isinstance(result.active_task, BookingTaskState)
    assert result.active_task.proposal is None


def test_device_auto_resolved_same_turn_continuation_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, read_calls, _saved = _run_ra_g6_unpresented_exact_time_case(
        monkeypatch,
        exact_time_requested=False,
        device_auto_resolved=True,
    )
    assert read_calls == [
        ["service_catalog", "availability"],
        ["service_catalog_set", "availability"],
    ]
    assert result.pending_write is None
    assert isinstance(result.active_task, BookingTaskState)


def test_h3b_grouped_reference_rejects_resource_not_common_to_service_set() -> None:
    task = BookingTaskState(
        status="awaiting_choice",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id="service-b", branch_id="branch-1"),
        requested_service_ids=["service-a", "service-b"],
    )
    catalog = {
        "services": [
            {
                "id": "service-a",
                "requires_laser_device": True,
                "laser_devices": [
                    {"device_key": "device-1", "price_minor": 1, "duration_minutes": 15}
                ],
            },
            {
                "id": "service-b",
                "requires_laser_device": True,
                "laser_devices": [
                    {"device_key": "device-2", "price_minor": 1, "duration_minutes": 20}
                ],
            },
        ],
        "doctors": [
            {
                "id": "doctor-1",
                "service_ids": ["service-a", "service-b"],
                "branch_ids": ["branch-1"],
            }
        ],
    }
    reference_context = {
        "requested_service_ids": ["service-a", "service-b"],
        "date": {"mode": "exact", "start_date": "2026-09-12", "end_date": None},
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "index": 1,
                "concrete": True,
                "start_local": "2026-09-12T20:00:00+03:00",
                "start_time_24h": "20:00",
                "slot": {
                    "service_id": "service-b",
                    "branch_id": "branch-1",
                    "doctor_id": "doctor-1",
                    "laser_device_key": "device-1",
                    "start_local": "2026-09-12T20:00:00+03:00",
                    "start_time_24h": "20:00",
                },
            }
        ],
    }

    result = runtime._grouped_visit_reference_understanding(
        task,
        decision=ReferenceDecision(action="clarify", option_ref=None, exact_time="20:00"),
        availability_context=reference_context,
        catalog=catalog,
    )

    assert result is None
    assert task.constraints.device_key is None
    assert task.write_authorization.authorized is False


def test_orchestrator_rejects_out_of_scope_booking_slot_without_authorizing_or_replacing_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = V2FixtureEnvironment()
    selected_slot = next(
        dict(row)
        for row in env.slots
        if row.get("service_id") == "svc-bikini"
        and row.get("doctor_id") == "doc-maryam"
        and str(row.get("start_local") or "")[11:16] == "20:00"
    )
    selected_slot["device_key"] = selected_slot.get("laser_device_key")
    task = BookingTaskState(
        status="awaiting_slot",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(
            service_id="svc-underarm",
            branch_id="single-location",
            doctor_id="doc-maryam",
            device_key="candela_gentle",
            date=DateConstraint(mode="exact", start_date="2026-09-12"),
        ),
        requested_service_ids=["svc-underarm"],
        service_device_keys={"svc-underarm": "candela_gentle"},
        option_snapshot=OptionSnapshot(
            snapshot_id="scope-rejection-snapshot",
            purpose="booking_slot",
            task_version=4,
            created_at=_NOW - timedelta(minutes=5),
            expires_at=_NOW + timedelta(minutes=10),
            options=[
                OptionChoice(ref="slot-bikini", label="20:00", payload=selected_slot)
            ],
        ),
        version=4,
    )
    persisted = PersistedActiveTask(active_task=task, flow_id=uuid4(), flow_version=7)
    reference_context = {
        "operation_type": "availability",
        "requested_service_ids": ["svc-underarm"],
        "date": {
            "mode": "exact",
            "start_date": "2026-09-12",
            "end_date": None,
        },
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "index": 1,
                "concrete": True,
                "service_id": "svc-bikini",
                "start_time_24h": "20:00",
                "start_local": selected_slot["start_local"],
                "slot": selected_slot,
            }
        ],
    }
    monkeypatch.setattr(runtime, "load_active_task", lambda *args, **kwargs: persisted)
    monkeypatch.setattr(
        runtime,
        "interpret_availability_reference_turn",
        lambda **kwargs: ReferenceInterpretation(
            decision=ReferenceDecision(
                action="select_presented_option",
                option_ref="opt_1",
                exact_time=None,
                selection_scope="none",
            ),
            model_name="deterministic-test",
        ),
    )
    monkeypatch.setattr(
        runtime,
        "interpret_customer_turn_v2",
        lambda **kwargs: pytest.fail(
            "a current but out-of-scope selection must be rejected before generic interpretation"
        ),
    )
    monkeypatch.setattr(
        runtime,
        "execute_step_reads",
        lambda *args, **kwargs: ReadExecutionBundle(),
    )
    monkeypatch.setattr(
        runtime,
        "compose_v2_customer_reply",
        lambda **kwargs: (
            "Please choose an available time for the requested service.",
            "deterministic-test",
        ),
    )

    write_calls: list[PlanStep] = []

    def execute_write(step: PlanStep) -> dict[str, object]:
        write_calls.append(step)
        return {
            "ok": True,
            "write_kind": step.write_intent.kind if step.write_intent else None,
        }

    args = _runtime_args()
    args.update(
        {
            "catalog": env.catalog,
            "history": [HumanMessage(content="Choose the first available time")],
            "availability_reference_context": reference_context,
            "write_executor": execute_write,
        }
    )
    result = runtime.orchestrate_v2_turn(**args)

    assert result.reference_action == "select_presented_option"
    assert result.full_interpreter_called is False
    assert result.pending_write is None
    assert result.plan.steps[0].disposition == "clarify"
    assert result.plan.steps[0].facts["booking_selection_rejected"] is True
    assert result.plan.steps[0].facts["booking_selection_rejection_reason"] == "booking_selection_validation_failed"
    assert result.plan.steps[0].facts["selected_option_ref"] == "opt_1"
    assert result.outcomes[-1].response_goal == "clarification"
    assert result.active_task == task
    assert result.active_task.constraints.service_id == "svc-underarm"
    assert result.active_task.requested_service_ids == ["svc-underarm"]
    assert result.active_task.write_authorization.authorized is False
    assert write_calls == []
    assert all(trace.disposition_after != "write_ready" for trace in result.traces)
