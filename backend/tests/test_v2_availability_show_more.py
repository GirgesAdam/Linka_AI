from __future__ import annotations

from datetime import datetime

from app.agents.v2.availability_composer import (
    _paged_contract,
    deterministic_availability_fallback,
)
from app.agents.v2.availability_scope import (
    AVAILABILITY_PRESENTATION_SCOPE_KEY,
    availability_scope_key,
)
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_read_context
from app.agents.v2.turn_contract import (
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import merge_verified_read_context
from app.services.agent_v2.orchestrator import _availability_presentation_continuation
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.planner import PlannerContext, plan_turn
from app.services.agent_v2.response_contract import build_customer_response_contract

NOW = datetime.fromisoformat("2026-10-02T19:50:00+03:00")
PARAMETERS = {
    "service_id": "service-hydra",
    "date": {"mode": "exact", "start_date": "2026-10-08", "end_date": None},
    "package_usage": "unspecified",
}


def _base_context():
    return build_semantic_context(
        {
            "services": [{"id": "service-hydra", "name": "Hydrafacial"}],
            "doctors": [
                {
                    "id": "doctor-mariam",
                    "name": "دكتورة مريم",
                    "service_ids": ["service-hydra"],
                }
            ],
            "branches": [],
        }
    )


def _windows() -> list[dict[str, object]]:
    return [
        {
            "doctor_name": "دكتورة مريم",
            "start_local": f"2026-10-08T{hour:02d}:00:00+03:00",
            "end_local": f"2026-10-08T{hour:02d}:30:00+03:00",
            "start_time_24h": f"{hour:02d}:00",
            "end_time_24h": f"{hour:02d}:30",
        }
        for hour in range(9, 21)
    ]


def _outcome() -> TurnOutcome:
    windows = _windows()
    return TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={
            "availability": {
                "service_name": "Hydrafacial",
                "checked_dates": ["2026-10-08"],
                "availability_windows": windows,
                "available_option_count": len(windows),
                "search_truncated": False,
            }
        },
    )


def _continued_understanding(*, doctor_ref: str | None = None) -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                entities=TurnEntities(
                    doctor=(
                        EntityReference(ref=doctor_ref)
                        if doctor_ref is not None
                        else None
                    )
                ),
                continues_previous=True,
            )
        ]
    )


def _same_scope_plan():
    context = with_safe_read_context(
        _base_context(),
        read_context={
            "operation_type": "availability",
            "service_id": "service-hydra",
            "date": PARAMETERS["date"],
            "availability_option_count": 12,
        },
    )
    merged = merge_verified_read_context(_continued_understanding(), context)
    plan = plan_turn(
        merged,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    return merged, plan


def test_same_scope_show_more_inherits_verified_scope_and_preserves_cursor() -> None:
    understanding, plan = _same_scope_plan()
    previous = {
        AVAILABILITY_PRESENTATION_SCOPE_KEY: availability_scope_key(PARAMETERS),
        "availability_presented_window_keys": ["shown-window"],
    }

    assert plan.steps[0].reads[0].parameters == PARAMETERS
    assert _availability_presentation_continuation(
        understanding,
        plan,
        previous,
    )


def test_show_more_pagination_progresses_three_pages_then_exhausts() -> None:
    contract = build_customer_response_contract([_outcome()])
    shown: set[str] = set()
    pages: list[list[str]] = []

    for _ in range(3):
        page, keys, _has_more = _paged_contract(
            contract,
            shown_window_keys=shown,
        )
        window_fact = next(
            fact for fact in page.units[0].facts if fact.key == "availability_windows"
        )
        pages.append(
            [
                str(row["start_local"])
                for row in window_fact.value
                if isinstance(row, dict)
            ]
        )
        assert len(keys) == 4
        assert shown.isdisjoint(keys)
        shown.update(keys)

    exhausted, exhausted_keys, exhausted_more = _paged_contract(
        contract,
        shown_window_keys=shown,
    )
    exhausted_text = deterministic_availability_fallback(
        exhausted,
        arabic=True,
        has_more_by_unit=exhausted_more,
        continuation=True,
    )

    assert [len(page) for page in pages] == [4, 4, 4]
    assert len({value for page in pages for value in page}) == 12
    assert exhausted_keys == []
    assert exhausted_more == {0: False}
    assert "مفيش مواعيد إضافية متاحة في الأيام دي" in exhausted_text
    assert "نطاق البحث" not in exhausted_text


def test_scope_narrowing_still_resets_cursor_under_f5() -> None:
    context = with_safe_read_context(
        _base_context(),
        read_context={
            "operation_type": "availability",
            "service_id": "service-hydra",
            "date": PARAMETERS["date"],
            "availability_option_count": 12,
        },
    )
    understanding = merge_verified_read_context(
        _continued_understanding(doctor_ref="D1"),
        context,
    )
    plan = plan_turn(
        understanding,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    previous = {
        AVAILABILITY_PRESENTATION_SCOPE_KEY: availability_scope_key(PARAMETERS),
        "availability_presented_window_keys": ["old-window"],
    }

    assert plan.steps[0].reads[0].parameters["doctor_id"] == "doctor-mariam"
    assert not _availability_presentation_continuation(
        understanding,
        plan,
        previous,
    )


def test_no_previous_verified_availability_cannot_continue_cursor() -> None:
    understanding, plan = _same_scope_plan()

    assert not _availability_presentation_continuation(
        understanding,
        plan,
        None,
    )
