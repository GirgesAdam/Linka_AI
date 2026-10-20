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
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import _normalize_time_semantics_v2
from app.agents.v2.turn_normalization import normalize_semantic_invariants
from app.services.agent_v2.outcome_builder import build_step_outcome
from app.services.agent_v2.planner import PlannerContext, plan_turn

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
DATE = "2026-10-24"


def _catalog(*, start: str, end: str, weekdays: tuple[int, ...] = tuple(range(7))) -> dict[str, object]:
    return {
        "services": [{"id": "service-1", "name": "Consultation", "duration_minutes": 30}],
        "branches": [
            {
                "id": "branch-1",
                "name": "Clinic A",
                "working_hours": [
                    {"weekday": weekday, "start": start, "end": end}
                    for weekday in weekdays
                ],
            }
        ],
    }


def _turn(
    clock: str | None,
    *,
    hint: str = "none",
    op_type: str = "book",
    selection: Selection | None = None,
    date: DateConstraint | None = None,
) -> TiaTurnUnderstanding:
    time = None
    if clock is not None:
        time = TimeConstraint(mode="exact", start_time=clock, spoken_period_hint=hint)
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type=op_type,
                entities=TurnEntities(
                    service=EntityReference(ref="S1"),
                    branch=EntityReference(ref="B1"),
                    date=date or DateConstraint(mode="exact", start_date=DATE),
                    time=time,
                ),
                selection=selection,
            )
        ]
    )


def _pipeline(
    clock: str,
    *,
    text: str,
    hint: str,
    start: str,
    end: str,
    op_type: str = "book",
):
    context = build_semantic_context(_catalog(start=start, end=end))
    raw = _turn(clock, hint=hint, op_type=op_type)
    normalized = _normalize_time_semantics_v2(raw, latest_customer_text=text)
    normalized = normalize_semantic_invariants(normalized)
    resolution = classify_operation_bare_clock(normalized.operations[0], context)
    resolved = resolve_turn_times_by_clinic_hours(normalized, context)
    step = plan_turn(
        resolved,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    ).steps[0]
    return raw, normalized, resolution, resolved, step, context


def _time(turn: TiaTurnUnderstanding) -> TimeConstraint:
    value = turn.operations[0].entities.time
    assert value is not None
    return value


def test_r1_explicit_9_am_feasible() -> None:
    _, normalized, resolution, resolved, step, _ = _pipeline(
        "09:00", text="9 AM", hint="am", start="09:00", end="22:00"
    )
    assert _time(normalized).spoken_period_hint == "am"
    assert resolution.status == "resolved"
    assert _time(resolved).start_time == "09:00"
    assert _time(resolved).start_time_ambiguity == "none"
    assert step.disposition == "read"
    assert step.write_intent is None


def test_r2_explicit_9_pm_feasible() -> None:
    _, normalized, resolution, resolved, step, _ = _pipeline(
        "21:00", text="9 PM", hint="pm", start="10:00", end="22:00"
    )
    assert _time(normalized).spoken_period_hint == "pm"
    assert resolution.status == "resolved"
    assert _time(resolved).start_time == "21:00"
    assert step.disposition == "read"
    assert step.write_intent is None


@pytest.mark.parametrize(("clock", "text"), [("01:00", "1 AM"), ("03:00", "3 AM")])
def test_r3_r4_explicit_am_infeasible_pm_feasible_clarifies(clock: str, text: str) -> None:
    _, normalized, resolution, resolved, step, _ = _pipeline(
        clock, text=text, hint="am", start="10:00", end="22:00"
    )
    assert _time(normalized).spoken_period_hint == "am"
    assert resolution.status == "clarify_period"
    assert len(resolution.valid_operating_hour_candidates) == 1
    assert resolution.valid_operating_hour_candidates[0] in {"13:00", "15:00"}
    assert _time(resolved).start_time == clock
    assert _time(resolved).start_time_ambiguity == "twelve_hour"
    assert _time(resolved).spoken_period_hint == "am"
    assert step.disposition == "clarify"
    assert step.clarification_field == "time"
    assert step.write_intent is None
    assert all(read.kind != "availability" for read in step.reads)
    assert step.facts["clock_period_clarification"] is True


