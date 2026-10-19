from __future__ import annotations

from datetime import datetime

from app.services.agent_v2.compound_turn_policy import (
    apply_compound_runtime_cursor,
    normalize_compound_turn_plan,
    resolve_compound_followup_after_reads,
)
from app.services.agent_v2.planner import PlanStep, ReadRequest, TurnPlan, WriteIntent
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.turn_normalization import guard_multiple_booking_writes


def _booking(index: int, service_id: str, *, device_key: str | None = None) -> PlanStep:
    params: dict[str, object] = {
        "service_id": service_id,
        "doctor_id": f"doctor-{index}",
        "date": {
            "mode": "exact",
            "start_date": "2026-09-20",
            "end_date": None,
        },
        "time": {
            "mode": "exact",
            "start_time": "12:00",
            "end_time": None,
            "start_time_ambiguity": "none",
            "end_time_ambiguity": "none",
        },
        "package_usage": "unspecified",
    }
    if device_key is not None:
        params["device_key"] = device_key
    return PlanStep(
        operation_index=index,
        operation_type="book",
        disposition="read",
        reads=[ReadRequest(kind="availability", parameters=dict(params))],
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters=dict(params),
        ),
        state_action="start_booking",
        response_goal="present_availability",
        facts={**params, "exact_time_requested": True},
    )


def _purchase(index: int, service_id: str, *, device_key: str | None = None) -> PlanStep:
    params: dict[str, object] = {
        "service_id": service_id,
        "package_sessions": 3,
        "package_usage": "unspecified",
    }
    if device_key is not None:
        params["device_key"] = device_key
    return PlanStep(
        operation_index=index,
        operation_type="buy_package",
        disposition="read",
        reads=[ReadRequest(kind="package_offers", parameters=dict(params))],
        write_intent=WriteIntent(kind="buy_package", authorized=True, parameters=dict(params)),
        response_goal="package_purchased",
        facts=params,
    )


def _catalog() -> dict[str, object]:
    return {
        "services": [
            {"id": "service-a", "duration_minutes": 30},
            {"id": "service-b", "duration_minutes": 45},
            {"id": "service-c", "duration_minutes": 20},
        ]
    }


def _time(step: PlanStep) -> dict[str, object]:
    assert step.write_intent is not None
    raw = step.write_intent.parameters["time"]
    assert isinstance(raw, dict)
    return raw


def _read_time(step: PlanStep) -> dict[str, object]:
    raw = step.reads[0].parameters["time"]
    assert isinstance(raw, dict)
    return raw


def _slot(start: str, end: str, *, doctor_id: str = "doctor-1") -> dict[str, object]:
    return {
        "branch_id": "branch-1",
        "branch_name": "Clinic",
        "service_id": "service-b",
        "service_name": "Service B",
        "doctor_id": doctor_id,
        "doctor_name": "Doctor",
        "start_at": start,
        "end_at": end,
        "start_local": start,
        "end_local": end,
        "start_time_24h": datetime.fromisoformat(start).strftime("%H:%M"),
        "end_time_24h": datetime.fromisoformat(end).strftime("%H:%M"),
        "duration_minutes": 30,
        "price_minor": 10000,
        "currency": "EGP",
        "laser_device_key": None,
        "laser_device_name": None,
    }


def test_same_anchor_bookings_search_back_to_back_in_customer_order() -> None:
    plan = TurnPlan(
        steps=[
            _booking(0, "service-a"),
            _booking(1, "service-b"),
            _booking(2, "service-c"),
        ]
    )

    normalized = normalize_compound_turn_plan(plan, catalog=_catalog())

    assert [str(_time(step)["start_time"]) for step in normalized.steps] == [
        "12:00",
        "12:30",
        "13:15",
    ]
    assert [str(_time(step)["mode"]) for step in normalized.steps] == ["exact", "after", "after"]
    assert [str(_read_time(step)["mode"]) for step in normalized.steps] == [
        "exact",
        "after",
        "after",
    ]
    assert [step.facts["compound_visit_sequence_index"] for step in normalized.steps] == [0, 1, 2]


def test_runtime_cursor_replaces_predicted_followup_start_with_actual_previous_end() -> None:
    normalized = normalize_compound_turn_plan(
        TurnPlan(steps=[_booking(0, "service-a"), _booking(1, "service-b")]),
        catalog=_catalog(),
    )
    second = normalized.steps[1]

    shifted = apply_compound_runtime_cursor(
        second,
        previous_end_at=datetime.fromisoformat("2026-09-20T15:10:00+03:00"),
        timezone_name="Africa/Cairo",
    )

    assert _time(shifted)["mode"] == "after"
    assert _time(shifted)["start_time"] == "15:10"
    assert _read_time(shifted)["start_time"] == "15:10"


def test_followup_selects_earliest_live_verified_slot_and_narrows_outcome_read() -> None:
    normalized = normalize_compound_turn_plan(
        TurnPlan(steps=[_booking(0, "service-a"), _booking(1, "service-b")]),
        catalog=_catalog(),
    )
    second = normalized.steps[1]
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=True,
                payload={
                    "slots": [
                        _slot("2026-09-20T15:30:00+03:00", "2026-09-20T16:00:00+03:00"),
                        _slot("2026-09-20T15:00:00+03:00", "2026-09-20T15:30:00+03:00"),
                    ],
                    "matching_slot_count": 2,
                },
            )
        ]
    )

    advanced, narrowed, handled = resolve_compound_followup_after_reads(second, reads)

    assert handled is True
    assert advanced.disposition == "write_ready"
    assert advanced.write_intent is not None
    assert advanced.write_intent.parameters["start_at"] == "2026-09-20T15:00:00+03:00"
    assert _time(advanced)["mode"] == "exact"
    assert _time(advanced)["start_time"] == "15:00"
    slots = narrowed.results[0].payload["slots"]
    assert isinstance(slots, list)
    assert len(slots) == 1
    assert slots[0]["start_at"] == "2026-09-20T15:00:00+03:00"


