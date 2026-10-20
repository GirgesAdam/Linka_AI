from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import merge_verified_read_context
from app.services.agent_v2 import orchestrator as runtime
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state_executor import StateTransition

NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
DATE = "2026-10-24"


def _catalog():
    return {"services": [{"id": "svc-1", "name": "Underarm", "requires_laser_device": False}], "doctors": [], "branches": [], "laser_devices": [], "appointments": [], "packages": []}


def _recent(context):
    service_ref = next(ref for ref, target in context.reference_map.items() if target.kind == "service")
    return {
        "operation_type": "availability",
        "service_ref": service_ref,
        "availability_found": True,
        "availability_option_count": 2,
        "date": DateConstraint(mode="exact", start_date=DATE).model_dump(mode="json"),
        "time": TimeConstraint(mode="after", start_time="17:00").model_dump(mode="json"),
    }


def _raw():
    return TiaTurnUnderstanding(
        operations=[TurnOperation(
            type="availability",
            entities=TurnEntities(
                date=DateConstraint(mode="from_date", start_date="2026-10-31"),
                time=TimeConstraint(mode="after", start_time="18:00"),
            ),
            execution_intent="informational",
            continues_previous=True,
            continuation_condition="if_previous_no_availability",
        )],
        response_disposition="reply",
    )


def test_reply_required_disproved_conditional_continuation_still_produces_outcome(monkeypatch):
    seed_context = build_semantic_context(_catalog())
    recent = _recent(seed_context)
    raw = _raw()
    monkeypatch.setattr(runtime, "load_active_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "interpret_customer_turn_v2",
        lambda **kwargs: merge_verified_read_context(raw, kwargs["semantic_context"]),
    )
    monkeypatch.setattr(
        runtime,
        "execute_step_reads",
        lambda step, context: ReadExecutionBundle(results=[ReadResult(kind="availability", ok=True, payload={"slots": [{"start_local": f"{DATE}T18:00:00+03:00"}], "matching_slot_count": 1})]),
    )
    monkeypatch.setattr(runtime, "apply_step_state", lambda *args, **kwargs: StateTransition(active_task=None, changed=False, reason="read_only"))
    monkeypatch.setattr(runtime, "finalize_step_after_state_transition", lambda step, transition: step)
    monkeypatch.setattr(runtime, "build_step_outcome", lambda step, **kwargs: TurnOutcome(status="answered", response_goal="present_availability", facts={"availability": {"available_option_count": 1}}))
    monkeypatch.setattr(runtime, "compose_v2_customer_reply", lambda **kwargs: ("grounded availability", "test-model"))

    result = runtime.orchestrate_v2_turn(
        db=object(), workspace=SimpleNamespace(id=uuid4()), patient=SimpleNamespace(id=uuid4()),
        conversation_id=uuid4(), run_id=uuid4(), history=[], local_now=NOW,
        timezone_name="Africa/Cairo", clinic_name="Linka Test Clinic", catalog=_catalog(),
        turn_id="pilot07b-a", recent_read_context=recent,
    )

    operation = result.understanding.operations[0]
    assert operation.continuation_condition == "always"
    assert operation.entities.date == DateConstraint(mode="exact", start_date=DATE)
    assert operation.entities.time == TimeConstraint(mode="after", start_time="17:00")
    assert len(result.outcomes) == 1
    assert result.pending_write is None
    assert result.reply == "grounded availability"
