from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.time_resolution import resolve_turn_times_by_clinic_hours
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
from app.services.agent_v2.planner import PlannerContext, plan_turn

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
DATE = "2026-10-15"


def _catalog(*, start: str, end: str) -> dict[str, object]:
    return {
        "services": [{"id": "service-1", "name": "Consultation", "duration_minutes": 30}],
        "branches": [
            {
                "id": "branch-1",
                "name": "Clinic A",
                "working_hours": [
                    {"weekday": weekday, "start": start, "end": end}
                    for weekday in range(7)
                ],
            }
        ],
    }


def _turn(clock: str, *, hint: str = "none") -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(ref="S1"),
                    branch=EntityReference(ref="B1"),
                    date=DateConstraint(mode="exact", start_date=DATE),
                    time=TimeConstraint(
                        mode="exact",
                        start_time=clock,
                        spoken_period_hint=hint,
                    ),
                ),
            )
        ]
    )


def _pipeline(clock: str, *, text: str, start: str, end: str, hint: str = "none"):
    context = build_semantic_context(_catalog(start=start, end=end))
    normalized = _normalize_time_semantics_v2(_turn(clock, hint=hint), latest_customer_text=text)
    normalized = normalize_semantic_invariants(normalized)
    resolved = resolve_turn_times_by_clinic_hours(normalized, context)
    return resolved, context


def _constraint(turn: TiaTurnUnderstanding) -> TimeConstraint:
    constraint = turn.operations[0].entities.time
    assert constraint is not None
    return constraint


def _clock(turn: TiaTurnUnderstanding) -> str:
    value = _constraint(turn).start_time
    assert value is not None
    return value


@pytest.mark.parametrize(
    ("model_clock", "text", "hint", "expected"),
    [
        ("01:00", "?????? 1", "none", "13:00"),
        ("01:00", "?????? 1 ?????", "am", "13:00"),
        ("03:00", "?????? 3", "none", "15:00"),
        ("03:00", "?????? 3 ?????", "am", "15:00"),
        ("15:00", "?????? 3 ??????", "pm", "15:00"),
        ("16:15", "?????? 4 ????", "none", "16:15"),
        ("04:15", "?????? 4 ???? ?????", "am", "16:15"),
        ("09:00", "?????? 9", "none", "21:00"),
    ],
)
def test_spoken_1_to_9_normal_clinic_prefers_pm(
    model_clock: str, text: str, hint: str, expected: str
) -> None:
    turn, _ = _pipeline(model_clock, text=text, hint=hint, start="10:00", end="22:00")
    assert _clock(turn) == expected
    assert _constraint(turn).start_time_ambiguity == "none"
    assert _constraint(turn).spoken_period_hint == "none"


@pytest.mark.parametrize(
    ("model_clock", "text", "hint", "expected"),
    [
        ("01:00", "?????? 1", "none", "01:00"),
        ("13:00", "?????? 1 ??????", "pm", "01:00"),
        ("03:00", "?????? 3", "none", "03:00"),
        ("15:00", "?????? 3 ??????", "pm", "03:00"),
        ("03:00", "?????? 3 ?????", "am", "03:00"),
    ],
)
def test_spoken_1_to_9_am_only_clinic_ignores_period_hint(
    model_clock: str, text: str, hint: str, expected: str
) -> None:
    turn, _ = _pipeline(model_clock, text=text, hint=hint, start="00:00", end="05:00")
    assert _clock(turn) == expected