def test_followup_with_no_later_slot_blocks_instead_of_forcing_overlap() -> None:
    normalized = normalize_compound_turn_plan(
        TurnPlan(steps=[_booking(0, "service-a"), _booking(1, "service-b")]),
        catalog=_catalog(),
    )
    second = normalized.steps[1]
    reads = ReadExecutionBundle(
        results=[ReadResult(kind="availability", ok=True, payload={"slots": []})]
    )

    advanced, _reads, handled = resolve_compound_followup_after_reads(second, reads)

    assert handled is True
    assert advanced.disposition == "blocked"
    assert advanced.response_goal == "no_availability"


def test_booking_before_same_package_purchase_is_reordered_and_made_package_required() -> None:
    plan = TurnPlan(
        steps=[
            _booking(0, "service-a", device_key="prime"),
            _purchase(1, "service-a", device_key="prime"),
        ]
    )

    normalized = normalize_compound_turn_plan(plan, catalog=_catalog())

    assert [step.operation_index for step in normalized.steps] == [1, 0]
    booking = normalized.steps[1]
    assert booking.write_intent is not None
    assert booking.write_intent.parameters["package_usage"] == "use_existing"
    assert booking.facts["depends_on_package_purchase_operation_index"] == 1


def test_package_for_different_service_does_not_become_booking_dependency() -> None:
    plan = TurnPlan(
        steps=[
            _booking(0, "service-a", device_key="prime"),
            _purchase(1, "service-b", device_key="prime"),
        ]
    )

    normalized = normalize_compound_turn_plan(plan, catalog=_catalog())

    assert [step.operation_index for step in normalized.steps] == [0, 1]
    booking = normalized.steps[0]
    assert booking.write_intent is not None
    assert booking.write_intent.parameters["package_usage"] == "unspecified"
    assert "depends_on_package_purchase_operation_index" not in booking.facts


def test_package_for_different_device_does_not_become_booking_dependency() -> None:
    plan = TurnPlan(
        steps=[
            _booking(0, "service-a", device_key="prime"),
            _purchase(1, "service-a", device_key="candela"),
        ]
    )

    normalized = normalize_compound_turn_plan(plan, catalog=_catalog())

    assert [step.operation_index for step in normalized.steps] == [0, 1]
    booking = normalized.steps[0]
    assert booking.write_intent is not None
    assert booking.write_intent.parameters["package_usage"] == "unspecified"


def test_different_requested_anchors_are_not_retimed() -> None:
    first = _booking(0, "service-a")
    second = _booking(1, "service-b")
    assert second.write_intent is not None
    second_time = {
        **second.write_intent.parameters["time"],
        "start_time": "14:00",
    }
    second = second.model_copy(
        update={
            "reads": [
                second.reads[0].model_copy(
                    update={
                        "parameters": {
                            **second.reads[0].parameters,
                            "time": second_time,
                        }
                    }
                )
            ],
            "write_intent": second.write_intent.model_copy(
                update={
                    "parameters": {
                        **second.write_intent.parameters,
                        "time": second_time,
                    }
                }
            ),
            "facts": {**second.facts, "time": second_time},
        }
    )

    normalized = normalize_compound_turn_plan(
        TurnPlan(steps=[first, second]),
        catalog=_catalog(),
    )

    assert [str(_time(step)["start_time"]) for step in normalized.steps] == ["12:00", "14:00"]
    assert all("compound_visit_sequenced" not in step.facts for step in normalized.steps)


def test_package_first_keeps_dependent_and_other_booking_in_same_compound_visit() -> None:
    plan = TurnPlan(
        steps=[
            _purchase(0, "service-a", device_key="prime"),
            _booking(1, "service-a", device_key="prime"),
            _booking(2, "service-b", device_key="candela"),
        ]
    )

    normalized = normalize_compound_turn_plan(plan, catalog=_catalog())

    assert [step.operation_index for step in normalized.steps] == [0, 1, 2]
    bookings = [step for step in normalized.steps if step.write_intent and step.write_intent.kind == "booking"]
    assert len(bookings) == 2
    dependent, other = bookings
    assert dependent.write_intent is not None
    assert dependent.write_intent.parameters["package_usage"] == "use_existing"
    assert dependent.facts["depends_on_package_purchase_operation_index"] == 0
    assert _time(dependent)["mode"] == "exact"
    assert _time(dependent)["start_time"] == "12:00"
    assert _time(other)["mode"] == "after"
    assert _time(other)["start_time"] == "12:30"


def test_multiple_booking_guard_allows_only_normalized_atomic_compound_group() -> None:
    raw = TurnPlan(steps=[_booking(0, "service-a"), _booking(1, "service-b")])
    blocked = guard_multiple_booking_writes(raw)
    assert len(blocked.steps) == 1
    assert blocked.steps[0].write_intent is None
    assert blocked.steps[0].facts["multiple_appointment_write_blocked"] is True

    normalized = normalize_compound_turn_plan(raw, catalog=_catalog())
    allowed = guard_multiple_booking_writes(normalized)
    assert len(allowed.steps) == 2
    assert all(step.write_intent is not None for step in allowed.steps)
    groups = {step.facts.get("compound_write_group") for step in allowed.steps}
    assert len(groups) == 1
    assert None not in groups
