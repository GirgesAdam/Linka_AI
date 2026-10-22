from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.agents.v2.semantic_context import SemanticContext, SemanticReferenceTarget
from app.agents.v2.semantic_state_view import verified_action_semantic_view
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


def _package_step(
    *,
    operation_index: int,
    operation_type: str,
    service_id: str,
    offer_id: str | None = None,
) -> PlanStep:
    params: dict[str, object] = {
        "service_id": service_id,
        "device_key": "prime_lase",
        "package_sessions": 6,
        "package_usage": "unspecified",
    }
    if offer_id is not None:
        params["package_offer_id"] = offer_id
    return PlanStep(
        operation_index=operation_index,
        operation_type=operation_type,
        disposition="write_ready",
        write_intent=WriteIntent(
            kind=operation_type,
            authorized=True,
            requires_verification=False,
            parameters=params,
        ),
        response_goal=(
            "package_purchased" if operation_type == "buy_package" else "package_cancelled"
        ),
        facts=params,
    )


def _recent_package_action(
    *,
    package_id: str,
    offer_id: str,
    service_id: str,
) -> dict[str, object]:
    return {
        "patient_package_id": package_id,
        "package_offer_id": offer_id,
        "amount_paid_minor": 0,
        "service_id": service_id,
        "device_key": "prime_lase",
        "package_sessions": 6,
    }


def _package_row(
    *,
    package_id: str,
    offer_id: str,
    paid: int = 0,
    reserved: int = 0,
    consumed: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=package_id,
        package_offer_id=offer_id,
        effective_status="active",
        purchase_transaction_id=("payment-1" if paid else None),
        amount_paid_minor=paid,
        amount_refunded_minor=0,
        sessions_reserved=reserved,
        sessions_consumed=consumed,
    )


def _patch_package_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: TurnPlan,
    understanding: TiaTurnUnderstanding,
    rows: list[SimpleNamespace],
) -> None:
    _patch_compound_runtime(monkeypatch, plan=plan, understanding=understanding)
    monkeypatch.setattr(
        orchestrator,
        "with_safe_action_context",
        lambda context, *, action_context: context,
    )
    monkeypatch.setattr(orchestrator, "list_patient_packages", lambda *args, **kwargs: rows)

    def outcome(step: PlanStep, **kwargs) -> TurnOutcome:
        action = kwargs.get("action_result")
        if isinstance(action, dict) and action.get("ok") is True:
            goal = (
                "package_cancelled"
                if step.write_intent is not None and step.write_intent.kind == "cancel_package"
                else "package_purchased"
            )
            return TurnOutcome(status="completed", response_goal=goal, action_result=action)
        return TurnOutcome(status="answered", response_goal=step.response_goal or "social_ack")

    monkeypatch.setattr(orchestrator, "build_step_outcome", outcome)


def test_package_completed_action_context_retains_server_owned_identity() -> None:
    step = _package_step(
        operation_index=0,
        operation_type="buy_package",
        service_id="service-a",
        offer_id="offer-a",
    )

    context = orchestrator._completed_action_context(
        step,
        {
            "ok": True,
            "write_kind": "buy_package",
            "patient_package_id": "package-a",
            "amount_paid_minor": 0,
        },
    )

    assert context == {
        "operation_type": "buy_package",
        "package_actions": [
            {
                "patient_package_id": "package-a",
                "package_offer_id": "offer-a",
                "amount_paid_minor": 0,
                "service_id": "service-a",
                "device_key": "prime_lase",
                "package_sessions": 6,
            }
        ],
    }


