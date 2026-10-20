from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.time_resolution import resolve_bare_clock_constraint
from app.agents.v2.turn_contract import (
    DateConstraint,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2 import state_persistence
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    plan_active_task_progress,
    prepare_active_task_clock_progress,
    resolve_pending_bare_clock_period,
)
from app.services.agent_v2.planner import PlannerContext, PlanStep, plan_turn
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state
from app.services.agent_v2.state_rules import (
    apply_booking_branch_change,
    apply_booking_date_change,
)

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
DATE = "2026-10-15"
SERVICE = "service-1"
BRANCH = "branch-1"


def _catalog(
    *,
    start: str,
    end: str,
    include_branch: bool = True,
    second_branch: tuple[str, str] | None = None,
) -> dict[str, object]:
    branches: list[dict[str, object]] = []
    if include_branch:
        branches.append(
            {
                "id": BRANCH,
                "name": "Clinic A",
                "working_hours": [
                    {"weekday": weekday, "start": start, "end": end}
                    for weekday in range(7)
                ],
            }
        )
    if second_branch is not None:
        second_start, second_end = second_branch
        branches.append(
            {
                "id": "branch-2",
                "name": "Clinic B",
                "working_hours": [
                    {"weekday": weekday, "start": second_start, "end": second_end}
                    for weekday in range(7)
                ],
            }
        )
    return {
        "services": [
            {
                "id": SERVICE,
                "name": "Consultation",
                "duration_minutes": 30,
                "requires_laser_device": False,
            }
        ],
        "branches": branches,
    }


def _context(
    *,
    start: str,
    end: str,
    include_branch: bool = True,
    second_branch: tuple[str, str] | None = None,
):
    return build_semantic_context(
        _catalog(
            start=start,
            end=end,
            include_branch=include_branch,
            second_branch=second_branch,
        )
    )


def _authorization(operation: str = "booking") -> WriteAuthorization:
    return WriteAuthorization(
        operation=operation,
        authorized=True,
        source_turn_id="test-turn",
        granted_at=NOW,
    )


def _time(
    clock: str,
    *,
    ambiguity: str = "twelve_hour",
    hint: str = "none",
) -> TimeConstraint:
    return TimeConstraint(
        mode="exact",
        start_time=clock,
        start_time_ambiguity=ambiguity,
        spoken_period_hint=hint,
    )


def _booking(
    clock: str,
    *,
    ambiguity: str = "twelve_hour",
    source: str = "bare_spoken_12h",
    date: str | None = DATE,
    branch_id: str | None = BRANCH,
    hint: str = "none",
) -> BookingTaskState:
    return BookingTaskState(
        write_authorization=_authorization(),
        constraints=CustomerConstraints(
            service_id=SERVICE,
            branch_id=branch_id,
            date=DateConstraint(mode="exact", start_date=date) if date else None,
            time=_time(clock, ambiguity=ambiguity, hint=hint),
            time_clock_source=source,
        ),
        requested_service_ids=[SERVICE],
    )


def _reschedule(
    clock: str,
    *,
    ambiguity: str = "twelve_hour",
    source: str = "bare_spoken_12h",
) -> RescheduleTaskState:
    return RescheduleTaskState(
        write_authorization=_authorization("reschedule"),
        target=RescheduleTarget(
            appointment_id="appointment-1",
            service_id=SERVICE,
        ),
        replacement=CustomerConstraints(
            service_id=SERVICE,
            branch_id=BRANCH,
            date=DateConstraint(mode="exact", start_date=DATE),
            time=_time(clock, ambiguity=ambiguity),
            time_clock_source=source,
        ),
    )


def _assert_write_barrier(step: PlanStep) -> None:
    assert all(read.kind != "availability" for read in step.reads)
    assert step.write_intent is None
    assert step.facts.get("booking_proposal_summary") is None


def _prepared_clock(state: BookingTaskState | RescheduleTaskState) -> str | None:
    constraint = state.constraints if state.task_type == "booking" else state.replacement
    return constraint.time.start_time if constraint.time is not None else None


