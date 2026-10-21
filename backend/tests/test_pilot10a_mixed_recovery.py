from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.services import patient_packages as package_service
from app.services.agent_v2 import write_executor
from app.services.agent_v2.planner import PlannerContext, PlanStep, WriteIntent, plan_turn
from app.services.patient_packages import PackageOperationError
from tests.test_package_refund_lifecycle_047726 import (
    _allow_package_writes,
    _engine,
    _seed_patient_service,
)


def _cancel_turn() -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="cancel_package",
                entities=TurnEntities(
                    service=EntityReference(text=None, ref="S1", candidate_refs=[]),
                    package_sessions=6,
                ),
                execution_intent="execute",
            )
        ],
        safety_signals=[],
    )


def test_cancel_package_uses_existing_planner_write_pipeline() -> None:
    semantic = build_semantic_context({
        "services": [{"id": "service-bikini", "name": "ليزر بكيني"}],
        "doctors": [],
        "appointments": [],
    })
    step = plan_turn(
        _cancel_turn(),
        PlannerContext(semantic_context=semantic, active_task=None, now=None),
    ).steps[0]
    assert step.disposition == "write_ready"
    assert step.write_intent is not None
    assert step.write_intent.kind == "cancel_package"


def test_reversible_unpaid_package_can_cancel_without_financial_mutation(monkeypatch) -> None:
    _allow_package_writes(monkeypatch)
    engine = _engine()
    with Session(engine) as db:
        workspace_id, patient_id, service_id = _seed_patient_service(db)
        package = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 sessions",
            sessions_purchased=6,
            sale_price_minor=1_200_000,
            amount_paid_minor=0,
            payment_method="unknown",
            created_by_user_id=None,
            actor_type="ai",
        )
        result = package_service.cancel_patient_package_with_refund(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            reason="Customer rejected inferred package",
            created_by_user_id=None,
            actor_type="ai",
            require_reversible_unpaid=True,
            settlement_target_minor=0,
        )
        assert result[0].status == "cancelled"
        assert result[1] == 0
        assert result[5] == 0
        assert result[6] == 0


def test_recovery_guard_refuses_paid_package(monkeypatch) -> None:
    _allow_package_writes(monkeypatch)
    engine = _engine()
    with Session(engine) as db:
        workspace_id, patient_id, service_id = _seed_patient_service(db)
        package = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 sessions",
            sessions_purchased=6,
            sale_price_minor=1_200_000,
            amount_paid_minor=100_000,
            payment_method="cash",
            created_by_user_id=None,
        )
        with pytest.raises(PackageOperationError, match="reversible unpaid"):
            package_service.cancel_patient_package_with_refund(
                db,
                workspace_id=workspace_id,
                package_id=package.id,
                reason="AI recovery must fail closed",
                created_by_user_id=None,
                actor_type="ai",
                require_reversible_unpaid=True,
                settlement_target_minor=0,
            )
        assert package.status == "active"


def test_cancel_package_is_safe_noop_when_rejected_package_was_never_created(monkeypatch) -> None:
    monkeypatch.setattr(write_executor, "list_patient_packages", lambda *args, **kwargs: [])
    events: list[str] = []
    db = SimpleNamespace(
        commit=lambda: events.append("commit"),
        rollback=lambda: events.append("rollback"),
    )
    workspace = SimpleNamespace(id=uuid4())
    patient = SimpleNamespace(id=uuid4(), status="active")
    service_id = uuid4()
    step = PlanStep(
        operation_index=0,
        operation_type="cancel_package",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="cancel_package",
            authorized=True,
            requires_verification=False,
            parameters={"service_id": str(service_id), "package_sessions": 6},
        ),
        response_goal="package_cancelled",
    )

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=step,
    )

    assert result == {
        "ok": True,
        "completed": False,
        "write_kind": "cancel_package",
        "reason": "no_matching_reversible_package",
    }
    assert events == ["commit"]