@pytest.mark.parametrize(
    ("hour", "start", "end", "hint", "expected", "clarifies"),
    [
        ("10:00", "09:00", "21:00", "none", "10:00", False),
        ("10:00", "20:00", "23:00", "none", "22:00", False),
        ("10:00", "00:00", "23:59", "none", "10:00", True),
        ("10:00", "00:00", "23:59", "am", "10:00", False),
        ("22:00", "00:00", "23:59", "pm", "22:00", False),
        ("11:00", "09:00", "21:00", "none", "11:00", False),
        ("11:00", "20:00", "23:59", "none", "23:00", False),
        ("11:00", "00:00", "23:59", "none", "11:00", True),
        ("11:00", "00:00", "23:59", "am", "11:00", False),
        ("23:00", "00:00", "23:59", "pm", "23:00", False),
    ],
)
def test_10_11_candidate_policy(
    hour: str, start: str, end: str, hint: str, expected: str, clarifies: bool
) -> None:
    text = f"?????? {hour.split(':')[0]}"
    turn, context = _pipeline(hour, text=text, hint=hint, start=start, end=end)
    if clarifies:
        assert _constraint(turn).start_time_ambiguity == "twelve_hour"
        step = plan_turn(
            turn,
            PlannerContext(semantic_context=context, active_task=None, now=NOW),
        ).steps[0]
        assert step.disposition == "clarify"
        assert step.clarification_field == "time"
        assert step.write_intent is None
    else:
        assert _clock(turn) == expected
        assert _constraint(turn).start_time_ambiguity == "none"


def test_11_pm_hint_remains_authoritative_when_pm_is_outside_hours() -> None:
    turn, _ = _pipeline("23:00", text="11 ??????", hint="pm", start="09:00", end="21:00")
    assert _clock(turn) == "23:00"


def test_11_am_hint_remains_authoritative_when_am_is_outside_hours() -> None:
    turn, _ = _pipeline("11:00", text="11 ?????", hint="am", start="20:00", end="23:59")
    assert _clock(turn) == "11:00"


@pytest.mark.parametrize("clock", ["10:00", "11:00"])
def test_10_11_zero_operational_candidates_blocks(clock: str) -> None:
    turn, context = _pipeline(clock, text=f"?????? {clock[:2]}", start="13:00", end="18:00")
    step = plan_turn(
        turn,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    ).steps[0]
    assert step.disposition == "blocked"
    assert step.write_intent is None


def test_structural_0300_remains_exact() -> None:
    turn, _ = _pipeline("15:00", text="03:00", start="10:00", end="22:00")
    assert _clock(turn) == "03:00"
    assert _constraint(turn).start_time_ambiguity == "none"


def test_structural_1615_remains_exact() -> None:
    turn, _ = _pipeline("04:15", text="16:15", start="00:00", end="05:00")
    assert _clock(turn) == "16:15"
    assert _constraint(turn).start_time_ambiguity == "none"


def test_structural_2230_remains_exact() -> None:
    turn, _ = _pipeline("10:30", text="22:30", start="00:00", end="23:59", hint="am")
    assert _clock(turn) == "22:30"
    assert _constraint(turn).start_time_ambiguity == "none"
    assert _constraint(turn).spoken_period_hint == "none"


def test_bare_12_remains_noon() -> None:
    turn, _ = _pipeline("12:00", text="?????? 12", start="10:00", end="22:00")
    assert _clock(turn) == "12:00"


def test_explicit_midnight_12_uses_existing_special_case() -> None:
    turn, _ = _pipeline("00:00", text="12 ??????", hint="midnight", start="00:00", end="05:00")
    assert _clock(turn) == "00:00"


def test_time_selection_uses_same_10_11_policy() -> None:
    context = build_semantic_context(_catalog(start="00:00", end="23:59"))
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="continue_active",
                selection=Selection(
                    kind="time",
                    time="23:00",
                    time_ambiguity="twelve_hour",
                    spoken_period_hint="pm",
                ),
                entities=TurnEntities(date=DateConstraint(mode="exact", start_date=DATE)),
            )
        ]
    )
    resolved = resolve_turn_times_by_clinic_hours(turn, context)
    selection = resolved.operations[0].selection
    assert selection is not None
    assert selection.time == "23:00"
    assert selection.time_ambiguity == "none"
    assert selection.spoken_period_hint == "none"


