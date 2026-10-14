from __future__ import annotations

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.time_resolution import (
    resolve_bare_clock_constraint,
    resolve_turn_times_by_clinic_hours,
)
from app.agents.v2.turn_contract import (
    DateConstraint,
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import _normalize_time_semantics_v2

DATE = DateConstraint(mode="exact", start_date="2026-10-15")


def _context(start: str, end: str):
    return build_semantic_context(
        {
            "branches": [
                {
                    "id": "branch-1",
                    "name": "Clinic",
                    "working_hours": [
                        {"weekday": weekday, "start": start, "end": end}
                        for weekday in range(7)
                    ],
                }
            ],
            "services": [],
        }
    )


def _resolve(clock: str, *, start: str, end: str, hint: str = "none"):
    constraint = TimeConstraint(
        mode="exact",
        start_time=clock,
        start_time_ambiguity="twelve_hour",
        spoken_period_hint=hint,
    )
    return resolve_bare_clock_constraint(
        constraint,
        date_constraint=DATE,
        context=_context(start, end),
        branch_id="branch-1",
    )


def _clock(result) -> str | None:
    return result.constraint.start_time if result.constraint is not None else None


def test_normal_clinic_1_to_9_ignores_period_hint_and_prefers_pm() -> None:
    for clock, hint, expected in [
        ("01:00", "none", "13:00"),
        ("01:00", "am", "13:00"),
        ("03:00", "none", "15:00"),
        ("03:00", "am", "15:00"),
        ("03:00", "pm", "15:00"),
        ("04:15", "none", "16:15"),
        ("04:15", "am", "16:15"),
        ("09:00", "none", "21:00"),
    ]:
        result = _resolve(clock, start="10:00", end="22:00", hint=hint)
        assert result.status == "resolved"
        assert _clock(result) == expected


def test_am_only_clinic_1_to_9_ignores_period_hint_and_uses_am() -> None:
    for clock, hint, expected in [
        ("01:00", "none", "01:00"),
        ("01:00", "pm", "01:00"),
        ("03:00", "none", "03:00"),
        ("03:00", "pm", "03:00"),
        ("03:00", "am", "03:00"),
    ]:
        result = _resolve(clock, start="00:00", end="05:00", hint=hint)
        assert result.status == "resolved"
        assert _clock(result) == expected


def test_ten_policy_uses_hint_only_when_both_candidates_are_operational() -> None:
    am_only = _resolve("10:00", start="09:00", end="21:00", hint="pm")
    assert am_only.status == "resolved" and _clock(am_only) == "10:00"
    pm_only = _resolve("10:00", start="20:00", end="23:00", hint="am")
    assert pm_only.status == "resolved" and _clock(pm_only) == "22:00"
    both = _resolve("10:00", start="09:00", end="23:30")
    assert both.status == "clarify_period"
    assert both.valid_operating_hour_candidates == ("10:00", "22:00")
    assert _clock(_resolve("10:00", start="09:00", end="23:30", hint="am")) == "10:00"
    assert _clock(_resolve("10:00", start="09:00", end="23:30", hint="pm")) == "22:00"


def test_eleven_policy_and_zero_valid() -> None:
    assert _clock(_resolve("11:00", start="10:30", end="20:00")) == "11:00"
    assert _clock(_resolve("11:00", start="22:00", end="23:30")) == "23:00"
    both = _resolve("11:00", start="10:00", end="23:30")
    assert both.status == "clarify_period"
    assert _clock(_resolve("11:00", start="10:00", end="23:30", hint="am")) == "11:00"
    assert _clock(_resolve("11:00", start="10:00", end="23:30", hint="pm")) == "23:00"
    assert _resolve("11:00", start="12:00", end="22:00").status == "outside_operating_hours"


def test_single_operational_candidate_wins_over_contradictory_hint() -> None:
    assert _clock(_resolve("11:00", start="10:30", end="20:00", hint="pm")) == "11:00"
    assert _clock(_resolve("11:00", start="22:00", end="23:30", hint="am")) == "23:00"


def test_bare_twelve_defaults_to_noon_and_midnight_hint_preserves_existing_special_case() -> None:
    noon = _resolve("12:00", start="00:00", end="23:59")
    assert noon.status == "resolved" and _clock(noon) == "12:00"
    midnight = _resolve("12:00", start="00:00", end="23:59", hint="midnight")
    assert midnight.status == "resolved" and _clock(midnight) == "00:00"


def test_spoken_normalization_neutralizes_provider_am_pm_guess_but_keeps_minutes() -> None:
    for raw, expected in [("03:00", "03:00"), ("15:00", "03:00"), ("11:00", "11:00"), ("23:00", "11:00"), ("16:15", "04:15")]:
        turn = TiaTurnUnderstanding(
            operations=[
                TurnOperation(
                    type="availability",
                    entities=TurnEntities(
                        time=TimeConstraint(mode="exact", start_time=raw, spoken_period_hint="am")
                    ),
                    execution_intent="informational",
                )
            ]
        )
        normalized = _normalize_time_semantics_v2(turn, latest_customer_text="spoken clock")
        value = normalized.operations[0].entities.time
        assert value is not None
        assert value.start_time == expected
        assert value.start_time_ambiguity == "twelve_hour"
        assert value.spoken_period_hint == "am"


def test_structural_hhmm_stays_exact_and_ignores_hint() -> None:
    for literal in ("03:00", "16:15"):
        turn = TiaTurnUnderstanding(
            operations=[
                TurnOperation(
                    type="availability",
                    entities=TurnEntities(
                        time=TimeConstraint(mode="exact", start_time="15:00", spoken_period_hint="pm")
                    ),
                    execution_intent="informational",
                )
            ]
        )
        normalized = _normalize_time_semantics_v2(turn, latest_customer_text=f"???? {literal}")
        value = normalized.operations[0].entities.time
        assert value is not None
        assert value.start_time == literal
        assert value.start_time_ambiguity == "none"
        assert value.spoken_period_hint == "none"


def test_legacy_period_fields_deserialize_without_becoming_new_authority() -> None:
    value = TimeConstraint.model_validate(
        {
            "mode": "exact",
            "start_time": "23:00",
            "period_evidence_text": "legacy",
            "period_status": "explicit",
        }
    )
    assert value.start_time == "23:00"
    assert value.spoken_period_hint == "none"
    assert not hasattr(value, "period_status")


def test_selection_time_uses_same_backend_spoken_clock_policy() -> None:
    context = _context("09:00", "23:30")
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="continue_active",
                entities=TurnEntities(date=DATE),
                selection=Selection(
                    kind="time",
                    time="10:00",
                    time_ambiguity="twelve_hour",
                    spoken_period_hint="pm",
                ),
            )
        ]
    )
    resolved = resolve_turn_times_by_clinic_hours(turn, context)
    selection = resolved.operations[0].selection
    assert selection is not None
    assert selection.time == "22:00"
    assert selection.time_ambiguity == "none"
    assert selection.spoken_period_hint == "none"


def test_selection_time_single_candidate_ignores_contradictory_hint() -> None:
    context = _context("09:00", "21:00")
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="continue_active",
                entities=TurnEntities(date=DATE),
                selection=Selection(
                    kind="time",
                    time="10:00",
                    time_ambiguity="twelve_hour",
                    spoken_period_hint="pm",
                ),
            )
        ]
    )
    resolved = resolve_turn_times_by_clinic_hours(turn, context)
    selection = resolved.operations[0].selection
    assert selection is not None
    assert selection.time == "10:00"
    assert selection.time_ambiguity == "none"
