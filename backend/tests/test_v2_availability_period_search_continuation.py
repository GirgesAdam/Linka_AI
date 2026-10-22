from __future__ import annotations

from datetime import datetime

from app.agents.availability_presentation import availability_windows_from_slots
from app.agents.v2.availability_reference_interpreter import ReferenceDecision
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_read_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.orchestrator import _normalize_availability_search_continuation
from app.services.agent_v2.planner import PlannerContext, plan_turn

NOW = datetime.fromisoformat("2026-10-22T12:00:00+03:00")
SERVICE_ID = "service-hydra"


def _decision() -> ReferenceDecision:
    return ReferenceDecision(
        action="refresh_availability",
        option_ref=None,
        relative_direction="next",
    )


def _understanding(*, date_constraint: DateConstraint | None = None) -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                entities=TurnEntities(
                    service=EntityReference(ref="S1"),
                    date=date_constraint,
                ),
                execution_intent="informational",
                continues_previous=True,
            )
        ]
    )


def _context(
    *,
    date_value: dict[str, object],
    checked_dates: list[str],
    has_more: bool,
    truncated: bool,
    selected: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "date": date_value,
        "availability_checked_dates": checked_dates,
        "availability_presentation_has_more": has_more,
        "availability_search_truncated": truncated,
        "availability_reference_options": [
            {"option_ref": "opt_1", "concrete": False, "start_time_24h": "15:00"}
        ],
    }
    if selected is not None:
        value["last_selected_option_ref"] = selected
    return value


def _normalized_date(context: dict[str, object]) -> DateConstraint | None:
    result = _normalize_availability_search_continuation(
        _understanding(),
        decision=_decision(),
        availability_reference_context=context,
    )
    return result.operations[0].entities.date


def test_e1_core_next_available_advances_to_next_customer_search_period_with_one_read() -> None:
    day_a_slots = [
        {
            "doctor_id": "doctor-a",
            "doctor_name": "Doctor A",
            "start_local": f"2026-10-22T{hour:02d}:{minute:02d}:00+03:00",
            "end_local": f"2026-10-22T{hour:02d}:{minute + 15:02d}:00+03:00",
        }
        for hour, minute in [(15, 0), (15, 15), (15, 30), (15, 45), (16, 0), (16, 15), (16, 30), (16, 45), (17, 0), (17, 15), (17, 30), (17, 45)]
    ]
    day_a_slots.append(
        {
            "doctor_id": "doctor-a",
            "doctor_name": "Doctor A",
            "start_local": "2026-10-22T18:00:00+03:00",
            "end_local": "2026-10-22T18:15:00+03:00",
        }
    )
    day_b_slots = [
        {
            "doctor_id": "doctor-a",
            "doctor_name": "Doctor A",
            "start_local": f"2026-10-23T10:{minute:02d}:00+03:00",
            "end_local": f"2026-10-23T10:{minute + 15:02d}:00+03:00",
        }
        for minute in (0, 15, 30)
    ]
    day_a_windows = availability_windows_from_slots(day_a_slots)
    day_b_windows = availability_windows_from_slots(day_b_slots)
    assert [(row["start_time_24h"], row["end_time_24h"]) for row in day_a_windows] == [("15:00", "18:00")]
    assert [(row["start_time_24h"], row["end_time_24h"]) for row in day_b_windows] == [("10:00", "10:30")]
    assert all(row["start_time_24h"] != "15:15" for row in day_a_windows)

    context = _context(
        date_value={"mode": "next_available", "start_date": None, "end_date": None},
        checked_dates=["2026-10-22"],
        has_more=False,
        truncated=True,
    )
    normalized = _normalize_availability_search_continuation(
        _understanding(date_constraint=DateConstraint(mode="next_available")),
        decision=_decision(),
        availability_reference_context=context,
    )
    assert normalized.operations[0].entities.date == DateConstraint(
        mode="from_date", start_date="2026-10-23"
    )

    semantic = build_semantic_context(
        {
            "services": [{"id": SERVICE_ID, "name": "Hydrafacial"}],
            "doctors": [],
            "branches": [],
        }
    )
    semantic = with_safe_read_context(
        semantic,
        read_context={
            "operation_type": "availability",
            "service_id": SERVICE_ID,
            "date": {"mode": "next_available", "start_date": None, "end_date": None},
            "availability_option_count": 47,
        },
    )
    plan = plan_turn(
        normalized,
        PlannerContext(
            semantic_context=semantic,
            active_task=None,
            now=NOW,
        ),
    )
    availability_reads = [
        read
        for step in plan.steps
        for read in step.reads
        if read.kind == "availability"
    ]
    assert len(availability_reads) == 1
    assert availability_reads[0].parameters["date"] == {
        "mode": "from_date",
        "start_date": "2026-10-23",
        "end_date": None,
    }


