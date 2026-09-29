from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import app.services.agent_v2.orchestrator as target
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.planner import PlanStep, TurnPlan, WriteIntent
from app.services.agent_v2.read_executor import ReadExecutionBundle

GROUP = "compound:0,1,2,3"
WORKSPACE_ID = UUID("11111111-1111-1111-1111-111111111111")
PATIENT_ID = UUID("22222222-2222-2222-2222-222222222222")
CONVERSATION_ID = UUID("33333333-3333-3333-3333-333333333333")
RUN_ID = UUID("44444444-4444-4444-4444-444444444444")


class _Nested:
    def __init__(self, db: _Db) -> None:
        self.db = db
        self.snapshot = list(db.effects)

    def commit(self) -> None:
        self.db.group_commits += 1

    def rollback(self) -> None:
        self.db.group_rollbacks += 1
        self.db.effects[:] = self.snapshot


class _Db:
    def __init__(self) -> None:
        self.effects: list[str] = []
        self.group_commits = 0
        self.group_rollbacks = 0

    def begin_nested(self) -> _Nested:
        return _Nested(self)


def _step(index: int, kind: str) -> PlanStep:
    goal = "package_purchased" if kind == "buy_package" else "booking_completed"
    return PlanStep(
        operation_index=index,
        operation_type="buy_package" if kind == "buy_package" else "book",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind=kind,
            authorized=True,
            parameters={},
            requires_verification=True,
        ),
        response_goal=goal,
        facts={"compound_write_group": GROUP, "compound_visit_grouped": True},
    )


def _plan() -> TurnPlan:
    return TurnPlan(
        steps=[
            _step(0, "buy_package"),
            _step(1, "buy_package"),
            _step(2, "booking"),
            _step(3, "booking"),
        ]
    )


def _patch_runtime(monkeypatch, plan: TurnPlan) -> None:
    understanding = SimpleNamespace(
        operations=[
    SimpleNamespace(
        type=step.operation_type,
        entities=SimpleNamespace(service=None),
    )
    for step in plan.steps
],
        safety_signals=[],
    )
    semantic = SimpleNamespace(model_input={})
    transition = SimpleNamespace(active_task=None, changed=False)

    monkeypatch.setattr(target, "load_active_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(target, "build_clinic_catalog", lambda *args, **kwargs: {})
    monkeypatch.setattr(target, "build_semantic_context", lambda *args, **kwargs: semantic)
    monkeypatch.setattr(target, "with_safe_task_context", lambda value, **kwargs: value)
    monkeypatch.setattr(target, "with_safe_read_context", lambda value, **kwargs: value)
    monkeypatch.setattr(target, "interpret_customer_turn_v2", lambda **kwargs: understanding)
    monkeypatch.setattr(target, "plan_turn", lambda *args, **kwargs: plan)
    monkeypatch.setattr(target, "normalize_compound_turn_plan", lambda value, **kwargs: value)
    monkeypatch.setattr(target, "preflight_compound_visit_plan", lambda value, **kwargs: value)
    monkeypatch.setattr(target, "adapt_matching_active_task_step", lambda step, **kwargs: step)
    monkeypatch.setattr(target, "persist_initial_task_intent", lambda step, **kwargs: step)
    monkeypatch.setattr(target, "execute_step_reads", lambda *args, **kwargs: ReadExecutionBundle())
    monkeypatch.setattr(
        target,
        "resolve_compound_followup_after_reads",
        lambda step, reads: (step, reads, False),
    )
    monkeypatch.setattr(target, "apply_step_state", lambda *args, **kwargs: transition)
    monkeypatch.setattr(target, "finalize_step_after_state_transition", lambda step, _transition: step)
    monkeypatch.setattr(target, "_persist_final_task", lambda **kwargs: None)
    monkeypatch.setattr(target, "compose_v2_customer_reply", lambda **kwargs: ("ok", "test"))

    def _outcome(step, *, action_result=None, **kwargs):
        if action_result and action_result.get("ok") is True:
            return TurnOutcome(
                status="completed",
                response_goal=step.response_goal,
                action_result=dict(action_result),
            )
        return TurnOutcome(
            status="blocked",
            response_goal="clarification",
            action_result=dict(action_result or {}),
        )

    monkeypatch.setattr(target, "build_step_outcome", _outcome)


def _run(db: _Db, monkeypatch, write_executor):
    plan = _plan()
    _patch_runtime(monkeypatch, plan)
    return target.orchestrate_v2_turn(
        db=db,
        workspace=SimpleNamespace(id=WORKSPACE_ID, name="Linka"),
        patient=SimpleNamespace(id=PATIENT_ID),
        conversation_id=CONVERSATION_ID,
        run_id=RUN_ID,
        history=[],
        local_now=datetime(2026, 9, 13, 9, 0, tzinfo=UTC),
        timezone_name="Africa/Cairo",
        clinic_name="Linka",
        write_executor=write_executor,
        turn_id="atomicity-test",
    )


def test_compound_failure_rolls_back_earlier_package_and_booking_writes(monkeypatch) -> None:
    db = _Db()
    calls = 0

    def write_executor(step: PlanStep) -> dict[str, object]:
        nonlocal calls
        calls += 1
        label = f"{step.write_intent.kind}:{step.operation_index}"
        if calls == 4:
            return {"ok": False, "write_kind": "booking", "error_code": "write_failed"}
        db.effects.append(label)
        return {"ok": True, "write_kind": step.write_intent.kind}

    result = _run(db, monkeypatch, write_executor)

    assert calls == 4
    assert db.effects == []
    assert db.group_rollbacks == 1
    assert db.group_commits == 0
    assert len(result.outcomes) == 1
    assert result.outcomes[0].status == "blocked"
    assert result.outcomes[0].action_result["error_code"] == "write_failed"


def test_compound_success_commits_group_once(monkeypatch) -> None:
    db = _Db()

    def write_executor(step: PlanStep) -> dict[str, object]:
        db.effects.append(f"{step.write_intent.kind}:{step.operation_index}")
        return {"ok": True, "write_kind": step.write_intent.kind}

    result = _run(db, monkeypatch, write_executor)

    assert db.effects == ["buy_package:0", "buy_package:1", "booking:2", "booking:3"]
    assert db.group_rollbacks == 0
    assert db.group_commits == 1
    assert len(result.outcomes) == 4
    assert all(outcome.status == "completed" for outcome in result.outcomes)