def test_run18_shape_cancels_recent_b_and_does_not_replay_a(monkeypatch) -> None:
    recent = {
        "operation_type": "buy_package",
        "package_actions": [
            _recent_package_action(package_id="package-a", offer_id="offer-a", service_id="service-a"),
            _recent_package_action(package_id="package-b", offer_id="offer-b", service_id="service-b"),
        ],
    }
    plan = TurnPlan(
        steps=[
            _package_step(
                operation_index=0,
                operation_type="buy_package",
                service_id="service-a",
                offer_id="offer-a",
            ),
            _package_step(
                operation_index=1,
                operation_type="cancel_package",
                service_id="service-b",
            ),
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(type="buy_package", entities=TurnEntities(), execution_intent="execute"),
            TurnOperation(
                type="cancel_package",
                entities=TurnEntities(),
                execution_intent="execute",
                continues_previous=True,
            ),
        ]
    )
    _patch_package_runtime(
        monkeypatch,
        plan=plan,
        understanding=understanding,
        rows=[
            _package_row(package_id="package-a", offer_id="offer-a"),
            _package_row(package_id="package-b", offer_id="offer-b"),
        ],
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def execute(step: PlanStep) -> dict[str, object]:
        assert step.write_intent is not None
        calls.append((step.write_intent.kind, dict(step.write_intent.parameters)))
        return {
            "ok": True,
            "write_kind": step.write_intent.kind,
            "patient_package_id": str(step.write_intent.parameters.get("package_id") or "package-new"),
            "status": "cancelled",
            "amount_paid_minor": 0,
        }

    result = orchestrator.orchestrate_v2_turn(
        **_compound_args(),
        write_executor=execute,
        recent_action_context=recent,
    )

    assert [kind for kind, _ in calls] == ["cancel_package"]
    assert calls[0][1]["package_id"] == "package-b"
    assert calls[0][1]["_recent_package_recovery_verified"] is True
    assert result.plan.steps[1].write_intent is not None
    assert result.plan.steps[1].write_intent.parameters["package_id"] == "package-b"
    assert result.outcomes[-1].response_goal == "package_cancelled"
    assert result.pending_write is None


def test_run16_shape_noops_missing_b_and_does_not_replay_a(monkeypatch) -> None:
    recent = {
        "operation_type": "buy_package",
        "package_actions": [
            _recent_package_action(package_id="package-a", offer_id="offer-a", service_id="service-a")
        ],
    }
    plan = TurnPlan(
        steps=[
            _package_step(
                operation_index=0,
                operation_type="buy_package",
                service_id="service-a",
                offer_id="offer-a",
            ),
            _package_step(
                operation_index=1,
                operation_type="cancel_package",
                service_id="service-b",
            ),
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(type="buy_package", entities=TurnEntities(), execution_intent="execute"),
            TurnOperation(
                type="cancel_package",
                entities=TurnEntities(),
                execution_intent="execute",
                continues_previous=True,
            ),
        ]
    )
    _patch_package_runtime(
        monkeypatch,
        plan=plan,
        understanding=understanding,
        rows=[_package_row(package_id="package-a", offer_id="offer-a")],
    )
    calls: list[str] = []

    result = orchestrator.orchestrate_v2_turn(
        **_compound_args(),
        write_executor=lambda step: calls.append(step.write_intent.kind) or {"ok": True},
        recent_action_context=recent,
    )

    assert calls == []
    assert result.pending_write is None
    assert result.plan.steps[0].facts["acknowledgment"] == {
        "action": "buy_package",
        "already_completed": True,
    }
    assert result.plan.steps[1].facts == {
        "package_correction_noop": True,
        "rejected_package_was_not_created": True,
    }
    assert "acknowledgment" not in result.plan.steps[1].facts


def test_immediate_package_replay_is_suppressed_after_canonical_validation(monkeypatch) -> None:
    recent = {
        "operation_type": "buy_package",
        "package_actions": [
            _recent_package_action(package_id="package-a", offer_id="offer-a", service_id="service-a")
        ],
    }
    plan = TurnPlan(
        steps=[
            _package_step(
                operation_index=0,
                operation_type="buy_package",
                service_id="service-a",
                offer_id="offer-a",
            )
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="buy_package",
                entities=TurnEntities(),
                execution_intent="execute",
                continues_previous=True,
            )
        ]
    )
    _patch_package_runtime(
        monkeypatch,
        plan=plan,
        understanding=understanding,
        rows=[_package_row(package_id="package-a", offer_id="offer-a")],
    )
    calls: list[str] = []

    orchestrator.orchestrate_v2_turn(
        **_compound_args(),
        write_executor=lambda step: calls.append(step.write_intent.kind) or {"ok": True},
        recent_action_context=recent,
    )

    assert calls == []