def _prepared_ambiguity(state: BookingTaskState | RescheduleTaskState) -> str | None:
    constraint = state.constraints if state.task_type == "booking" else state.replacement
    return constraint.time.start_time_ambiguity if constraint.time is not None else None


def test_ap1_rt3_persisted_bare_ten_both_valid_clarifies_before_scheduling() -> None:
    state = _booking("10:00")
    context = _context(start="09:00", end="23:30")

    prepared, gate = prepare_active_task_clock_progress(
        state,
        operation_index=0,
        context=context,
    )
    assert prepared == state
    assert gate is not None
    assert gate.disposition == "clarify"
    assert gate.clarification_field == "time"
    assert gate.facts["valid_operating_hour_candidates"] == ["10:00", "22:00"]
    _assert_write_barrier(gate)

    direct = plan_active_task_progress(state, operation_index=0, context=context)
    assert direct.clarification_field == "time"
    _assert_write_barrier(direct)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("09:00", "21:00", "10:00"),
        ("14:00", "23:30", "22:00"),
    ],
)
def test_ap2_ap3_bare_ten_unique_candidate_is_canonical_before_progression(
    start: str,
    end: str,
    expected: str,
) -> None:
    state = _booking("10:00")
    context = _context(start=start, end=end)

    prepared, gate = prepare_active_task_clock_progress(
        state,
        operation_index=0,
        context=context,
    )
    assert gate is None
    assert _prepared_clock(prepared) == expected
    assert _prepared_ambiguity(prepared) == "none"
    assert prepared.constraints.time_clock_source == "bare_spoken_12h"

    step = plan_active_task_progress(prepared, operation_index=0, context=context)
    availability = [read for read in step.reads if read.kind == "availability"]
    assert availability
    assert availability[0].parameters["time"]["start_time"] == expected
    assert availability[0].parameters["time"]["start_time_ambiguity"] == "none"


def test_ap4_bare_eleven_both_valid_clarifies_without_read_or_write() -> None:
    state = _booking("11:00")
    context = _context(start="09:00", end="23:30")
    step = plan_active_task_progress(state, operation_index=0, context=context)
    assert step.clarification_field == "time"
    assert step.facts["valid_operating_hour_candidates"] == ["11:00", "23:00"]
    _assert_write_barrier(step)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("09:00", "22:00", "11:00"),
        ("14:00", "23:30", "23:00"),
    ],
)
def test_ap5_bare_eleven_unique_candidate_resolves(
    start: str,
    end: str,
    expected: str,
) -> None:
    prepared, gate = prepare_active_task_clock_progress(
        _booking("11:00"),
        operation_index=0,
        context=_context(start=start, end=end),
    )
    assert gate is None
    assert _prepared_clock(prepared) == expected
    assert _prepared_ambiguity(prepared) == "none"


def test_ap6_persisted_bare_three_resolves_to_15_without_clarification() -> None:
    prepared, gate = prepare_active_task_clock_progress(
        _booking("03:00"),
        operation_index=0,
        context=_context(start="10:00", end="22:00"),
    )
    assert gate is None
    assert _prepared_clock(prepared) == "15:00"
    assert _prepared_ambiguity(prepared) == "none"


def test_ap7_persisted_bare_twelve_is_noon_never_midnight() -> None:
    prepared, gate = prepare_active_task_clock_progress(
        _booking("12:00"),
        operation_index=0,
        context=_context(start="10:00", end="22:00"),
    )
    assert gate is None
    assert _prepared_clock(prepared) == "12:00"
    assert _prepared_clock(prepared) != "00:00"
    assert _prepared_ambiguity(prepared) == "none"


def test_ap8_literal_exact_time_cannot_be_reopened_by_clinic_hours() -> None:
    state = _booking(
        "03:00",
        ambiguity="none",
        source="literal_24h",
    )
    context = _context(start="10:00", end="22:00")
    prepared, gate = prepare_active_task_clock_progress(
        state,
        operation_index=0,
        context=context,
    )
    assert gate is None
    assert prepared == state
    step = plan_active_task_progress(prepared, operation_index=0, context=context)
    availability = [read for read in step.reads if read.kind == "availability"]
    assert availability
    assert availability[0].parameters["time"]["start_time"] == "03:00"


