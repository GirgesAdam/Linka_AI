from __future__ import annotations

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.time_resolution import (
    classify_operation_bare_clock,
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


def _catalog(*, start: str, end: str, weekdays: tuple[int, ...] = tuple(range(7))) -> dict:
    return {
        "services": [{"id": "svc-1", "name": "Service"}],
        "branches": [
            {
                "id": "branch-1",
                "name": "Clinic",
                "working_hours": [
                    {"weekday": weekday, "start": start, "end": end}
                    for weekday in weekdays
                ],
            }
        ],
    }


def _turn(
    time_constraint: TimeConstraint | None,
    *,
    selection: Selection | None = None,
) -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    date=DateConstraint(mode="exact", start_date="2026-09-12"),
                    time=time_constraint,
                ),
                selection=selection,
            )
        ]
    )


def test_bare_eight_resolves_to_evening_when_only_pm_is_inside_clinic_hours() -> None:
    context = build_semantic_context(_catalog(start="10:00", end="22:00"))
    turn = _turn(
        TimeConstraint(
            mode="exact",
            start_time="08:00",
            start_time_ambiguity="twelve_hour",
        )
    )

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "20:00"
    assert constraint.start_time_ambiguity == "none"


def test_bare_eight_prefers_pm_when_both_am_and_pm_are_inside_hours() -> None:
    context = build_semantic_context(_catalog(start="08:00", end="22:00"))
    turn = _turn(
        TimeConstraint(
            mode="exact",
            start_time="08:00",
            start_time_ambiguity="twelve_hour",
        )
    )

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "20:00"
    assert constraint.start_time_ambiguity == "none"


def test_bare_eight_neither_candidate_stays_unresolved_for_planner_unavailable_gate() -> None:
    context = build_semantic_context(_catalog(start="09:00", end="17:00"))
    turn = _turn(
        TimeConstraint(
            mode="exact",
            start_time="08:00",
            start_time_ambiguity="twelve_hour",
        )
    )

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "08:00"
    assert constraint.start_time_ambiguity == "twelve_hour"
    classified = classify_operation_bare_clock(resolved.operations[0], context)
    assert classified.status == "outside_operating_hours"
    assert classified.valid_operating_hour_candidates == ()


def test_explicit_morning_is_never_flipped_to_evening() -> None:
    context = build_semantic_context(_catalog(start="10:00", end="22:00"))
    turn = _turn(TimeConstraint(mode="exact", start_time="08:00"))

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "08:00"


def test_explicit_evening_stays_evening() -> None:
    context = build_semantic_context(_catalog(start="10:00", end="22:00"))
    turn = _turn(TimeConstraint(mode="exact", start_time="20:00"))

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "20:00"


def test_ambiguous_after_six_resolves_to_18_and_keeps_inclusive_mode() -> None:
    context = build_semantic_context(_catalog(start="10:00", end="22:00"))
    turn = _turn(
        TimeConstraint(
            mode="after",
            start_time="06:00",
            start_time_ambiguity="twelve_hour",
        )
    )

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.mode == "after"
    assert constraint.start_time == "18:00"


def test_date_specific_weekday_hours_drive_resolution() -> None:
    catalog = {
        "services": [{"id": "svc-1", "name": "Service"}],
        "branches": [
            {
                "id": "branch-1",
                "name": "Clinic",
                "working_hours": [
                    {"weekday": 5, "start": "10:00", "end": "22:00"},
                    {"weekday": 6, "start": "07:00", "end": "17:00"},
                ],
            }
        ],
    }
    context = build_semantic_context(catalog)
    turn = _turn(
        TimeConstraint(
            mode="exact",
            start_time="08:00",
            start_time_ambiguity="twelve_hour",
        )
    )

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "20:00"


def test_time_selection_uses_same_business_hour_resolution() -> None:
    context = build_semantic_context(_catalog(start="10:00", end="22:00"))
    turn = _turn(
        None,
        selection=Selection(
            kind="time",
            time="08:00",
            time_ambiguity="twelve_hour",
        ),
    )

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    selection = resolved.operations[0].selection
    assert selection is not None
    assert selection.time == "20:00"
    assert selection.time_ambiguity == "none"


def test_different_branch_schedules_are_not_merged_or_guessed() -> None:
    catalog = {
        "services": [{"id": "svc-1", "name": "Service"}],
        "branches": [
            {
                "id": "branch-1",
                "name": "Clinic A",
                "working_hours": [{"weekday": 5, "start": "10:00", "end": "22:00"}],
            },
            {
                "id": "branch-2",
                "name": "Clinic B",
                "working_hours": [{"weekday": 5, "start": "08:00", "end": "17:00"}],
            },
        ],
    }

    context = build_semantic_context(catalog)

    assert "clinic_operating_hours" not in context.model_input
    assert context.server_metadata["clinic_operating_hours"] == []



def _turn_without_date(time_constraint: TimeConstraint) -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(time=time_constraint),
            )
        ]
    )


def test_explicit_pm_without_date_preserves_period_pending_context() -> None:
    context = build_semantic_context(_catalog(start="09:00", end="21:00"))
    turn = _turn_without_date(
        TimeConstraint(
            mode="exact",
            start_time="10:30",
            start_time_ambiguity="twelve_hour",
            spoken_period_hint="pm",
        )
    )

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "10:30"
    assert constraint.start_time_ambiguity == "twelve_hour"
    assert constraint.spoken_period_hint == "pm"


def test_explicit_am_without_date_preserves_period_pending_context() -> None:
    context = build_semantic_context(_catalog(start="20:00", end="23:59"))
    turn = _turn_without_date(
        TimeConstraint(
            mode="exact",
            start_time="10:30",
            start_time_ambiguity="twelve_hour",
            spoken_period_hint="am",
        )
    )

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "10:30"
    assert constraint.start_time_ambiguity == "twelve_hour"
    assert constraint.spoken_period_hint == "am"


def test_explicit_pm_with_exact_date_resolves_to_pm_clock() -> None:
    context = build_semantic_context(_catalog(start="00:00", end="23:59"))
    turn = _turn(
        TimeConstraint(
            mode="exact",
            start_time="10:30",
            start_time_ambiguity="twelve_hour",
            spoken_period_hint="pm",
        )
    )

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "22:30"
    assert constraint.start_time_ambiguity == "none"


def test_explicit_pm_outside_hours_with_am_feasible_stays_unresolved_for_clarification() -> None:
    context = build_semantic_context(_catalog(start="09:00", end="21:00"))
    turn = _turn(
        TimeConstraint(
            mode="exact",
            start_time="10:30",
            start_time_ambiguity="twelve_hour",
            spoken_period_hint="pm",
        )
    )

    resolved = resolve_turn_times_by_clinic_hours(turn, context)

    constraint = resolved.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "10:30"
    assert constraint.start_time_ambiguity == "twelve_hour"
    assert constraint.spoken_period_hint == "pm"
    classified = classify_operation_bare_clock(resolved.operations[0], context)
    assert classified.status == "clarify_period"
    assert classified.valid_operating_hour_candidates == ("10:30",)
