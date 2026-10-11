from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import Selection, TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.services.agent_v2.active_task_progress import adapt_matching_active_task_step
from app.services.agent_v2.orchestrator import _active_task_reference_option_is_current
from app.services.agent_v2.planner import PlannerContext, plan_turn
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    OptionChoice,
    OptionSnapshot,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
SLOT = {
    "branch_id": "branch-1",
    "service_id": "svc-main",
    "doctor_id": "doctor-youssef",
    "laser_device_key": "candela",
    "start_at": "2026-10-13T16:00:00+03:00",
    "start_local": "2026-10-13T16:00:00+03:00",
    "start_time_24h": "16:00",
}
REFERENCE_CONTEXT = {
    "availability_reference_options": [
        {"option_ref": "opt_1", "index": 1, "concrete": True, "slot": SLOT},
    ]
}


def _task(*, expired: bool = False) -> BookingTaskState:
    expires_at = NOW - timedelta(seconds=1) if expired else NOW + timedelta(minutes=10)
    return BookingTaskState(
        status="awaiting_choice",
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(
            service_id="svc-main",
            device_key="candela",
            date={"mode": "exact", "start_date": "2026-10-13"},
        ),
        requested_service_ids=["svc-main", "svc-additional"],
        service_device_keys={"svc-main": "candela", "svc-additional": "candela"},
        option_snapshot=OptionSnapshot(
            snapshot_id="snapshot-1",
            purpose="booking_slot",
            task_version=4,
            created_at=NOW - timedelta(minutes=1),
            expires_at=expires_at,
            options=[
                OptionChoice(
                    ref="slot-1",
                    label="16:00 ? Youssef",
                    payload={
                        "branch_id": "branch-1",
                        "service_id": "svc-main",
                        "doctor_id": "doctor-youssef",
                        "device_key": "candela",
                        "start_at": "2026-10-13T16:00:00+03:00",
                        "start_local": "2026-10-13T16:00:00+03:00",
                        "start_time_24h": "16:00",
                    },
                )
            ],
        ),
        version=4,
    )


def _selection_operation() -> TurnOperation:
    return TurnOperation(
        type="book",
        entities=TurnEntities(),
        selection=Selection(kind="ref", ref="opt_1"),
        execution_intent="execute",
        active_task_relationship="continue",
        continues_previous=True,
    )


def test_current_reference_option_matches_current_booking_snapshot() -> None:
    assert _active_task_reference_option_is_current(
        _task(),
        option_ref="opt_1",
        availability_context=REFERENCE_CONTEXT,
        now=NOW,
    ) is True


def test_expired_reference_option_fails_closed() -> None:
    assert _active_task_reference_option_is_current(
        _task(expired=True),
        option_ref="opt_1",
        availability_context=REFERENCE_CONTEXT,
        now=NOW,
    ) is False


def test_reference_option_must_match_current_snapshot_payload() -> None:
    mismatched = {
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "index": 1,
                "concrete": True,
                "slot": {**SLOT, "doctor_id": "doctor-other"},
            }
        ]
    }
    assert _active_task_reference_option_is_current(
        _task(),
        option_ref="opt_1",
        availability_context=mismatched,
        now=NOW,
    ) is False


def test_verified_option_ref_binds_canonical_doctor_time_and_slot_ref() -> None:
    task = _task()
    operation = _selection_operation()
    semantic = SemanticContext(model_input={}, reference_map={}, server_metadata={})
    plan = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(
            semantic_context=semantic,
            active_task=task,
            now=NOW,
            availability_reference_context=REFERENCE_CONTEXT,
        ),
    )
    planned = plan.steps[0]
    adapted = adapt_matching_active_task_step(
        planned,
        operation=operation,
        active_task=task,
        context=semantic,
        now=NOW,
    )
    assert adapted.state_action == "update_active"
    assert adapted.facts["doctor_id"] == "doctor-youssef"
    assert adapted.facts["time"]["start_time"] == "16:00"
    assert adapted.facts["selected_option_ref"] == "opt_1"

    transition = apply_step_state(
        task,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-2",
    )
    assert transition.active_task is not None
    assert transition.active_task.constraints.doctor_id == "doctor-youssef"
    assert transition.active_task.constraints.time is not None
    assert transition.active_task.constraints.time.start_time == "16:00"
    assert transition.active_task.derived.selected_slot_ref == "opt_1"
    assert transition.active_task.requested_service_ids == ["svc-main", "svc-additional"]
    assert transition.active_task.constraints.device_key == "candela"


def test_unknown_ref_never_binds_or_mutates_active_booking() -> None:
    task = _task()
    operation = _selection_operation().model_copy(
        update={"selection": Selection(kind="ref", ref="opt_missing")}
    )
    semantic = SemanticContext(model_input={}, reference_map={}, server_metadata={})
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(
            semantic_context=semantic,
            active_task=task,
            now=NOW,
            availability_reference_context=REFERENCE_CONTEXT,
        ),
    ).steps[0]
    assert step.disposition == "clarify"
    assert step.state_action == "none"
    assert step.facts["availability_reference_reason"] == "unavailable"
