from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2 import orchestrator as runtime
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.planner import (
    PlannerContext,
    PlanStep,
    ReadRequest,
    TurnPlan,
    WriteIntent,
    plan_turn,
)
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state_executor import StateTransition

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _catalog():
    return {
        "services": [{"id": "svc-1", "name": "Dermatology", "requires_laser_device": False}],
        "doctors": [],
        "branches": [],
        "laser_devices": [],
        "appointments": [],
        "packages": [],
    }


def _availability_bundle(count: int, *, ok: bool = True) -> ReadExecutionBundle:
    if not ok:
        return ReadExecutionBundle(
            results=[ReadResult(kind="availability", ok=False, error_code="provider_error")]
        )
    return ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=True,
                payload={
                    "matching_slot_count": count,
                    "slots": [
                        {"start_local": f"2026-10-21T{10 + index:02d}:00:00+03:00"}
                        for index in range(count)
                    ],
                },
            )
        ]
    )


def _conditional_availability(context, *, continues_previous: bool = False):
    service_ref = next(ref for ref, target in context.reference_map.items() if target.kind == "service")
    return TurnOperation(
        type="availability",
        entities=TurnEntities(
            service=EntityReference(text="Dermatology", ref=service_ref),
            time=TimeConstraint(mode="nearest"),
        ),
        execution_intent="informational",
        continues_previous=continues_previous,
        continuation_condition="if_previous_no_availability",
    )