def test_e2_more_windows_in_current_result_does_not_advance_search_date() -> None:
    original = _understanding(date_constraint=DateConstraint(mode="next_available"))
    result = _normalize_availability_search_continuation(
        original,
        decision=_decision(),
        availability_reference_context=_context(
            date_value={"mode": "next_available", "start_date": None, "end_date": None},
            checked_dates=["2026-10-22"],
            has_more=True,
            truncated=True,
        ),
    )
    assert result == original


def test_e3_exhausted_presentation_with_truncated_search_starts_after_last_checked_date() -> None:
    assert _normalized_date(
        _context(
            date_value={"mode": "from_date", "start_date": "2026-10-20", "end_date": None},
            checked_dates=["2026-10-20", "2026-10-21", "2026-10-22"],
            has_more=False,
            truncated=True,
        )
    ) == DateConstraint(mode="from_date", start_date="2026-10-23")


def test_e4_exact_date_scope_never_silently_advances_to_next_day() -> None:
    original = _understanding(date_constraint=DateConstraint(mode="exact", start_date="2026-10-24"))
    result = _normalize_availability_search_continuation(
        original,
        decision=_decision(),
        availability_reference_context=_context(
            date_value={"mode": "exact", "start_date": "2026-10-24", "end_date": None},
            checked_dates=["2026-10-24"],
            has_more=False,
            truncated=True,
        ),
    )
    assert result == original


def test_e5_bounded_range_progresses_only_inside_original_end_date() -> None:
    inside = _normalized_date(
        _context(
            date_value={"mode": "range", "start_date": "2026-10-22", "end_date": "2026-10-26"},
            checked_dates=["2026-10-22", "2026-10-23"],
            has_more=False,
            truncated=True,
        )
    )
    assert inside == DateConstraint(
        mode="range",
        start_date="2026-10-24",
        end_date="2026-10-26",
    )

    original = _understanding(
        date_constraint=DateConstraint(
            mode="range", start_date="2026-10-22", end_date="2026-10-23"
        )
    )
    exhausted = _normalize_availability_search_continuation(
        original,
        decision=_decision(),
        availability_reference_context=_context(
            date_value={"mode": "range", "start_date": "2026-10-22", "end_date": "2026-10-23"},
            checked_dates=["2026-10-22", "2026-10-23"],
            has_more=False,
            truncated=True,
        ),
    )
    assert exhausted == original


def test_e6_selected_option_relative_next_keeps_existing_option_navigation() -> None:
    original = _understanding(date_constraint=DateConstraint(mode="next_available"))
    result = _normalize_availability_search_continuation(
        original,
        decision=ReferenceDecision(
            action="select_presented_option",
            option_ref="opt_2",
            relative_direction="next",
        ),
        availability_reference_context=_context(
            date_value={"mode": "next_available", "start_date": None, "end_date": None},
            checked_dates=["2026-10-22"],
            has_more=False,
            truncated=True,
            selected="opt_1",
        ),
    )
    assert result == original


def test_e7_repeated_open_ended_pagination_moves_forward_without_looping_back() -> None:
    first = _normalized_date(
        _context(
            date_value={"mode": "next_available", "start_date": None, "end_date": None},
            checked_dates=["2026-10-22"],
            has_more=False,
            truncated=True,
        )
    )
    second = _normalized_date(
        _context(
            date_value={"mode": "from_date", "start_date": "2026-10-23", "end_date": None},
            checked_dates=["2026-10-23", "2026-10-24"],
            has_more=False,
            truncated=True,
        )
    )
    assert first == DateConstraint(mode="from_date", start_date="2026-10-23")
    assert second == DateConstraint(mode="from_date", start_date="2026-10-25")


def test_e8_non_truncated_search_does_not_invent_another_backend_horizon() -> None:
    original = _understanding(date_constraint=DateConstraint(mode="from_date", start_date="2026-10-23"))
    result = _normalize_availability_search_continuation(
        original,
        decision=_decision(),
        availability_reference_context=_context(
            date_value={"mode": "from_date", "start_date": "2026-10-23", "end_date": None},
            checked_dates=["2026-10-23", "2026-10-24"],
            has_more=False,
            truncated=False,
        ),
    )
    assert result == original
