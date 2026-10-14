from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.time_resolution import (
    classify_operation_bare_clock,
    resolve_turn_times_by_clinic_hours,
)
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import _normalize_time_semantics_v2
from app.agents.v2.turn_normalization import normalize_semantic_invariants
from app.services.agent_v2.planner import PlannerContext, plan_turn

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
DATE = "2026-10-15"


def _catalog(
    *,
    start: str,
    end: str,
    second_branch: tuple[str, str] | None = None,
) -> dict[str, object]:
    branches: list[dict[str, object]] = [
        {
            "id": "branch-1",
            "name": "Clinic A",
            "working_hours": [
                {"weekday": weekday, "start": start, "end": end}
                for weekday in range(7)
            ],
        }
    ]
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
                "id": "service-1",
                "name": "Consultation",
                "duration_minutes": 30,
            }
        ],
        "branches": branches,
    }


def _turn(
    clock: str,
    *,
    ambiguity: str = "none",
    period: str = "none",
    date: str | None = DATE,
    branch_ref: str | None = None,
) -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(ref="S1"),
                    branch=(EntityReference(ref=branch_ref) if branch_ref else None),
                    date=(
                        DateConstraint(mode="exact", start_date=date)
                        if date is not None
                        else None
                    ),
                    time=TimeConstraint(
                        mode="exact",
                        start_time=clock,
                        start_time_ambiguity=ambiguity,
                        explicit_period=period,
                    ),
                ),
            )
        ]
    )


def _pipeline(
    turn: TiaTurnUnderstanding,
    *,
    start: str,
    end: str,
    second_branch: tuple[str, str] | None = None,
):
    context = build_semantic_context(
        _catalog(start=start, end=end, second_branch=second_branch)
    )
    time_safe = _normalize_time_semantics_v2(turn, latest_customer_text="")
    normalized = normalize_semantic_invariants(time_safe)
    resolved = resolve_turn_times_by_clinic_hours(normalized, context)
    return resolved, context


def _clock(turn: TiaTurnUnderstanding) -> str:
    constraint = turn.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time is not None
    return constraint.start_time


def _ambiguity(turn: TiaTurnUnderstanding) -> str:
    constraint = turn.operations[0].entities.time
    assert constraint is not None
    return constraint.start_time_ambiguity


def _plan(turn: TiaTurnUnderstanding, context):
    return plan_turn(
        turn,
        PlannerContext(
            semantic_context=context,
            active_task=None,
            now=NOW,
        ),
    ).steps[0]


def test_b1_bare_three_normal_clinic_resolves_to_15_without_clarification() -> None:
    turn, context = _pipeline(
        _turn("03:00", ambiguity="twelve_hour"),
        start="10:00",
        end="22:00",
    )
    assert _clock(turn) == "15:00"
    assert _ambiguity(turn) == "none"
    step = _plan(turn, context)
    assert step.disposition == "read"
    assert step.clarification_field is None


def test_t2_bare_three_uses_am_when_pm_invalid_and_am_uniquely_valid() -> None:
    turn, context = _pipeline(
        _turn("03:00", ambiguity="twelve_hour"),
        start="00:00",
        end="12:00",
    )
    assert _clock(turn) == "03:00"
    assert _ambiguity(turn) == "none"
    assert _plan(turn, context).clarification_field is None


def test_b2_bare_nine_prefers_21_when_clinic_allows_it() -> None:
    turn, _context = _pipeline(
        _turn("09:00", ambiguity="twelve_hour"),
        start="10:00",
        end="22:00",
    )
    assert _clock(turn) == "21:00"


def test_b3_bare_ten_morning_only_resolves_without_clarification() -> None:
    turn, context = _pipeline(
        _turn("10:00", ambiguity="twelve_hour"),
        start="09:00",
        end="21:00",
    )
    assert _clock(turn) == "10:00"
    assert _plan(turn, context).clarification_field is None


def test_b4_bare_ten_night_only_resolves_without_clarification() -> None:
    turn, context = _pipeline(
        _turn("10:00", ambiguity="twelve_hour"),
        start="14:00",
        end="23:30",
    )
    assert _clock(turn) == "22:00"
    assert _plan(turn, context).clarification_field is None


