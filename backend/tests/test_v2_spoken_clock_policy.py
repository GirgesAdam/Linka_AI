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
from app.services.agent_v2.planner import PlannerContext, plan_turn
from app.services.agent_v2.state import CustomerConstraints

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
DATE = "2026-10-15"


def _catalog(*, start: str, end: str, second_branch: tuple[str, str] | None = None) -> dict[str, object]:
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
        "services": [{"id": "service-1", "name": "Consultation", "duration_minutes": 30}],
        "branches": branches,
    }


def _turn(
    clock: str,
    *,
    hint: str = "none",
    date: str | None = DATE,
    branch_ref: str | None = None,
    selection: bool = False,
) -> TiaTurnUnderstanding:
    entities = TurnEntities(
        service=EntityReference(ref="S1"),
        branch=EntityReference(ref=branch_ref) if branch_ref else None,
        date=DateConstraint(mode="exact", start_date=date) if date is not None else None,
        time=None if selection else TimeConstraint(
            mode="exact",
            start_time=clock,
            spoken_period_hint=hint,
        ),
    )
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=entities,
                selection=(
                    Selection(kind="time", time=clock, spoken_period_hint=hint)
                    if selection
                    else None
                ),
            )
        ]
    )


def _pipeline(
    turn: TiaTurnUnderstanding,
    *,
    customer_text: str,
    start: str,
    end: str,
    second_branch: tuple[str, str] | None = None,
):
    context = build_semantic_context(
        _catalog(start=start, end=end, second_branch=second_branch)
    )
    safe = _normalize_time_semantics_v2(turn, latest_customer_text=customer_text)
    normalized = normalize_semantic_invariants(safe)
    resolved = resolve_turn_times_by_clinic_hours(normalized, context)
    return resolved, context


def _constraint(turn: TiaTurnUnderstanding) -> TimeConstraint:
    value = turn.operations[0].entities.time
    assert value is not None
    return value


def _clock(turn: TiaTurnUnderstanding) -> str:
    value = _constraint(turn).start_time
    assert value is not None
    return value


def _plan(turn: TiaTurnUnderstanding, context):
    return plan_turn(
        turn,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    ).steps[0]


@pytest.mark.parametrize(
    ("customer", "model_clock", "hint", "expected"),
    [
        ("الساعة 1", "01:00", "none", "13:00"),
        ("الساعة 1 الصبح", "01:00", "am", "13:00"),
        ("الساعة 3", "15:00", "none", "15:00"),
        ("الساعة 3 الصبح", "03:00", "am", "15:00"),
        ("الساعة 3 بالليل", "15:00", "pm", "15:00"),
        ("الساعة 4 وربع", "16:15", "none", "16:15"),
        ("الساعة 4 وربع الصبح", "04:15", "am", "16:15"),
        ("الساعة 9", "09:00", "none", "21:00"),
    ],
)
def test_normal_clinic_1_through_9_is_owned_by_clinic_hours(
    customer: str,
    model_clock: str,
    hint: str,
    expected: str,
) -> None:
    turn, context = _pipeline(
        _turn(model_clock, hint=hint),
        customer_text=customer,
        start="10:00",
        end="22:00",
    )
    assert _clock(turn) == expected
    assert _constraint(turn).start_time_ambiguity == "none"
    assert _plan(turn, context).clarification_field is None


@pytest.mark.parametrize(
    ("customer", "model_clock", "hint", "expected"),
    [
        ("الساعة 1", "13:00", "none", "01:00"),
        ("الساعة 1 بالليل", "13:00", "pm", "01:00"),
        ("الساعة 3", "15:00", "none", "03:00"),
        ("الساعة 3 بالليل", "15:00", "pm", "03:00"),
        ("الساعة 3 الصبح", "03:00", "am", "03:00"),
    ],
)
def test_am_only_clinic_1_through_9_ignores_period_hint(
    customer: str,
    model_clock: str,
    hint: str,
    expected: str,
) -> None:
    turn, context = _pipeline(
        _turn(model_clock, hint=hint),
        customer_text=customer,
        start="00:00",
        end="05:00",
    )
    assert _clock(turn) == expected
    assert _plan(turn, context).clarification_field is None


