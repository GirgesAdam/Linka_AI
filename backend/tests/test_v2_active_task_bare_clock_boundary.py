from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    TimeConstraint,
)
from app.services.agent_v2.active_task_progress import (
    plan_active_task_progress,
    prepare_active_task_clock_progress,
)
from app.services.agent_v2.planner import PlanStep
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
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
) -> TimeConstraint:
    return TimeConstraint(
        mode="exact",
        start_time=clock,
        start_time_ambiguity=ambiguity,
    )


def _booking(
    clock: str,
    *,
    ambiguity: str = "twelve_hour",
    source: str = "bare_spoken_12h",
    date: str | None = DATE,
    branch_id: str | None = BRANCH,
) -> BookingTaskState:
    return BookingTaskState(
        write_authorization=_authorization(),
        constraints=CustomerConstraints(
            service_id=SERVICE,
            branch_id=branch_id,
            date=DateConstraint(mode="exact", start_date=date) if date else None,
            time=_time(clock, ambiguity=ambiguity),
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


@pytest.mark.parametrize(("hint", "expected"), [("am", "10:00"), ("pm", "22:00")])
def test_typed_period_hint_resolves_existing_10_ambiguity_without_new_state_machine(
    hint: str, expected: str
) -> None:
    state = _booking("10:00")
    assert state.constraints.time is not None
    hinted_time = state.constraints.time.model_copy(update={"spoken_period_hint": hint})
    state = state.model_copy(
        update={
            "constraints": state.constraints.model_copy(update={"time": hinted_time})
        }
    )
    prepared, gate = prepare_active_task_clock_progress(
        state,
        operation_index=0,
        context=_context(start="09:00", end="23:30"),
    )
    assert gate is None
    assert _prepared_clock(prepared) == expected
    assert _prepared_ambiguity(prepared) == "none"
    assert prepared.constraints.time_clock_source == "bare_spoken_12h"
