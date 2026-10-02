from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.v2.availability_scope import (
    AVAILABILITY_PRESENTATION_SCOPE_KEY,
    availability_scope_key,
)
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.services.agent_v2.live_chat import _verified_read_context_from_turn
from app.services.agent_v2.orchestrator import _availability_presentation_continuation
from app.services.agent_v2.planner import PlanStep, ReadRequest, TurnPlan


def _understanding() -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                entities=TurnEntities(),
                continues_previous=True,
            )
        ]
    )


def _plan(parameters: dict[str, object]) -> TurnPlan:
    return TurnPlan(
        steps=[
            PlanStep(
                operation_index=0,
                operation_type="availability",
                disposition="read",
                reads=[ReadRequest(kind="availability", parameters=parameters)],
                response_goal="present_availability",
                facts=parameters,
            )
        ]
    )


@pytest.mark.parametrize(
    ("changed_key", "changed_value"),
    [
        ("doctor_id", "doctor-2"),
        ("device_key", "prime_lase"),
        ("date", {"mode": "exact", "value": "2026-10-04"}),
        ("time", {"mode": "before", "value": "17:00"}),
        ("service_id", "service-prp"),
    ],
)
def test_changed_verified_availability_scope_resets_presentation_cursor(
    changed_key: str,
    changed_value: object,
) -> None:
    previous = {
        "service_id": "service-hydrafacial",
        "doctor_id": "doctor-1",
        "device_key": "candela",
        "date": {"mode": "exact", "value": "2026-10-03"},
        "time": {"mode": "after", "value": "17:00"},
    }
    current = dict(previous)
    current[changed_key] = changed_value
    recent = {
        AVAILABILITY_PRESENTATION_SCOPE_KEY: availability_scope_key(previous),
        "availability_presented_window_keys": ["shown-window"],
    }

    assert not _availability_presentation_continuation(
        _understanding(),
        _plan(current),
        recent,
    )


def test_same_verified_availability_scope_preserves_pagination_cursor() -> None:
    parameters = {
        "service_id": "service-hydrafacial",
        "doctor_id": "doctor-1",
        "date": {"mode": "exact", "value": "2026-10-03"},
    }
    recent = {
        AVAILABILITY_PRESENTATION_SCOPE_KEY: availability_scope_key(parameters),
        "availability_presented_window_keys": ["shown-window"],
    }

    assert _availability_presentation_continuation(
        _understanding(),
        _plan(parameters),
        recent,
    )
def test_live_read_context_starts_new_cursor_for_narrowed_verified_scope() -> None:
    broad = {"service_id": "service-laser", "date": {"mode": "exact", "value": "2026-10-03"}}
    narrowed = {**broad, "device_key": "prime_lase"}
    previous_context = {
        AVAILABILITY_PRESENTATION_SCOPE_KEY: availability_scope_key(broad),
        "availability_presented_window_keys": ["broad-window"],
    }
    plan = _plan(narrowed)
    turn = SimpleNamespace(
        plan=plan,
        understanding=_understanding(),
        traces=(
            SimpleNamespace(
                operation_index=0,
                outcome=SimpleNamespace(
                    facts={
                        "availability": {
                            "service_name": "Laser",
                            "available_option_count": 1,
                            "availability_windows": [
                                {
                                    "doctor_name": "Ahmed",
                                    "laser_device_name": "Prime",
                                    "start_local": "2026-10-03T18:00:00+03:00",
                                    "end_local": "2026-10-03T18:30:00+03:00",
                                }
                            ],
                        }
                    }
                ),
            ),
        ),
    )
    context = _verified_read_context_from_turn(
        object(),
        workspace=SimpleNamespace(),
        turn=turn,
        previous_read_context=previous_context,
    )

    assert context is not None
    assert context[AVAILABILITY_PRESENTATION_SCOPE_KEY] == availability_scope_key(narrowed)
    assert "broad-window" not in context["availability_presented_window_keys"]
    assert len(context["availability_presented_window_keys"]) == 1