@pytest.mark.parametrize(
    ("hour", "start", "end", "hint", "expected", "clarify"),
    [
        ("10:00", "09:00", "21:00", "none", "10:00", False),
        ("10:00", "14:00", "23:30", "none", "22:00", False),
        ("10:00", "09:00", "23:30", "none", "10:00", True),
        ("10:00", "09:00", "23:30", "am", "10:00", False),
        ("10:00", "09:00", "23:30", "pm", "22:00", False),
        ("11:00", "09:00", "22:00", "none", "11:00", False),
        ("11:00", "14:00", "23:30", "none", "23:00", False),
        ("11:00", "09:00", "23:30", "none", "11:00", True),
        ("11:00", "09:00", "23:30", "am", "11:00", False),
        ("11:00", "09:00", "23:30", "pm", "23:00", False),
    ],
)
def test_10_11_policy(
    hour: str,
    start: str,
    end: str,
    hint: str,
    expected: str,
    clarify: bool,
) -> None:
    customer = f"الساعة {int(hour[:2])}" + (" بالليل" if hint == "pm" else " الصبح" if hint == "am" else "")
    turn, context = _pipeline(
        _turn(hour, hint=hint),
        customer_text=customer,
        start=start,
        end=end,
    )
    if clarify:
        assert _clock(turn) == hour
        assert _constraint(turn).start_time_ambiguity == "twelve_hour"
        resolution = classify_operation_bare_clock(turn.operations[0], context)
        assert resolution.status == "clarify_period"
        step = _plan(turn, context)
        assert step.disposition == "clarify"
        assert step.clarification_field == "time"
        assert step.write_intent is None
    else:
        assert _clock(turn) == expected
        assert _constraint(turn).start_time_ambiguity == "none"
        assert _plan(turn, context).clarification_field is None


def test_10_11_zero_valid_is_unavailable() -> None:
    turn, context = _pipeline(
        _turn("11:00"),
        customer_text="الساعة 11",
        start="12:00",
        end="20:00",
    )
    step = _plan(turn, context)
    assert step.disposition == "blocked"
    assert step.response_goal == "requested_time_unavailable"
    assert step.write_intent is None


@pytest.mark.parametrize(
    ("customer", "model_clock", "hint", "start", "end", "expected"),
    [
        ("الساعة 11 بالليل", "23:00", "pm", "09:00", "21:00", "11:00"),
        ("الساعة 11 الصبح", "11:00", "am", "14:00", "23:30", "23:00"),
    ],
)
def test_only_operational_candidate_wins_over_hint(
    customer: str,
    model_clock: str,
    hint: str,
    start: str,
    end: str,
    expected: str,
) -> None:
    turn, _context = _pipeline(
        _turn(model_clock, hint=hint),
        customer_text=customer,
        start=start,
        end=end,
    )
    assert _clock(turn) == expected


@pytest.mark.parametrize(
    ("customer", "model_clock", "hint", "expected"),
    [
        ("الساعة 12", "12:00", "none", "12:00"),
        ("الساعة 12 الظهر", "12:00", "noon", "12:00"),
        ("الساعة 12 بالليل", "00:00", "midnight", "00:00"),
    ],
)
def test_12_special_case_is_preserved(
    customer: str,
    model_clock: str,
    hint: str,
    expected: str,
) -> None:
    turn, _context = _pipeline(
        _turn(model_clock, hint=hint),
        customer_text=customer,
        start="00:00",
        end="23:59",
    )
    assert _clock(turn) == expected
    assert _constraint(turn).start_time_ambiguity == "none"


