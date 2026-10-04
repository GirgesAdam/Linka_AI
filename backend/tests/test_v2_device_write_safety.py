from __future__ import annotations

from datetime import UTC, datetime

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.planner import PlannerContext, plan_turn

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)


def _context() -> PlannerContext:
    semantic = build_semantic_context(
        {
            "services": [
                {
                    "id": "service-underarm",
                    "name": "ليزر إبط",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_key": "candela", "device_name": "Candela Gentle"},
                        {"device_key": "prime", "device_name": "Prime Lase"},
                    ],
                },
                {
                    "id": "service-hydrafacial",
                    "name": "هيدرافيشل",
                    "requires_laser_device": False,
                },
            ],
            "doctors": [
                {"id": "doctor-maryam", "name": "مريم"},
                {"id": "doctor-sarah", "name": "سارة"},
            ],
        }
    )
    return PlannerContext(semantic_context=semantic, active_task=None, now=NOW)


def _booking(
    *,
    service_ref: str = "S1",
    doctor_ref: str | None = "D1",
    device: EntityReference | None = None,
) -> TurnOperation:
    return TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(ref=service_ref),
            doctor=EntityReference(ref=doctor_ref) if doctor_ref is not None else None,
            device=device,
            date=DateConstraint(mode="exact", start_date="2026-09-12"),
            time=TimeConstraint(mode="exact", start_time="20:00"),
        ),
    )


def test_ambiguous_device_is_clarified_before_availability_or_write() -> None:
    operation = _booking(
        device=EntityReference(candidate_refs=["V1", "V2"]),
    )
    turn = TiaTurnUnderstanding(operations=[operation], safety_signals=[])

    step = plan_turn(turn, _context()).steps[0]

    assert step.disposition == "clarify"
    assert step.clarification_field == "device"
    assert step.reads == []
    assert step.write_intent is None


def test_laser_booking_without_device_never_reaches_availability_or_write() -> None:
    turn = TiaTurnUnderstanding(operations=[_booking()], safety_signals=[])
    step = plan_turn(turn, _context()).steps[0]

    assert step.disposition == "read"
    assert step.response_goal == "answer_price"
    assert [read.kind for read in step.reads] == ["service_catalog"]
    assert step.write_intent is None


def test_laser_booking_with_selected_device_presents_price_before_write() -> None:
    turn = TiaTurnUnderstanding(
        operations=[_booking(device=EntityReference(ref="V1"))],
        safety_signals=[],
    )
    step = plan_turn(turn, _context()).steps[0]

    assert step.disposition == "read"
    assert step.response_goal == "answer_price"
    assert [read.kind for read in step.reads] == ["service_catalog", "availability"]
    assert step.write_intent is None
    assert step.facts["device_key"] == "candela"
    assert step.facts["exact_time_requested"] is True
    assert step.facts["booking_next_field"] == "booking"


def test_multi_device_laser_booking_requires_device_price_step_before_slots() -> None:
    turn = TiaTurnUnderstanding(operations=[_booking()], safety_signals=[])
    step = plan_turn(turn, _context()).steps[0]

    assert step.disposition == "read"
    assert step.response_goal == "answer_price"
    assert [read.kind for read in step.reads] == ["service_catalog"]
    assert step.write_intent is None


def test_doctor_is_not_requested_before_selected_device_price_is_presented() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _booking(
                doctor_ref=None,
                device=EntityReference(ref="V1"),
            )
        ],
        safety_signals=[],
    )
    step = plan_turn(turn, _context()).steps[0]

    assert step.disposition == "read"
    assert step.response_goal == "answer_price"
    assert step.write_intent is None
    assert step.clarification_field is None


def test_non_laser_service_skips_device_but_requires_commercial_basis_before_write() -> None:
    turn = TiaTurnUnderstanding(
        operations=[_booking(service_ref="S2")],
        safety_signals=[],
    )
    step = plan_turn(turn, _context()).steps[0]

    assert step.disposition == "read"
    assert step.response_goal == "answer_price"
    assert step.write_intent is None
    assert [read.kind for read in step.reads] == ["service_catalog", "availability"]
    assert step.facts["service_requires_laser_device"] is False
    assert step.facts["commercial_basis_key"] == "service-hydrafacial|none|standalone"