def test_period_only_selection_can_carry_typed_hint_for_pending_10_11() -> None:
    selection = Selection(kind="time", time=None, spoken_period_hint="pm")
    assert selection.spoken_period_hint == "pm"


def test_spoken_period_hint_is_provider_visible_but_general_serialization_stays_clean() -> None:
    schema = TimeConstraint.model_json_schema()
    assert "spoken_period_hint" in schema["properties"]
    constraint = TimeConstraint(
        mode="exact",
        start_time="10:30",
        start_time_ambiguity="twelve_hour",
        spoken_period_hint="pm",
    )
    assert "spoken_period_hint" not in constraint.model_dump(mode="json")

    selection_schema = Selection.model_json_schema()
    assert "spoken_period_hint" in selection_schema["properties"]
    selection = Selection(kind="time", time=None, spoken_period_hint="pm")
    assert selection.spoken_period_hint == "pm"
    assert "spoken_period_hint" not in selection.model_dump(mode="json")


def test_noon_is_not_a_provider_hint_value() -> None:
    with pytest.raises(ValueError):
        TimeConstraint(mode="exact", start_time="12:00", spoken_period_hint="noon")


@pytest.mark.parametrize("hint", ["am", "pm"])
def test_1_to_9_provider_hint_is_neutralized_before_resolution(hint: str) -> None:
    normalized = _normalize_time_semantics_v2(
        _turn("03:00", hint=hint), latest_customer_text="?????? 3 ?????"
    )
    assert _constraint(normalized).start_time == "03:00"
    assert _constraint(normalized).start_time_ambiguity == "twelve_hour"
    assert _constraint(normalized).spoken_period_hint == "none"


def test_structural_literal_neutralizes_provider_hint() -> None:
    normalized = _normalize_time_semantics_v2(
        _turn("22:30", hint="am"), latest_customer_text="22:30"
    )
    assert _constraint(normalized).start_time == "22:30"
    assert _constraint(normalized).start_time_ambiguity == "none"
    assert _constraint(normalized).spoken_period_hint == "none"


@pytest.mark.parametrize(
    ("start", "end", "hint", "expected", "clarifies"),
    [
        ("09:00", "21:00", "none", "10:30", False),
        ("20:00", "23:30", "none", "22:30", False),
        ("00:00", "23:59", "none", "10:30", True),
        ("00:00", "23:59", "am", "10:30", False),
        ("00:00", "23:59", "pm", "22:30", False),
        ("09:00", "21:00", "pm", "22:30", False),
        ("20:00", "23:30", "am", "10:30", False),
    ],
    ids=[
        "T1030-1-only-am",
        "T1030-2-only-pm",
        "T1030-3-both-no-hint",
        "T1030-4-both-am",
        "T1030-5-both-pm",
        "T1030-6-explicit-pm-outside-hours",
        "T1030-7-explicit-am-outside-hours",
    ],
)
def test_1030_required_matrix(
    start: str, end: str, hint: str, expected: str, clarifies: bool
) -> None:
    turn, context = _pipeline("10:30", text="?????? 10 ???", hint=hint, start=start, end=end)
    if clarifies:
        assert _constraint(turn).start_time == "10:30"
        assert _constraint(turn).start_time_ambiguity == "twelve_hour"
        step = plan_turn(
            turn, PlannerContext(semantic_context=context, active_task=None, now=NOW)
        ).steps[0]
        assert step.disposition == "clarify"
        assert step.clarification_field == "time"
        assert step.write_intent is None
        assert all(read.kind != "availability" for read in step.reads)
    else:
        assert _clock(turn) == expected
        assert _constraint(turn).start_time_ambiguity == "none"
        assert _constraint(turn).spoken_period_hint == "none"


def test_1130_uses_same_generic_candidate_policy() -> None:
    turn, _ = _pipeline(
        "11:30", text="?????? 11 ??? ??????", hint="pm", start="00:00", end="23:59"
    )
    assert _clock(turn) == "23:30"
    assert _constraint(turn).spoken_period_hint == "none"
