from __future__ import annotations

from datetime import UTC, datetime

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    plan_active_task_progress,
)
from app.services.agent_v2.planner import (
    PlannerContext,
    VerificationFacts,
    advance_step_after_verification,
    plan_turn,
)
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import RescheduleTaskState
from app.services.agent_v2.state_executor import apply_step_state

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _semantic_context():
    return build_semantic_context(
        {
            "services": [
                {
                    "id": "svc-underarm",
                    "name": "ليزر إبط",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_key": "candela_gentle", "device_name": "Candela Gentle"}
                    ],
                }
            ],
            "doctors": [{"id": "doc-maryam", "name": "مريم"}],
            "appointments": [
                {
                    "appointment_id": "apt-1",
                    "service_id": "svc-underarm",
                    "doctor_id": "doc-maryam",
                    "laser_device_key": "candela_gentle",
                    "status": "confirmed",
                    "start_local": "2026-09-14T19:00:00+03:00",
                },
                {
                    "appointment_id": "apt-2",
                    "service_id": "svc-underarm",
                    "doctor_id": "doc-maryam",
                    "laser_device_key": "candela_gentle",
                    "status": "confirmed",
                    "start_local": "2026-09-20T18:00:00+03:00",
                },
            ],
        }
    )


def _planner_context(*, active_task=None) -> PlannerContext:
    return PlannerContext(
        semantic_context=_semantic_context(),
        active_task=active_task,
        now=NOW,
    )


def _reschedule_operation(*, date: str | None = None, time: str | None = None) -> TurnOperation:
    return TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date=date) if date else None,
            time=TimeConstraint(mode="exact", start_time=time) if time else None,
        ),
        package_usage="unspecified",
    )


def _appointment_reads(rows: list[dict[str, object]]) -> ReadExecutionBundle:
    return ReadExecutionBundle(
        results=[ReadResult(kind="appointments", ok=True, payload={"appointments": rows})]
    )


def _appointment_row(appointment_id: str = "apt-1") -> dict[str, object]:
    return {
        "appointment_id": appointment_id,
        "service_id": "svc-underarm",
        "doctor_id": "doc-maryam",
        "laser_device_key": "candela_gentle",
        "start_local": "2026-09-14T19:00:00+03:00",
    }


def test_reschedule_anchors_unique_target_before_date_then_reuses_same_id_on_follow_up() -> None:
    first_operation = _reschedule_operation()
    first_turn = TiaTurnUnderstanding(operations=[first_operation], safety_signals=[])
    first_step = plan_turn(first_turn, _planner_context()).steps[0]

    # The first turn must resolve the existing appointment before asking for replacement timing.
    assert first_step.disposition == "clarify"
    assert first_step.clarification_field == "date"
    assert first_step.state_action == "start_reschedule"
    assert [read.kind for read in first_step.reads] == ["appointments"]
    assert first_step.write_intent is None

    verified_first_step = advance_step_after_verification(
        first_step,
        VerificationFacts(
            appointment_match_count=1,
            verified_parameters={"appointment_id": "apt-1"},
        ),
    )
    anchored_transition = apply_step_state(
        None,
        step=verified_first_step,
        operation=first_operation,
        reads=_appointment_reads([_appointment_row()]),
        now=NOW,
        turn_id="turn-reschedule-start",
    )

    anchored = anchored_transition.active_task
    assert isinstance(anchored, RescheduleTaskState)
    assert anchored.target.appointment_id == "apt-1"
    assert anchored.replacement.date is None
    assert anchored.write_authorization.authorized is True

    # A natural follow-up may be interpreted as reschedule again. Python must update only the
    # replacement constraints and keep the previously verified appointment target.
    follow_up_operation = _reschedule_operation(date="2026-09-15", time="18:00")
    follow_up_turn = TiaTurnUnderstanding(operations=[follow_up_operation], safety_signals=[])
    fresh_follow_up_step = plan_turn(
        follow_up_turn,
        _planner_context(active_task=anchored),
    ).steps[0]
    adapted_follow_up_step = adapt_matching_active_task_step(
        fresh_follow_up_step,
        operation=follow_up_operation,
        active_task=anchored,
        context=_semantic_context(),
    )

    assert adapted_follow_up_step.disposition == "state_update"
    assert adapted_follow_up_step.state_action == "update_active"
    assert "appointment_id" not in adapted_follow_up_step.facts

    updated_transition = apply_step_state(
        anchored,
        step=adapted_follow_up_step,
        operation=follow_up_operation,
        reads=None,
        now=NOW,
        turn_id="turn-reschedule-time",
    )
    updated = updated_transition.active_task
    assert isinstance(updated, RescheduleTaskState)
    assert updated.target.appointment_id == "apt-1"
    assert updated.replacement.date is not None
    assert updated.replacement.date.start_date == "2026-09-15"
    assert updated.replacement.time is not None
    assert updated.replacement.time.start_time == "18:00"

    progress = plan_active_task_progress(
        updated,
        operation_index=0,
        context=_semantic_context(),
    )
    assert progress.disposition == "read"
    assert progress.write_intent is not None
    assert progress.write_intent.kind == "reschedule"
    assert progress.write_intent.parameters["appointment_id"] == "apt-1"
    assert progress.reads[0].kind == "appointments"
    assert progress.reads[0].parameters == {"appointment_id": "apt-1"}
    assert progress.reads[1].kind == "availability"
    assert progress.reads[1].parameters["appointment_id"] == "apt-1"
    assert progress.reads[1].parameters["date"]["start_date"] == "2026-09-15"
    assert progress.reads[1].parameters["time"]["start_time"] == "18:00"

    ready = advance_step_after_verification(
        progress,
        VerificationFacts(
            appointment_match_count=1,
            exact_slot_match_count=1,
            verified_parameters={"start_at": "2026-09-15T18:00:00+03:00"},
        ),
    )
    assert ready.disposition == "write_ready"
    assert ready.write_intent is not None
    assert ready.write_intent.parameters["appointment_id"] == "apt-1"
    assert ready.write_intent.parameters["start_at"] == "2026-09-15T18:00:00+03:00"
    assert ready.write_intent.parameters["_reschedule_required_fields_resolved"] is True