def test_r5_explicit_9_am_both_periods_infeasible_is_unavailable() -> None:
    _, normalized, resolution, resolved, step, _ = _pipeline(
        "09:00", text="9 AM", hint="am", start="10:00", end="18:00"
    )
    assert _time(normalized).spoken_period_hint == "am"
    assert resolution.status == "outside_operating_hours"
    assert resolution.valid_operating_hour_candidates == ()
    assert _time(resolved).start_time == "09:00"
    assert _time(resolved).spoken_period_hint == "am"
    assert step.disposition == "blocked"
    assert step.response_goal == "requested_time_unavailable"
    assert step.write_intent is None
    assert all(read.kind != "availability" for read in step.reads)


@pytest.mark.parametrize(("clock", "expected"), [("03:00", "15:00"), ("09:00", "21:00")])
def test_r6_r7_bare_clock_unique_pm_candidate_still_resolves(clock: str, expected: str) -> None:
    _, normalized, resolution, resolved, step, _ = _pipeline(
        clock, text=f"at {int(clock[:2])}", hint="none", start="10:00", end="22:00"
    )
    assert _time(normalized).spoken_period_hint == "none"
    assert resolution.status == "resolved"
    assert _time(resolved).start_time == expected
    assert _time(resolved).start_time_ambiguity == "none"
    assert step.write_intent is None


@pytest.mark.parametrize(
    ("clock", "text", "hint", "expected"),
    [
        ("10:00", "10 AM", "am", "10:00"),
        ("22:00", "10 PM", "pm", "22:00"),
        ("11:00", "11 AM", "am", "11:00"),
        ("23:00", "11 PM", "pm", "23:00"),
        ("12:00", "12 noon", "none", "12:00"),
        ("00:00", "12 midnight", "midnight", "00:00"),
    ],
)
def test_r8_to_r13_explicit_period_semantics_remain_exact(
    clock: str, text: str, hint: str, expected: str
) -> None:
    _, _, resolution, resolved, step, _ = _pipeline(
        clock, text=text, hint=hint, start="00:00", end="23:59"
    )
    assert resolution.status == "resolved"
    assert _time(resolved).start_time == expected
    assert _time(resolved).start_time_ambiguity == "none"
    assert step.write_intent is None


def test_bare_10_with_both_periods_feasible_still_clarifies() -> None:
    _, _, resolution, resolved, step, _ = _pipeline(
        "10:00", text="at 10", hint="none", start="00:00", end="23:59"
    )
    assert resolution.status == "clarify_period"
    assert _time(resolved).start_time_ambiguity == "twelve_hour"
    assert step.disposition == "clarify"
    assert step.write_intent is None


def test_literal_0100_is_never_reinterpreted_as_1300() -> None:
    _, normalized, resolution, resolved, step, _ = _pipeline(
        "13:00", text="01:00", hint="pm", start="10:00", end="22:00"
    )
    assert _time(normalized).start_time == "01:00"
    assert _time(normalized).start_time_ambiguity == "none"
    assert _time(normalized).spoken_period_hint == "none"
    assert resolution.status == "not_applicable"
    assert _time(resolved).start_time == "01:00"
    assert step.write_intent is None


def test_literal_1300_remains_1300() -> None:
    _, normalized, resolution, resolved, step, _ = _pipeline(
        "01:00", text="13:00", hint="am", start="10:00", end="22:00"
    )
    assert _time(normalized).start_time == "13:00"
    assert _time(normalized).start_time_ambiguity == "none"
    assert resolution.status == "not_applicable"
    assert _time(resolved).start_time == "13:00"
    assert step.write_intent is None