def test_ap9_legacy_explicit_period_clock_source_is_read_compatibility_only() -> None:
    state = _booking("10:00")
    payload = state.model_dump(mode="json")
    payload["constraints"]["time_clock_source"] = "explicit_period"
    restored = BookingTaskState.model_validate(payload)
    assert restored.constraints.time_clock_source == "bare_spoken_12h"


def test_ap10_missing_date_gathers_context_without_exact_availability_or_write() -> None:
    state = _booking("10:00", date=None)
    step = plan_active_task_progress(
        state,
        operation_index=0,
        context=_context(start="09:00", end="23:30"),
    )
    assert step.clarification_field == "date"
    _assert_write_barrier(step)


def test_ap10_missing_branch_gathers_branch_before_period_question() -> None:
    state = _booking("10:00", branch_id=None)
    context = _context(
        start="09:00",
        end="21:00",
        second_branch=("14:00", "23:30"),
    )
    step = plan_active_task_progress(state, operation_index=0, context=context)
    assert step.clarification_field == "branch"
    assert step.facts["bare_clock_context_required"] == "branch"
    _assert_write_barrier(step)


def test_ap11_state_json_round_trip_preserves_clock_source_and_safety_gate() -> None:
    original = _booking("10:00")
    restored = BookingTaskState.model_validate(original.model_dump(mode="json"))
    assert restored.constraints.time_clock_source == "bare_spoken_12h"

    step = plan_active_task_progress(
        restored,
        operation_index=0,
        context=_context(start="09:00", end="23:30"),
    )
    assert step.clarification_field == "time"
    _assert_write_barrier(step)

    legacy_payload = original.model_dump(mode="json")
    legacy_payload["constraints"].pop("time_clock_source")
    legacy = BookingTaskState.model_validate(legacy_payload)
    assert legacy.constraints.time_clock_source == "unspecified"
    legacy_step = plan_active_task_progress(
        legacy,
        operation_index=0,
        context=_context(start="09:00", end="23:30"),
    )
    assert legacy_step.clarification_field == "time"
    assert legacy_step.facts["clock_source_missing_or_inconsistent"] is True
    _assert_write_barrier(legacy_step)


@pytest.mark.parametrize(
    ("clock", "hint", "expected"),
    [
        ("10:00", "pm", "22:00"),
        ("10:00", "am", "10:00"),
        ("11:00", "pm", "23:00"),
        ("11:00", "am", "11:00"),
    ],
)
def test_typed_period_hint_resolves_same_booking_task(
    clock: str,
    hint: str,
    expected: str,
) -> None:
    state = _booking(clock)
    context = _context(start="09:00", end="23:30")
    result = resolve_pending_bare_clock_period(state, hint, context=context)
    assert result is not None
    updated, resolved_period = result
    assert isinstance(updated, BookingTaskState)
    assert resolved_period == hint
    assert updated.constraints.service_id == state.constraints.service_id
    assert updated.constraints.branch_id == state.constraints.branch_id
    assert updated.constraints.date == state.constraints.date
    assert updated.requested_service_ids == state.requested_service_ids
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == expected
    assert updated.constraints.time.start_time_ambiguity == "none"
    assert updated.constraints.time.spoken_period_hint == "none"
    assert updated.constraints.time_clock_source == "bare_spoken_12h"
    assert updated.proposal is None


def test_period_hint_requires_both_operational_candidates() -> None:
    state = _booking("10:00")
    assert (
        resolve_pending_bare_clock_period(
            state,
            "pm",
            context=_context(start="09:00", end="21:00"),
        )
        is None
    )