def test_b5_bare_ten_both_possible_requires_period_clarification_and_no_write() -> None:
    turn, context = _pipeline(
        _turn("10:00", ambiguity="twelve_hour"),
        start="09:00",
        end="23:30",
    )
    assert _clock(turn) == "10:00"
    assert _ambiguity(turn) == "twelve_hour"
    resolution = classify_operation_bare_clock(turn.operations[0], context)
    assert resolution.status == "clarify_period"
    assert resolution.valid_operating_hour_candidates == ("10:00", "22:00")
    step = _plan(turn, context)
    assert step.disposition == "clarify"
    assert step.clarification_field == "time"
    assert step.write_intent is None
    assert step.facts["clock_period_clarification"] is True
    assert step.facts["candidate_clocks"] == ["10:00", "22:00"]


def test_b6_bare_eleven_morning_only_resolves_to_11() -> None:
    turn, context = _pipeline(
        _turn("11:00", ambiguity="twelve_hour"),
        start="09:00",
        end="22:00",
    )
    assert _clock(turn) == "11:00"
    assert _plan(turn, context).clarification_field is None


def test_b7_bare_eleven_night_only_resolves_to_23() -> None:
    turn, context = _pipeline(
        _turn("11:00", ambiguity="twelve_hour"),
        start="14:00",
        end="23:30",
    )
    assert _clock(turn) == "23:00"
    assert _plan(turn, context).clarification_field is None


def test_b8_bare_eleven_both_possible_requires_clarification() -> None:
    turn, context = _pipeline(
        _turn("11:00", ambiguity="twelve_hour"),
        start="09:00",
        end="23:30",
    )
    step = _plan(turn, context)
    assert step.disposition == "clarify"
    assert step.clarification_field == "time"
    assert step.write_intent is None
    assert step.facts["valid_operating_hour_candidates"] == ["11:00", "23:00"]


@pytest.mark.parametrize("clock", ["10:00", "11:00"])
def test_b9_bare_ten_or_eleven_neither_possible_fails_closed(clock: str) -> None:
    turn, context = _pipeline(
        _turn(clock, ambiguity="twelve_hour"),
        start="12:00",
        end="20:00",
    )
    step = _plan(turn, context)
    assert step.disposition == "blocked"
    assert step.response_goal == "requested_time_unavailable"
    assert step.write_intent is None
    assert step.facts["clock_outside_operating_hours"] is True
    assert step.facts["valid_operating_hour_candidates"] == []


def test_b10_bare_twelve_is_noon_never_midnight() -> None:
    turn, _context = _pipeline(
        _turn("12:00", ambiguity="twelve_hour"),
        start="14:00",
        end="20:00",
    )
    assert _clock(turn) == "12:00"
    assert _ambiguity(turn) == "none"


def test_b11_literal_0300_remains_exact_even_when_1500_is_inside_hours() -> None:
    turn, _context = _pipeline(
        _turn("03:00", ),
        start="10:00",
        end="22:00",
    )
    assert _clock(turn) == "03:00"
    assert _ambiguity(turn) == "none"


def test_b11_exact_clock_is_not_reopened_without_twelve_hour_ambiguity() -> None:
    turn, _context = _pipeline(
        _turn("03:00", ambiguity="none"),
        start="10:00",
        end="22:00",
    )
    assert _clock(turn) == "03:00"
    assert _ambiguity(turn) == "none"


def test_branch_specific_hours_resolve_bare_ten_from_the_grounded_branch() -> None:
    turn, context = _pipeline(
        _turn(
            "10:00",
            ambiguity="twelve_hour",
            branch_ref="B2",
        ),
        start="09:00",
        end="21:00",
        second_branch=("14:00", "23:30"),
    )
    assert _clock(turn) == "22:00"
    assert _ambiguity(turn) == "none"


def test_b12_literal_1615_remains_exact() -> None:
    turn, _context = _pipeline(
        _turn("16:15", ),
        start="10:00",
        end="22:00",
    )
    assert _clock(turn) == "16:15"