def test_selection_explicit_am_conflict_never_auto_selects_pm() -> None:
    context = build_semantic_context(_catalog(start="10:00", end="22:00"))
    raw = _turn(
        None,
        op_type="continue_active",
        selection=Selection(kind="time", time="09:00", spoken_period_hint="am"),
    )
    normalized = _normalize_time_semantics_v2(raw, latest_customer_text="9 AM")
    selection = normalized.operations[0].selection
    assert selection is not None
    assert selection.time == "09:00"
    assert selection.time_ambiguity == "twelve_hour"
    assert selection.spoken_period_hint == "am"

    resolved = resolve_turn_times_by_clinic_hours(normalized, context)
    selection = resolved.operations[0].selection
    assert selection is not None
    assert selection.time == "09:00"
    assert selection.time != "21:00"
    assert selection.time_ambiguity == "twelve_hour"
    assert selection.spoken_period_hint == "am"


@pytest.mark.parametrize(
    ("clock", "text", "hint", "start", "end", "expected"),
    [
        ("01:00", "1 AM", "am", "00:00", "05:00", "01:00"),
        ("13:00", "1 PM", "pm", "10:00", "22:00", "13:00"),
    ],
)
def test_explicit_1_period_resolves_when_literal_period_is_feasible(
    clock: str,
    text: str,
    hint: str,
    start: str,
    end: str,
    expected: str,
) -> None:
    _, _, resolution, resolved, step, _ = _pipeline(
        clock, text=text, hint=hint, start=start, end=end
    )
    assert resolution.status == "resolved"
    assert _time(resolved).start_time == expected
    assert _time(resolved).start_time_ambiguity == "none"
    assert _time(resolved).spoken_period_hint == "none"
    assert step.write_intent is None


def test_explicit_1_pm_infeasible_am_feasible_clarifies_without_flip() -> None:
    _, normalized, resolution, resolved, step, _ = _pipeline(
        "13:00", text="1 PM", hint="pm", start="00:00", end="05:00"
    )
    assert _time(normalized).start_time == "01:00"
    assert _time(normalized).spoken_period_hint == "pm"
    assert resolution.status == "clarify_period"
    assert resolution.valid_operating_hour_candidates == ("01:00",)
    assert _time(resolved).start_time == "01:00"
    assert _time(resolved).start_time_ambiguity == "twelve_hour"
    assert _time(resolved).spoken_period_hint == "pm"
    assert step.disposition == "clarify"
    assert step.write_intent is None
    assert all(read.kind != "availability" for read in step.reads)


def test_explicit_9_am_infeasible_pm_feasible_clarifies_without_searching_pm() -> None:
    _, _, resolution, resolved, step, _ = _pipeline(
        "09:00", text="9 AM", hint="am", start="10:00", end="22:00"
    )
    assert resolution.status == "clarify_period"
    assert resolution.valid_operating_hour_candidates == ("21:00",)
    assert _time(resolved).start_time == "09:00"
    assert _time(resolved).spoken_period_hint == "am"
    assert step.disposition == "clarify"
    assert step.write_intent is None
    assert all(read.kind != "availability" for read in step.reads)


def test_bare_1_only_pm_feasible_resolves_to_13() -> None:
    _, _, resolution, resolved, step, _ = _pipeline(
        "01:00", text="at 1", hint="none", start="10:00", end="22:00"
    )
    assert resolution.status == "resolved"
    assert _time(resolved).start_time == "13:00"
    assert step.write_intent is None


def test_bare_10_with_one_feasible_candidate_resolves_without_clarification() -> None:
    _, _, resolution, resolved, step, _ = _pipeline(
        "10:00", text="at 10", hint="none", start="09:00", end="21:00"
    )
    assert resolution.status == "resolved"
    assert _time(resolved).start_time == "10:00"
    assert _time(resolved).start_time_ambiguity == "none"
    assert step.disposition != "clarify"
    assert step.write_intent is None