def test_unknown_or_missing_typed_period_hint_keeps_clock_unresolved() -> None:
    state = _booking("10:00")
    context = _context(start="09:00", end="23:30")
    assert resolve_pending_bare_clock_period(state, None, context=context) is None
    step = plan_active_task_progress(state, operation_index=0, context=context)
    assert step.clarification_field == "time"
    assert step.facts["valid_operating_hour_candidates"] == ["10:00", "22:00"]
    _assert_write_barrier(step)


def test_reschedule_unresolved_bare_clock_uses_same_write_barrier() -> None:
    state = _reschedule("10:00")
    step = plan_active_task_progress(
        state,
        operation_index=0,
        context=_context(start="09:00", end="23:30"),
    )
    assert step.clarification_field == "time"
    _assert_write_barrier(step)


def test_unresolved_ambiguous_clock_without_bare_source_fails_closed() -> None:
    state = _booking("10:00", source="unspecified")
    step = plan_active_task_progress(
        state,
        operation_index=0,
        context=_context(start="09:00", end="21:00"),
    )
    assert step.clarification_field == "time"
    assert step.facts["clock_source_missing_or_inconsistent"] is True
    _assert_write_barrier(step)


def test_typed_period_hint_resolves_reschedule_same_boundary() -> None:
    state = _reschedule("10:00")
    result = resolve_pending_bare_clock_period(
        state,
        "pm",
        context=_context(start="09:00", end="23:30"),
    )
    assert result is not None
    updated, period = result
    assert isinstance(updated, RescheduleTaskState)
    assert period == "pm"
    assert updated.target == state.target
    assert updated.replacement.service_id == state.replacement.service_id
    assert updated.replacement.date == state.replacement.date
    assert updated.replacement.time is not None
    assert updated.replacement.time.start_time == "22:00"
    assert updated.replacement.time.start_time_ambiguity == "none"
    assert updated.replacement.time_clock_source == "bare_spoken_12h"


def test_p1_missing_date_persists_explicit_pm_hint_until_backend_can_resolve() -> None:
    state = _booking("10:30", date=None, hint="pm")
    restored = BookingTaskState.model_validate(state_persistence._task_namespace(state)["active_task"])
    assert restored.constraints.time is not None
    assert restored.constraints.time.spoken_period_hint == "pm"
    dated = apply_booking_date_change(
        restored, date=DateConstraint(mode="exact", start_date=DATE)
    )
    prepared, gate = prepare_active_task_clock_progress(
        dated, operation_index=0, context=_context(start="00:00", end="23:59")
    )
    assert gate is None
    assert prepared.constraints.time is not None
    assert prepared.constraints.time.start_time == "22:30"
    assert prepared.constraints.time.start_time_ambiguity == "none"
    assert prepared.constraints.time.spoken_period_hint == "none"
    persisted_resolved = state_persistence._task_namespace(prepared)["active_task"]
    assert "spoken_period_hint" not in persisted_resolved["constraints"]["time"]


def test_p2_missing_branch_persists_pm_hint_until_selected_branch_resolves() -> None:
    state = _booking("10:30", branch_id=None, hint="pm")
    restored = BookingTaskState.model_validate(state_persistence._task_namespace(state)["active_task"])
    assert restored.constraints.time is not None
    assert restored.constraints.time.spoken_period_hint == "pm"
    selected = apply_booking_branch_change(restored, branch_id=BRANCH)
    prepared, gate = prepare_active_task_clock_progress(
        selected,
        operation_index=0,
        context=_context(start="00:00", end="23:59", second_branch=("09:00", "21:00")),
    )
    assert gate is None
    assert prepared.constraints.time is not None
    assert prepared.constraints.time.start_time == "22:30"
    assert prepared.constraints.time.spoken_period_hint == "none"