@pytest.mark.parametrize(
    ("customer", "model_clock", "hint", "expected"),
    [
        ("الساعة 03:00", "15:00", "pm", "03:00"),
        ("الساعة 16:15", "04:15", "am", "16:15"),
        ("الساعة 03:00 بالليل", "15:00", "pm", "03:00"),
    ],
)
def test_structural_hhmm_is_exact_and_ignores_hint(
    customer: str,
    model_clock: str,
    hint: str,
    expected: str,
) -> None:
    turn, _context = _pipeline(
        _turn(model_clock, hint=hint),
        customer_text=customer,
        start="10:00",
        end="22:00",
    )
    assert _clock(turn) == expected
    assert _constraint(turn).start_time_ambiguity == "none"


def test_selection_time_uses_same_10_11_policy() -> None:
    turn, _context = _pipeline(
        _turn("23:00", hint="pm", selection=True),
        customer_text="11 بالليل",
        start="09:00",
        end="23:30",
    )
    selection = turn.operations[0].selection
    assert selection is not None
    assert selection.time == "23:00"
    assert selection.time_ambiguity == "none"


def test_branch_specific_hours_own_spoken_clock_resolution() -> None:
    turn, _context = _pipeline(
        _turn("22:00", branch_ref="B2"),
        customer_text="الساعة 10",
        start="09:00",
        end="21:00",
        second_branch=("14:00", "23:30"),
    )
    assert _clock(turn) == "22:00"


def test_missing_date_waits_for_context_before_10_11_period_question() -> None:
    turn, context = _pipeline(
        _turn("22:00", date=None),
        customer_text="الساعة 10",
        start="09:00",
        end="23:30",
    )
    assert _constraint(turn).start_time_ambiguity == "twelve_hour"
    resolution = classify_operation_bare_clock(turn.operations[0], context)
    assert resolution.status == "needs_context"
    step = _plan(turn, context)
    assert step.write_intent is None
    assert step.facts["booking_next_field"] == "date"


def test_provider_schema_has_one_period_hint_and_no_legacy_period_authority() -> None:
    schema = TiaTurnUnderstanding.model_json_schema()
    defs = schema["$defs"]
    time_fields = set(defs["TimeConstraint"]["properties"])
    selection_fields = set(defs["Selection"]["properties"])
    assert "spoken_period_hint" in time_fields
    assert "spoken_period_hint" in selection_fields
    assert "period_evidence_text" not in time_fields | selection_fields
    assert "period_status" not in time_fields | selection_fields
    assert "start_time_ambiguity" not in time_fields
    assert "time_ambiguity" not in selection_fields


def test_legacy_period_fields_are_read_then_dropped() -> None:
    constraint = TimeConstraint.model_validate(
        {
            "mode": "exact",
            "start_time": "23:00",
            "period_status": "explicit",
            "period_evidence_text": "legacy",
        }
    )
    assert constraint.start_time == "23:00"
    assert constraint.spoken_period_hint == "none"
    assert "period_status" not in constraint.model_dump(mode="json")
    assert "period_evidence_text" not in constraint.model_dump(mode="json")

    state = CustomerConstraints.model_validate(
        {
            "time": constraint.model_dump(mode="json"),
            "time_clock_source": "explicit_period",
        }
    )
    assert state.time_clock_source == "inherited"


def test_model_pm_guess_is_neutralized_to_spoken_base_before_clinic_resolution() -> None:
    safe = _normalize_time_semantics_v2(
        _turn("16:15", hint="none"),
        latest_customer_text="الساعة 4 وربع",
    )
    assert _clock(safe) == "04:15"
    assert _constraint(safe).start_time_ambiguity == "twelve_hour"


def test_model_am_guess_with_hint_is_still_neutralized_for_1_through_9() -> None:
    safe = _normalize_time_semantics_v2(
        _turn("03:00", hint="am"),
        latest_customer_text="الساعة 3 الصبح",
    )
    assert _clock(safe) == "03:00"
    assert _constraint(safe).start_time_ambiguity == "twelve_hour"
    assert _constraint(safe).spoken_period_hint == "am"
