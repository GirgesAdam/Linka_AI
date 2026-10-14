from __future__ import annotations

from datetime import UTC, datetime

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
from app.services.agent_v2.planner import PlannerContext, plan_turn

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
DATE = "2026-10-15"


def _turn(clock: str, *, evidence: str | None = None) -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(ref="S1"),
                    date=DateConstraint(mode="exact", start_date=DATE),
                    time=TimeConstraint(
                        mode="exact",
                        start_time=clock,
                        period_evidence_text=evidence,
                    ),
                ),
            )
        ]
    )


def _catalog(start: str, end: str) -> dict[str, object]:
    return {
        "services": [{"id": "service-1", "name": "Service", "duration_minutes": 30}],
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
    }


def _normalize_and_resolve(
    clock: str,
    *,
    evidence: str | None,
    text: str,
    start: str,
    end: str,
):
    turn = _normalize_time_semantics_v2(
        _turn(clock, evidence=evidence),
        latest_customer_text=text,
    )
    context = build_semantic_context(_catalog(start, end))
    return resolve_turn_times_by_clinic_hours(turn, context), context


def test_bare_clock_has_no_period_authority_even_if_model_guesses_pm() -> None:
    resolved, _ = _normalize_and_resolve(
        "15:00",
        evidence=None,
        text="الساعة 3",
        start="10:00",
        end="22:00",
    )
    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "15:00"
    assert constraint.start_time_ambiguity == "none"
    assert constraint.period_status == "none"


def test_bare_clock_uses_am_when_pm_is_invalid_even_if_model_guesses_pm() -> None:
    resolved, _ = _normalize_and_resolve(
        "15:00",
        evidence=None,
        text="الساعة 3",
        start="00:00",
        end="05:00",
    )
    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "03:00"
    assert constraint.start_time_ambiguity == "none"


def test_spoken_quarter_clock_without_period_evidence_uses_clinic_hours() -> None:
    resolved, _ = _normalize_and_resolve(
        "16:15",
        evidence=None,
        text="الساعة 4 وربع",
        start="10:00",
        end="22:00",
    )
    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "16:15"
    assert constraint.start_time_ambiguity == "none"
    assert constraint.period_status == "none"


def test_spoken_clock_amount_mislabeled_as_period_evidence_still_fails_closed() -> None:
    turn = _normalize_time_semantics_v2(
        _turn("16:15", evidence="4 وربع"),
        latest_customer_text="الساعة 4 وربع",
    )
    constraint = turn.operations[0].entities.time
    assert constraint is not None
    assert constraint.period_status == "conflict"


def test_bare_ten_reopens_guessed_22_and_clarifies_when_both_valid() -> None:
    turn = _normalize_time_semantics_v2(
        _turn("22:00"),
        latest_customer_text="الساعة 10",
    )
    constraint = turn.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "10:00"
    assert constraint.start_time_ambiguity == "twelve_hour"
    context = build_semantic_context(_catalog("09:00", "23:30"))
    resolution = classify_operation_bare_clock(turn.operations[0], context)
    assert resolution.status == "clarify_period"
    plan = plan_turn(
        turn,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    assert plan.steps[0].clarification_field == "time"
    assert plan.steps[0].write_intent is None


def test_explicit_period_evidence_makes_semantic_clock_exact() -> None:
    pm, _ = _normalize_and_resolve(
        "15:00",
        evidence="العصر",
        text="الساعة 3 العصر",
        start="00:00",
        end="23:59",
    )
    am, _ = _normalize_and_resolve(
        "03:00",
        evidence="الصبح",
        text="الساعة 3 الصبح",
        start="00:00",
        end="23:59",
    )
    pm_time = pm.operations[0].entities.time
    am_time = am.operations[0].entities.time
    assert pm_time is not None and pm_time.start_time == "15:00"
    assert am_time is not None and am_time.start_time == "03:00"
    assert pm_time.period_status == "explicit"
    assert am_time.period_status == "explicit"


def test_noon_and_midnight_meaning_stays_model_semantic_when_evidence_is_verified() -> None:
    noon, _ = _normalize_and_resolve(
        "12:00",
        evidence="الظهر",
        text="12 الظهر",
        start="00:00",
        end="23:59",
    )
    midnight, _ = _normalize_and_resolve(
        "00:00",
        evidence="بالليل",
        text="12 بالليل",
        start="00:00",
        end="23:59",
    )
    noon_time = noon.operations[0].entities.time
    midnight_time = midnight.operations[0].entities.time
    assert noon_time is not None and noon_time.start_time == "12:00"
    assert midnight_time is not None and midnight_time.start_time == "00:00"
    assert noon_time.period_status == "explicit"
    assert midnight_time.period_status == "explicit"


def test_fabricated_period_evidence_fails_closed() -> None:
    turn = _normalize_time_semantics_v2(
        _turn("15:00", evidence="العصر"),
        latest_customer_text="الساعة 3",
    )
    constraint = turn.operations[0].entities.time
    assert constraint is not None
    assert constraint.period_status == "conflict"
    context = build_semantic_context(_catalog("00:00", "23:59"))
    plan = plan_turn(
        turn,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    assert plan.steps[0].clarification_field == "time"
    assert plan.steps[0].write_intent is None


def test_clock_digits_cannot_be_used_as_period_evidence() -> None:
    turn = _normalize_time_semantics_v2(
        _turn("15:00", evidence="3"),
        latest_customer_text="الساعة 3",
    )
    constraint = turn.operations[0].entities.time
    assert constraint is not None
    assert constraint.period_status == "conflict"


def test_literal_hhmm_remains_structural_authority_without_period_evidence() -> None:
    turn = _normalize_time_semantics_v2(
        _turn("15:30"),
        latest_customer_text="03:00",
    )
    constraint = turn.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "03:00"
    assert constraint.start_time_ambiguity == "none"
    assert constraint.period_status == "none"


def test_literal_hhmm_with_compatible_period_evidence_can_use_semantic_12h_variant() -> None:
    turn = _normalize_time_semantics_v2(
        _turn("16:15", evidence="العصر"),
        latest_customer_text="4:15 العصر",
    )
    constraint = turn.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "16:15"
    assert constraint.period_status == "explicit"


def test_literal_hhmm_conflicting_period_semantics_fails_closed() -> None:
    turn = _normalize_time_semantics_v2(
        _turn("04:15", evidence="الصبح"),
        latest_customer_text="16:15 الصبح",
    )
    constraint = turn.operations[0].entities.time
    assert constraint is not None
    assert constraint.period_status == "conflict"
    context = build_semantic_context(_catalog("00:00", "23:59"))
    plan = plan_turn(
        turn,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    assert plan.steps[0].clarification_field == "time"
    assert plan.steps[0].write_intent is None