def test_p3_side_question_round_trip_preserves_unresolved_pm_hint() -> None:
    state = _booking("10:30", branch_id=None, hint="pm")
    after_first_persist = BookingTaskState.model_validate(state_persistence._task_namespace(state)["active_task"])
    after_side_question_persist = BookingTaskState.model_validate(
        state_persistence._task_namespace(after_first_persist)["active_task"]
    )
    assert after_side_question_persist.constraints.time is not None
    assert after_side_question_persist.constraints.time.spoken_period_hint == "pm"
    selected = apply_booking_branch_change(after_side_question_persist, branch_id=BRANCH)
    prepared, gate = prepare_active_task_clock_progress(
        selected, operation_index=0, context=_context(start="00:00", end="23:59")
    )
    assert gate is None
    assert prepared.constraints.time is not None
    assert prepared.constraints.time.start_time == "22:30"


def test_p4_bare_both_valid_clarification_pm_resolves_same_task() -> None:
    state = _booking("10:30")
    context = _context(start="00:00", end="23:59")
    prepared, gate = prepare_active_task_clock_progress(
        state, operation_index=0, context=context
    )
    assert gate is not None
    assert gate.clarification_field == "time"
    _assert_write_barrier(gate)
    result = resolve_pending_bare_clock_period(prepared, "pm", context=context)
    assert result is not None
    updated, _ = result
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "22:30"
    assert updated.constraints.time.spoken_period_hint == "none"


def test_p5_bare_both_valid_clarification_am_resolves_same_task() -> None:
    state = _booking("10:30")
    context = _context(start="00:00", end="23:59")
    result = resolve_pending_bare_clock_period(state, "am", context=context)
    assert result is not None
    updated, _ = result
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "10:30"
    assert updated.constraints.time.spoken_period_hint == "none"




def test_explicit_pm_without_date_stays_pending_then_clarifies_after_date() -> None:
    context = _context(start="09:00", end="21:00")
    resolution = resolve_bare_clock_constraint(
        _time("10:30", hint="pm"),
        date_constraint=None,
        context=context,
        branch_id=BRANCH,
    )
    assert resolution.status == "needs_context"
    assert resolution.constraint is not None
    assert resolution.constraint.start_time == "10:30"
    assert resolution.constraint.start_time_ambiguity == "twelve_hour"
    assert resolution.constraint.spoken_period_hint == "pm"

    loaded = _booking(
        resolution.constraint.start_time,
        ambiguity=resolution.constraint.start_time_ambiguity,
        date=None,
        hint=resolution.constraint.spoken_period_hint,
    )
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(date=DateConstraint(mode="exact", start_date=DATE)),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date"],
        continues_previous=True,
    )
    planner_step = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(semantic_context=context, active_task=loaded, now=NOW),
    ).steps[0]
    adapted = adapt_matching_active_task_step(
        planner_step,
        operation=operation,
        active_task=loaded,
        context=context,
        now=NOW,
    )
    transition = apply_step_state(
        loaded,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-explicit-pm-date-only",
    )
    updated = transition.active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "10:30"
    assert updated.constraints.time.start_time_ambiguity == "twelve_hour"
    assert updated.constraints.time.spoken_period_hint == "pm"

    prepared, gate = prepare_active_task_clock_progress(
        updated,
        operation_index=0,
        context=context,
    )
    assert prepared == updated
    assert gate is not None
    assert gate.disposition == "clarify"
    assert gate.clarification_field == "time"
    assert gate.write_intent is None
    assert gate.facts["valid_operating_hour_candidates"] == ["10:30"]


def test_p1_actual_date_only_pipeline_preserves_pm_until_clock_progression() -> None:
    loaded = BookingTaskState.model_validate(
        state_persistence._task_namespace(
            _booking("10:30", date=None, branch_id=None, hint="pm")
        )["active_task"]
    )
    assert loaded.constraints.time is not None
    assert loaded.constraints.time.spoken_period_hint == "pm"
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(date=DateConstraint(mode="exact", start_date=DATE)),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date"],
        continues_previous=True,
    )
    context = _context(start="00:00", end="23:59")
    planner_step = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(semantic_context=context, active_task=loaded, now=NOW),
    ).steps[0]
    adapted = adapt_matching_active_task_step(
        planner_step,
        operation=operation,
        active_task=loaded,
        context=context,
        now=NOW,
    )
    transition = apply_step_state(
        loaded,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-p1-date",
    )
    after_update = transition.active_task
    assert isinstance(after_update, BookingTaskState)
    assert after_update.constraints.time is not None
    assert after_update.constraints.time.start_time == "10:30"
    assert after_update.constraints.time.start_time_ambiguity == "twelve_hour"
    assert after_update.constraints.time.spoken_period_hint == "pm"
    assert after_update.version == loaded.version + 1

    prepared, gate = prepare_active_task_clock_progress(
        after_update,
        operation_index=0,
        context=context,
    )
    assert gate is None
    assert prepared.constraints.time is not None
    assert prepared.constraints.time.start_time == "22:30"
    assert prepared.constraints.time.start_time_ambiguity == "none"
    assert prepared.constraints.time.spoken_period_hint == "none"