def _patch_read_only_runtime(monkeypatch: pytest.MonkeyPatch, turn: TiaTurnUnderstanding) -> list[int]:
    read_calls: list[int] = []
    monkeypatch.setattr(runtime, "load_active_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "interpret_customer_turn_v2", lambda **kwargs: turn)

    def execute_reads(step, _context):
        read_calls.append(step.operation_index)
        return _availability_bundle(1)

    monkeypatch.setattr(runtime, "execute_step_reads", execute_reads)
    monkeypatch.setattr(
        runtime,
        "apply_step_state",
        lambda *args, **kwargs: StateTransition(active_task=None, changed=False, reason="read_only"),
    )
    monkeypatch.setattr(runtime, "finalize_step_after_state_transition", lambda step, transition: step)
    monkeypatch.setattr(
        runtime,
        "build_step_outcome",
        lambda step, **kwargs: TurnOutcome(
            status="answered",
            response_goal="present_availability",
            facts={"availability": {"available_option_count": 1}},
        ),
    )
    monkeypatch.setattr(
        runtime,
        "compose_v2_customer_reply",
        lambda **kwargs: ("grounded availability", "test-model"),
    )
    return read_calls


def _runtime_args(*, recent_read_context=None):
    return {
        "db": object(),
        "workspace": SimpleNamespace(id=uuid4()),
        "patient": SimpleNamespace(id=uuid4()),
        "conversation_id": uuid4(),
        "run_id": uuid4(),
        "history": [],
        "local_now": NOW,
        "timezone_name": "Africa/Cairo",
        "clinic_name": "Linka Test Clinic",
        "catalog": _catalog(),
        "turn_id": "pilot07b3",
        "recent_read_context": recent_read_context,
    }


def test_unverified_reply_required_informational_availability_survives(monkeypatch):
    context = build_semantic_context(_catalog())
    operation = _conditional_availability(context, continues_previous=False)
    turn = TiaTurnUnderstanding(operations=[operation], response_disposition="reply")
    plan = plan_turn(
        turn,
        PlannerContext(
            semantic_context=context,
            active_task=None,
            now=NOW,
            pending_choice=None,
            explicit_user_time=None,
            availability_reference_exact_time=None,
            recent_read_context=None,
            availability_reference_context=None,
        ),
    )
    assert len(plan.steps) == 1
    assert plan.steps[0].disposition == "read"
    assert plan.steps[0].write_intent is None
    assert runtime._continuation_condition_satisfied(
        operation, previous_reads=None, recent_read_context=None
    ) is None

    read_calls = _patch_read_only_runtime(monkeypatch, turn)
    result = runtime.orchestrate_v2_turn(**_runtime_args())

    assert read_calls == [0]
    assert len(result.outcomes) == 1
    assert result.pending_write is None
    assert result.reply == "grounded availability"
    assert result.traces[0].skipped is False


def test_condition_semantics_distinguish_verified_false_true_and_unverified():
    context = build_semantic_context(_catalog())
    operation = _conditional_availability(context, continues_previous=False)

    assert runtime._continuation_condition_satisfied(
        operation, previous_reads=_availability_bundle(0), recent_read_context=None
    ) is True
    assert runtime._continuation_condition_satisfied(
        operation, previous_reads=_availability_bundle(1), recent_read_context=None
    ) is False
    assert runtime._continuation_condition_satisfied(
        operation, previous_reads=_availability_bundle(0, ok=False), recent_read_context=None
    ) is None
    assert runtime._continuation_condition_satisfied(
        operation, previous_reads=ReadExecutionBundle(), recent_read_context=None
    ) is None
    assert runtime._continuation_condition_satisfied(
        operation,
        previous_reads=None,
        recent_read_context={
            "operation_type": "availability",
            "availability_option_count": 0,
        },
    ) is True
    assert runtime._continuation_condition_satisfied(
        operation,
        previous_reads=None,
        recent_read_context={
            "operation_type": "availability",
            "availability_option_count": 2,
        },
    ) is False
    assert runtime._continuation_condition_satisfied(
        operation,
        previous_reads=None,
        recent_read_context={
            "operation_type": "availability",
            "availability_found": True,
        },
    ) is False
    assert runtime._continuation_condition_satisfied(
        operation, previous_reads=None, recent_read_context=None
    ) is None


def test_verified_recent_availability_disproves_condition_even_when_relationship_bit_is_false():
    context = build_semantic_context(_catalog())
    operation = _conditional_availability(context, continues_previous=False)
    assert runtime._continuation_condition_satisfied(
        operation,
        previous_reads=None,
        recent_read_context={
            "operation_type": "availability",
            "availability_option_count": 1,
        },
    ) is False


def test_unverified_condition_does_not_authorize_write_and_independent_reply_survives(monkeypatch):
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(),
                execution_intent="execute",
                continuation_condition="if_previous_no_availability",
            ),
            TurnOperation(type="clinic_info", entities=TurnEntities()),
        ],
        response_disposition="reply",
    )
    plan = TurnPlan(
        steps=[
            PlanStep(
                operation_index=0,
                operation_type="book",
                disposition="write_ready",
                write_intent=WriteIntent(
                    kind="booking",
                    authorized=True,
                    requires_verification=False,
                ),
                response_goal="booking_completed",
            ),
            PlanStep(
                operation_index=1,
                operation_type="clinic_info",
                disposition="read",
                reads=[ReadRequest(kind="clinic_info")],
                response_goal="answer_clinic_info",
            ),
        ]
    )

    semantic_context = object()
    monkeypatch.setattr(runtime, "load_active_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "build_semantic_context", lambda catalog: semantic_context)
    monkeypatch.setattr(runtime, "with_safe_task_context", lambda context, *, active_task: context)
    monkeypatch.setattr(runtime, "interpret_customer_turn_v2", lambda **kwargs: understanding)
    monkeypatch.setattr(runtime, "plan_turn", lambda turn, context: plan)
    monkeypatch.setattr(runtime, "adapt_matching_active_task_step", lambda step, **kwargs: step)
    monkeypatch.setattr(runtime, "persist_initial_task_intent", lambda step, **kwargs: step)

    read_calls: list[int] = []

    def execute_reads(step, _context):
        read_calls.append(step.operation_index)
        return ReadExecutionBundle(
            results=[ReadResult(kind="clinic_info", ok=True, payload={"clinic": {"name": "Linka"}})]
        )

    monkeypatch.setattr(runtime, "execute_step_reads", execute_reads)
    monkeypatch.setattr(
        runtime,
        "apply_step_state",
        lambda *args, **kwargs: StateTransition(active_task=None, changed=False, reason="read_only"),
    )
    monkeypatch.setattr(runtime, "finalize_step_after_state_transition", lambda step, transition: step)
    monkeypatch.setattr(
        runtime,
        "build_step_outcome",
        lambda step, **kwargs: TurnOutcome(
            status="answered", response_goal="answer_clinic_info", facts={"clinic_name": "Linka"}
        ),
    )
    monkeypatch.setattr(runtime, "compose_v2_customer_reply", lambda **kwargs: ("clinic reply", "test-model"))

    result = runtime.orchestrate_v2_turn(**_runtime_args())

    assert read_calls == [1]
    assert result.pending_write is None
    assert len(result.outcomes) == 1
    assert result.reply == "clinic reply"
    assert result.traces[0].skipped is True
    assert result.traces[0].skip_reason == (
        "continuation_condition_unverified:if_previous_no_availability"
    )
    assert result.traces[1].skipped is False