def test_b13_explicit_pm_is_backend_canonicalized_to_1615() -> None:
    turn, _context = _pipeline(
        _turn(
            "04:15",
            period="pm",
        ),
        start="10:00",
        end="22:00",
    )
    assert _clock(turn) == "16:15"
    assert turn.operations[0].entities.time.explicit_period == "pm"


def test_t12_explicit_three_pm_semantics_canonicalize_to_1500() -> None:
    turn, _context = _pipeline(
        _turn("03:00", period="pm"),
        start="10:00",
        end="22:00",
    )
    assert _clock(turn) == "15:00"
    assert turn.operations[0].entities.time is not None
    assert turn.operations[0].entities.time.explicit_period == "pm"


def test_b14_explicit_am_stays_0415() -> None:
    turn, _context = _pipeline(
        _turn(
            "04:15",
            period="am",
        ),
        start="10:00",
        end="22:00",
    )
    assert _clock(turn) == "04:15"
    assert turn.operations[0].entities.time.explicit_period == "am"


def test_b15_backend_canonicalization_uses_typed_semantics_not_customer_text() -> None:
    turn = _turn(
        "04:15",
        period="pm",
    )
    canonical = _normalize_time_semantics_v2(turn, latest_customer_text="")
    assert _clock(canonical) == "16:15"
    assert canonical.operations[0].entities.time.explicit_period == "pm"


def test_b16_explicit_11_at_night_stays_2300_then_availability_can_reject_it() -> None:
    turn, context = _pipeline(
        _turn(
            "11:00",
            period="pm",
        ),
        start="09:00",
        end="21:00",
    )
    assert _clock(turn) == "23:00"
    step = _plan(turn, context)
    assert step.disposition == "read"
    assert step.write_intent is None
    availability = next(read for read in step.reads if read.kind == "availability")
    assert availability.parameters["time"]["start_time"] == "23:00"
    assert step.facts["exact_time_requested"] is True
    assert step.facts["booking_next_field"] == "booking"


def test_bare_clock_waits_for_date_instead_of_premature_am_pm_question() -> None:
    turn, context = _pipeline(
        _turn(
            "10:00",
            ambiguity="twelve_hour",
            date=None,
        ),
        start="09:00",
        end="23:30",
    )
    assert _ambiguity(turn) == "twelve_hour"
    resolution = classify_operation_bare_clock(turn.operations[0], context)
    assert resolution.status == "needs_context"
    step = _plan(turn, context)
    assert step.disposition == "read"
    assert step.clarification_field is None
    assert step.write_intent is None
    assert step.facts["booking_next_field"] == "date"
    assert step.facts["time"]["start_time"] == "10:00"
    assert step.facts["time"]["start_time_ambiguity"] == "twelve_hour"


def test_bare_clock_waits_for_branch_when_branch_schedules_differ() -> None:
    turn, context = _pipeline(
        _turn("10:00", ambiguity="twelve_hour"),
        start="09:00",
        end="21:00",
        second_branch=("14:00", "23:30"),
    )
    assert _ambiguity(turn) == "twelve_hour"
    resolution = classify_operation_bare_clock(turn.operations[0], context)
    assert resolution.status == "needs_context"
    step = _plan(turn, context)
    assert step.disposition == "clarify"
    assert step.clarification_field == "branch"
    assert step.write_intent is None


def test_explicit_midnight_typed_semantics_canonicalize_to_0000() -> None:
    turn, _context = _pipeline(
        _turn(
            "12:00",
            period="midnight",
        ),
        start="09:00",
        end="21:00",
    )
    assert _clock(turn) == "00:00"
    assert turn.operations[0].entities.time.explicit_period == "midnight"


def test_explicit_noon_typed_semantics_canonicalize_to_1200() -> None:
    turn, _context = _pipeline(
        _turn(
            "12:00",
            period="noon",
        ),
        start="09:00",
        end="21:00",
    )
    assert _clock(turn) == "12:00"
    assert turn.operations[0].entities.time.explicit_period == "noon"