def test_exact_requested_weekday_controls_explicit_period_feasibility() -> None:
    catalog = {
        "services": [{"id": "service-1", "name": "Consultation", "duration_minutes": 30}],
        "branches": [
            {
                "id": "branch-1",
                "name": "Clinic A",
                "working_hours": [
                    {"weekday": 5, "start": "10:00", "end": "22:00"},
                    {"weekday": 6, "start": "00:00", "end": "05:00"},
                ],
            }
        ],
    }
    context = build_semantic_context(catalog)
    saturday = _normalize_time_semantics_v2(
        _turn("01:00", hint="am", date=DateConstraint(mode="exact", start_date="2026-10-24")),
        latest_customer_text="Saturday 1 AM",
    )
    sunday = _normalize_time_semantics_v2(
        _turn("01:00", hint="am", date=DateConstraint(mode="exact", start_date="2026-10-25")),
        latest_customer_text="Sunday 1 AM",
    )
    saturday_resolution = classify_operation_bare_clock(saturday.operations[0], context)
    sunday_resolution = classify_operation_bare_clock(sunday.operations[0], context)
    assert saturday_resolution.status == "clarify_period"
    assert saturday_resolution.valid_operating_hour_candidates == ("13:00",)
    assert sunday_resolution.status == "resolved"
    assert sunday_resolution.constraint is not None
    assert sunday_resolution.constraint.start_time == "01:00"


def test_selected_branch_hours_outrank_other_branch_hours() -> None:
    catalog = {
        "services": [{"id": "service-1", "name": "Consultation", "duration_minutes": 30}],
        "branches": [
            {
                "id": "branch-pm",
                "name": "PM Clinic",
                "working_hours": [
                    {"weekday": weekday, "start": "10:00", "end": "22:00"}
                    for weekday in range(7)
                ],
            },
            {
                "id": "branch-am",
                "name": "AM Clinic",
                "working_hours": [
                    {"weekday": weekday, "start": "00:00", "end": "05:00"}
                    for weekday in range(7)
                ],
            },
        ],
    }
    context = build_semantic_context(catalog)
    branch_refs = {
        target.canonical_id: ref
        for ref, target in context.reference_map.items()
        if target.kind == "branch"
    }
    assert set(branch_refs) == {"branch-pm", "branch-am"}

    def make(ref: str) -> TiaTurnUnderstanding:
        return TiaTurnUnderstanding(
            operations=[
                TurnOperation(
                    type="availability",
                    entities=TurnEntities(
                        service=EntityReference(ref="S1"),
                        branch=EntityReference(ref=ref),
                        date=DateConstraint(mode="exact", start_date=DATE),
                        time=TimeConstraint(mode="exact", start_time="01:00", spoken_period_hint="am"),
                    ),
                    execution_intent="informational",
                )
            ]
        )

    pm_turn = _normalize_time_semantics_v2(make(branch_refs["branch-pm"]), latest_customer_text="1 AM")
    am_turn = _normalize_time_semantics_v2(make(branch_refs["branch-am"]), latest_customer_text="1 AM")
    assert classify_operation_bare_clock(pm_turn.operations[0], context).status == "clarify_period"
    assert classify_operation_bare_clock(am_turn.operations[0], context).status == "resolved"


@pytest.mark.parametrize("op_type", ["availability", "book", "reschedule", "continue_active"])
def test_explicit_period_contradiction_is_operation_type_independent(op_type: str) -> None:
    context = build_semantic_context(_catalog(start="10:00", end="22:00"))
    turn = _normalize_time_semantics_v2(
        _turn("01:00", hint="am", op_type=op_type), latest_customer_text="1 AM"
    )
    resolution = classify_operation_bare_clock(turn.operations[0], context)
    assert resolution.status == "clarify_period"
    assert resolution.valid_operating_hour_candidates == ("13:00",)


def test_clarification_outcome_exposes_requested_and_plausible_period_evidence() -> None:
    _, _, resolution, resolved, step, context = _pipeline(
        "01:00", text="1 AM", hint="am", start="10:00", end="22:00"
    )
    assert resolution.status == "clarify_period"
    outcome = build_step_outcome(step, turn=resolved, semantic_context=context)
    assert outcome.status == "needs_input"
    assert outcome.response_goal == "clarification"
    assert outcome.facts["needed"] == "time"
    assert outcome.facts["clock_period_clarification"] is True
    assert outcome.facts["candidate_clocks"] == ["01:00", "13:00"]
    assert outcome.facts["valid_operating_hour_candidates"] == ["13:00"]
