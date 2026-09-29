from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.services.agent_v2 import orchestrator, write_executor
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.planner import PlanStep, TurnPlan, WriteIntent
from app.services.agent_v2.state_executor import StateTransition
from app.services.appointment_operations import AppointmentOperationError

_NOW = datetime(2026, 9, 12, 3, 0, 0)


def _booking_step(*, operation_index: int = 0) -> PlanStep:
    return PlanStep(
        operation_index=operation_index,
        operation_type="book",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            requires_verification=False,
            parameters={
                "branch_id": str(uuid4()),
                "doctor_id": str(uuid4()),
                "service_id": str(uuid4()),
                "start_at": "2026-09-13T12:00:00+03:00",
            },
        ),
    )


def test_live_write_uses_savepoint_without_committing_outer_transaction(monkeypatch) -> None:
    db = MagicMock()
    savepoint = MagicMock()
    db.begin_nested.return_value = savepoint
    workspace = SimpleNamespace(id=uuid4())
    patient = SimpleNamespace(id=uuid4(), status="active")
    appointment = SimpleNamespace(id=uuid4(), status="confirmed")

    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", MagicMock())
    monkeypatch.setattr(
        write_executor,
        "create_appointment_operation",
        MagicMock(return_value=appointment),
    )

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_booking_step(),
        idempotency_key="v2:inbound:0",
        commit=False,
    )

    assert result["ok"] is True
    db.begin_nested.assert_called_once_with()
    savepoint.__enter__.assert_called_once_with()
    savepoint.__exit__.assert_called_once()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


def test_live_write_failure_rolls_back_savepoint_not_outer_transaction(monkeypatch) -> None:
    db = MagicMock()
    savepoint = MagicMock()
    db.begin_nested.return_value = savepoint
    workspace = SimpleNamespace(id=uuid4())
    patient = SimpleNamespace(id=uuid4(), status="active")

    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", MagicMock())
    monkeypatch.setattr(
        write_executor,
        "create_appointment_operation",
        MagicMock(side_effect=AppointmentOperationError("slot conflict")),
    )

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_booking_step(),
        idempotency_key="v2:inbound:0",
        commit=False,
    )

    assert result["ok"] is False
    assert result["error_code"] == "write_failed"
    db.begin_nested.assert_called_once_with()
    savepoint.__exit__.assert_called_once()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


def _patch_compound_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: TurnPlan,
    understanding: TiaTurnUnderstanding,
) -> None:
    semantic_context = object()
    monkeypatch.setattr(orchestrator, "load_active_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "build_semantic_context", lambda catalog: semantic_context)
    monkeypatch.setattr(
        orchestrator,
        "with_safe_task_context",
        lambda context, *, active_task: context,
    )
    monkeypatch.setattr(
        orchestrator,
        "interpret_customer_turn_v2",
        lambda **kwargs: understanding,
    )
    monkeypatch.setattr(orchestrator, "plan_turn", lambda turn, context: plan)
    monkeypatch.setattr(
        orchestrator,
        "adapt_matching_active_task_step",
        lambda step, **kwargs: step,
    )
    monkeypatch.setattr(
        orchestrator,
        "persist_initial_task_intent",
        lambda step, **kwargs: step,
    )
    monkeypatch.setattr(
        orchestrator,
        "apply_step_state",
        lambda *args, **kwargs: StateTransition(
            active_task=None,
            changed=False,
            reason="none",
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "finalize_step_after_state_transition",
        lambda step, transition: step,
    )
    monkeypatch.setattr(
        orchestrator,
        "compose_v2_customer_reply",
        lambda **kwargs: ("done", "test-model"),
    )


def _compound_args() -> dict[str, object]:
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
        "turn_id": "turn-live",
    }


def test_compound_live_writes_execute_in_order_after_each_success(monkeypatch) -> None:
    first = _booking_step(operation_index=0)
    second = PlanStep(
        operation_index=1,
        operation_type="marketing_update",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="marketing_update",
            authorized=True,
            requires_verification=False,
            parameters={"marketing_consent": True},
        ),
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(type="book", entities=TurnEntities()),
            TurnOperation(type="marketing_update", entities=TurnEntities()),
        ]
    )
    _patch_compound_runtime(
        monkeypatch,
        plan=TurnPlan(steps=[first, second]),
        understanding=understanding,
    )

    def completed_outcome(step, **kwargs):
        goal = "booking_completed" if step.operation_index == 0 else "marketing_updated"
        return TurnOutcome(
            status="completed",
            response_goal=goal,
            action_result=kwargs["action_result"],
        )

    monkeypatch.setattr(orchestrator, "build_step_outcome", completed_outcome)
    calls: list[int] = []

    def execute(step: PlanStep) -> dict[str, object]:
        calls.append(step.operation_index)
        return {"ok": True, "write_kind": step.write_intent.kind}

    result = orchestrator.orchestrate_v2_turn(
        **_compound_args(),
        write_executor=execute,
    )

    assert calls == [0, 1]
    assert [outcome.status for outcome in result.outcomes] == ["completed", "completed"]
    assert result.pending_write is None
    assert result.reply == "done"


def test_compound_live_writes_stop_before_next_write_after_failure(monkeypatch) -> None:
    first = _booking_step(operation_index=0)
    second = PlanStep(
        operation_index=1,
        operation_type="marketing_update",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="marketing_update",
            authorized=True,
            requires_verification=False,
            parameters={"marketing_consent": True},
        ),
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(type="book", entities=TurnEntities()),
            TurnOperation(type="marketing_update", entities=TurnEntities()),
        ]
    )
    _patch_compound_runtime(
        monkeypatch,
        plan=TurnPlan(steps=[first, second]),
        understanding=understanding,
    )
    monkeypatch.setattr(
        orchestrator,
        "build_step_outcome",
        lambda step, **kwargs: TurnOutcome(
            status="blocked",
            response_goal="clarification",
            action_result=kwargs["action_result"],
        ),
    )
    calls: list[int] = []

    def execute(step: PlanStep) -> dict[str, object]:
        calls.append(step.operation_index)
        return {
            "ok": False,
            "write_kind": step.write_intent.kind,
            "error_code": "write_failed",
            "detail": "slot conflict",
        }

    result = orchestrator.orchestrate_v2_turn(
        **_compound_args(),
        write_executor=execute,
    )

    assert calls == [0]
    assert len(result.outcomes) == 1
    assert result.outcomes[0].status == "blocked"
    assert result.pending_write is None