def test_unrelated_turn_does_not_cancel_recent_packages(monkeypatch) -> None:
    recent = {
        "operation_type": "buy_package",
        "package_actions": [
            _recent_package_action(package_id="package-a", offer_id="offer-a", service_id="service-a"),
            _recent_package_action(package_id="package-b", offer_id="offer-b", service_id="service-b"),
        ],
    }
    plan = TurnPlan(
        steps=[
            PlanStep(
                operation_index=0,
                operation_type="social",
                disposition="respond",
                response_goal="social_ack",
            )
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[TurnOperation(type="social", entities=TurnEntities(), execution_intent="informational")]
    )
    _patch_package_runtime(
        monkeypatch,
        plan=plan,
        understanding=understanding,
        rows=[
            _package_row(package_id="package-a", offer_id="offer-a"),
            _package_row(package_id="package-b", offer_id="offer-b"),
        ],
    )
    calls: list[str] = []

    orchestrator.orchestrate_v2_turn(
        **_compound_args(),
        write_executor=lambda step: calls.append(step.write_intent.kind) or {"ok": True},
        recent_action_context=recent,
    )

    assert calls == []


@pytest.mark.parametrize(
    ("paid", "reserved", "consumed"),
    [(100, 0, 0), (0, 1, 0), (0, 0, 1)],
)
def test_recent_package_recovery_fails_closed_on_financial_or_usage_impact(
    monkeypatch,
    paid: int,
    reserved: int,
    consumed: int,
) -> None:
    recent = {
        "operation_type": "buy_package",
        "package_actions": [
            _recent_package_action(package_id="package-b", offer_id="offer-b", service_id="service-b")
        ],
    }
    plan = TurnPlan(
        steps=[
            _package_step(
                operation_index=0,
                operation_type="cancel_package",
                service_id="service-b",
            )
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="cancel_package",
                entities=TurnEntities(),
                execution_intent="execute",
                continues_previous=True,
            )
        ]
    )
    _patch_package_runtime(
        monkeypatch,
        plan=plan,
        understanding=understanding,
        rows=[
            _package_row(
                package_id="package-b",
                offer_id="offer-b",
                paid=paid,
                reserved=reserved,
                consumed=consumed,
            )
        ],
    )
    calls: list[str] = []

    result = orchestrator.orchestrate_v2_turn(
        **_compound_args(),
        write_executor=lambda step: calls.append(step.write_intent.kind) or {"ok": True},
        recent_action_context=recent,
    )

    assert calls == []
    assert any(trace.disposition_after == "clarify" for trace in result.traces)
    assert result.plan.steps[0].facts == {"recent_package_recovery_requires_human": True}
    assert "acknowledgment" not in result.plan.steps[0].facts


def test_later_explicit_package_purchase_remains_allowed(monkeypatch) -> None:
    recent = {
        "operation_type": "buy_package",
        "package_actions": [
            _recent_package_action(package_id="package-a", offer_id="offer-a", service_id="service-a")
        ],
    }
    plan = TurnPlan(
        steps=[
            _package_step(
                operation_index=0,
                operation_type="buy_package",
                service_id="service-b",
                offer_id="offer-b",
            )
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="buy_package",
                entities=TurnEntities(),
                execution_intent="execute",
                continues_previous=False,
            )
        ]
    )
    _patch_package_runtime(
        monkeypatch,
        plan=plan,
        understanding=understanding,
        rows=[_package_row(package_id="package-a", offer_id="offer-a")],
    )
    calls: list[str] = []

    orchestrator.orchestrate_v2_turn(
        **_compound_args(),
        write_executor=lambda step: calls.append(step.write_intent.kind)
        or {
            "ok": True,
            "write_kind": "buy_package",
            "patient_package_id": "package-b",
            "amount_paid_minor": 0,
        },
        recent_action_context=recent,
    )

    assert calls == ["buy_package"]