def test_date_only_echo_survives_two_persistence_cycles_before_context_complete() -> None:
    state = _booking("10:30", date=None, branch_id=None, hint="pm")
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(date=DateConstraint(mode="exact", start_date=DATE)),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date"],
        continues_previous=True,
    )
    stripped = TimeConstraint(
        mode="exact", start_time="10:30", start_time_ambiguity="twelve_hour"
    ).model_dump(mode="json")
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts={
            "date": operation.entities.date.model_dump(mode="json"),
            "time": stripped,
        },
    )
    updated = apply_step_state(
        state,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-cycle-date",
    ).active_task
    assert isinstance(updated, BookingTaskState)
    one = BookingTaskState.model_validate(
        state_persistence._task_namespace(updated)["active_task"]
    )
    two = BookingTaskState.model_validate(
        state_persistence._task_namespace(one)["active_task"]
    )
    assert two.constraints.time is not None
    assert two.constraints.time.start_time == "10:30"
    assert two.constraints.time.start_time_ambiguity == "twelve_hour"
    assert two.constraints.time.spoken_period_hint == "pm"


@pytest.mark.parametrize(
    ("clock", "hint", "expected"),
    [
        ("10:30", "am", "10:30"),
        ("10:30", "pm", "22:30"),
        ("11:30", "pm", "23:30"),
    ],
)
def test_date_only_continuation_generic_minutes_preserves_period_then_resolves(
    clock: str, hint: str, expected: str
) -> None:
    state = _booking(clock, date=None, hint=hint)
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(date=DateConstraint(mode="exact", start_date=DATE)),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date"],
    )
    stripped = TimeConstraint(
        mode="exact", start_time=clock, start_time_ambiguity="twelve_hour"
    ).model_dump(mode="json")
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        facts={"date": operation.entities.date.model_dump(mode="json"), "time": stripped},
    )
    updated = apply_step_state(
        state, step=step, operation=operation, reads=None, now=NOW, turn_id="turn-minutes"
    ).active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is not None
    assert updated.constraints.time.spoken_period_hint == hint
    prepared, gate = prepare_active_task_clock_progress(
        updated, operation_index=0, context=_context(start="00:00", end="23:59")
    )
    assert gate is None
    assert prepared.constraints.time is not None
    assert prepared.constraints.time.start_time == expected
    assert prepared.constraints.time.spoken_period_hint == "none"


def test_past_temporal_resume_producer_marks_and_applies_intentional_time_clear() -> None:
    state = _booking("10:30", date="2026-10-14", hint="pm")
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="execute",
        active_task_relationship="continue",
        continues_previous=True,
    )
    seed = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts={},
    )
    adapted = adapt_matching_active_task_step(
        seed,
        operation=operation,
        active_task=state,
        context=_context(start="00:00", end="23:59"),
        now=datetime(2026, 10, 16, 10, 0, tzinfo=UTC),
    )
    assert adapted.facts["date"] is None
    assert adapted.facts["time"] is None
    assert adapted.facts["_backend_time_state_action"] == "clear_past"
    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=datetime(2026, 10, 16, 10, 0, tzinfo=UTC),
        turn_id="turn-past-resume",
    ).active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.date is None
    assert updated.constraints.time is None