def test_reschedule_does_not_anchor_an_arbitrary_target_when_multiple_appointments_match() -> None:
    operation = _reschedule_operation()
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    verified = advance_step_after_verification(
        step,
        VerificationFacts(appointment_match_count=2),
    )

    assert verified.disposition == "clarify"
    assert verified.clarification_field == "appointment"
    assert verified.response_goal == "ask_appointment_choice"

    transition = apply_step_state(
        None,
        step=verified,
        operation=operation,
        reads=_appointment_reads([_appointment_row("apt-1"), _appointment_row("apt-2")]),
        now=NOW,
        turn_id="turn-reschedule-ambiguous",
    )
    assert transition.active_task is None


def test_reschedule_does_not_create_target_when_no_appointment_matches() -> None:
    operation = _reschedule_operation()
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    verified = advance_step_after_verification(
        step,
        VerificationFacts(appointment_match_count=0),
    )

    assert verified.disposition == "blocked"
    assert verified.write_intent is None

    transition = apply_step_state(
        None,
        step=verified,
        operation=operation,
        reads=_appointment_reads([]),
        now=NOW,
        turn_id="turn-reschedule-missing",
    )
    assert transition.active_task is None


def test_reschedule_missing_time_never_promotes_to_write_ready() -> None:
    operation = _reschedule_operation()
    first_step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    verified_first = advance_step_after_verification(
        first_step,
        VerificationFacts(appointment_match_count=1, verified_parameters={"appointment_id": "apt-1"}),
    )
    transition = apply_step_state(
        None,
        step=verified_first,
        operation=operation,
        reads=_appointment_reads([_appointment_row()]),
        now=NOW,
        turn_id="turn-start",
    )
    state = transition.active_task
    assert isinstance(state, RescheduleTaskState)
    state = state.model_copy(
        update={
            "replacement": state.replacement.model_copy(
                update={"date": DateConstraint(mode="exact", start_date="2026-09-15"), "time": None}
            )
        }
    )
    progress = plan_active_task_progress(state, operation_index=0, context=_semantic_context())
    assert progress.facts["exact_time_requested"] is False
    advanced = advance_step_after_verification(
        progress,
        VerificationFacts(appointment_match_count=1, exact_slot_match_count=1, verified_parameters={"start_at": "2026-09-15T18:00:00+03:00"}),
    )
    assert advanced.disposition != "write_ready"



def test_reschedule_occupied_exact_target_never_promotes_to_write() -> None:
    operation = _reschedule_operation(date="2026-09-15", time="18:00")
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    verified_target = advance_step_after_verification(
        step,
        VerificationFacts(appointment_match_count=1, verified_parameters={"appointment_id": "apt-1"}),
    )
    transition = apply_step_state(
        None,
        step=verified_target,
        operation=operation,
        reads=_appointment_reads([_appointment_row()]),
        now=NOW,
        turn_id="turn-occupied",
    )
    state = transition.active_task
    assert isinstance(state, RescheduleTaskState)
    progress = plan_active_task_progress(state, operation_index=0, context=_semantic_context())
    blocked = advance_step_after_verification(
        progress,
        VerificationFacts(
            appointment_match_count=1,
            exact_slot_match_count=0,
            verified_parameters={},
        ),
    )
    assert blocked.disposition == "blocked"
    assert blocked.response_goal == "requested_time_unavailable"
    assert blocked.disposition != "write_ready"