def test_two_successful_package_writes_share_one_recent_action_container(monkeypatch) -> None:
    plan = TurnPlan(
        steps=[
            _package_step(
                operation_index=0,
                operation_type="buy_package",
                service_id="service-a",
                offer_id="offer-a",
            ),
            _package_step(
                operation_index=1,
                operation_type="buy_package",
                service_id="service-b",
                offer_id="offer-b",
            ),
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(type="buy_package", entities=TurnEntities(), execution_intent="execute"),
            TurnOperation(type="buy_package", entities=TurnEntities(), execution_intent="execute"),
        ]
    )
    _patch_package_runtime(monkeypatch, plan=plan, understanding=understanding, rows=[])

    def execute(step: PlanStep) -> dict[str, object]:
        assert step.write_intent is not None
        suffix = "a" if step.operation_index == 0 else "b"
        return {
            "ok": True,
            "write_kind": "buy_package",
            "patient_package_id": f"package-{suffix}",
            "amount_paid_minor": 0,
        }

    result = orchestrator.orchestrate_v2_turn(
        **_compound_args(),
        write_executor=execute,
    )

    assert result.verified_action_context is not None
    assert result.verified_action_context["operation_type"] == "buy_package"
    assert [
        item["patient_package_id"]
        for item in result.verified_action_context["package_actions"]
    ] == ["package-a", "package-b"]


def test_recent_package_semantic_view_exposes_only_opaque_refs() -> None:
    context = SemanticContext(
        model_input={},
        reference_map={
            "P1": SemanticReferenceTarget(kind="package", canonical_id="package-a"),
            "P2": SemanticReferenceTarget(kind="package", canonical_id="package-b"),
            "S1": SemanticReferenceTarget(kind="service", canonical_id="service-a"),
            "S2": SemanticReferenceTarget(kind="service", canonical_id="service-b"),
            "V1": SemanticReferenceTarget(kind="device", canonical_id="prime_lase"),
        },
    )
    action_context = {
        "operation_type": "buy_package",
        "package_actions": [
            _recent_package_action(package_id="package-a", offer_id="offer-secret-a", service_id="service-a"),
            _recent_package_action(package_id="package-b", offer_id="offer-secret-b", service_id="service-b"),
        ],
    }

    view = verified_action_semantic_view(action_context, context=context)

    assert view == {
        "operation_type": "buy_package",
        "package_actions": [
            {
                "package_ref": "P1",
                "service_ref": "S1",
                "device_ref": "V1",
                "package_sessions": 6,
            },
            {
                "package_ref": "P2",
                "service_ref": "S2",
                "device_ref": "V1",
                "package_sessions": 6,
            },
        ],
    }
    assert "offer-secret" not in repr(view)
    assert "package-a" not in repr(view)
    assert "package-b" not in repr(view)


def test_cancel_package_write_executor_requires_exact_verified_recent_identity(monkeypatch) -> None:
    db = MagicMock()
    savepoint = MagicMock()
    db.begin_nested.return_value = savepoint
    workspace = SimpleNamespace(id=uuid4())
    patient = SimpleNamespace(id=uuid4(), status="active")
    package_id = uuid4()
    cancelled = SimpleNamespace(id=package_id, status="cancelled")
    lifecycle = MagicMock(
        return_value=(cancelled, 0, 0, 0, 0, 0, 0, [])
    )
    monkeypatch.setattr(write_executor, "cancel_patient_package_with_refund", lifecycle)

    unverified = PlanStep(
        operation_index=0,
        operation_type="cancel_package",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="cancel_package",
            authorized=True,
            requires_verification=False,
            parameters={"package_id": str(package_id)},
        ),
        response_goal="package_cancelled",
    )
    blocked = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=unverified,
        idempotency_key="cancel-unverified",
        commit=False,
    )
    assert blocked["ok"] is False
    assert blocked["error_code"] == "package_recovery_unverified"
    lifecycle.assert_not_called()

    verified = unverified.model_copy(
        update={
            "write_intent": unverified.write_intent.model_copy(
                update={
                    "parameters": {
                        "package_id": str(package_id),
                        "_recent_package_recovery_verified": True,
                    }
                }
            )
        }
    )
    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=verified,
        idempotency_key="cancel-verified",
        commit=False,
    )

    assert result["ok"] is True
    assert result["write_kind"] == "cancel_package"
    lifecycle.assert_called_once()
    kwargs = lifecycle.call_args.kwargs
    assert kwargs["package_id"] == package_id
    assert kwargs["expected_patient_id"] == patient.id
    assert kwargs["require_reversible_unpaid"] is True
    assert kwargs["settlement_target_minor"] == 0
    assert kwargs["actor_type"] == "ai"
