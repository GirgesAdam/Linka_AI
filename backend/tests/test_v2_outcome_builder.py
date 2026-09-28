from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import (
    OutcomeBuildError,
    build_step_outcome,
    customer_visible_outcome,
)
from app.services.agent_v2.planner import PlanStep, ReadRequest, WriteIntent
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.response_contract import build_customer_response_contract

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)


def _semantic_context():
    return build_semantic_context(
        {
            "services": [
                {"id": "service-1", "name": "ليزر إبط"},
                {"id": "service-2", "name": "ليزر بكيني"},
            ],
            "doctors": [{"id": "doctor-1", "name": "مريم"}],
            "appointments": [
                {
                    "appointment_id": "appointment-1",
                    "service_id": "service-1",
                    "doctor_id": "doctor-1",
                    "status": "confirmed",
                    "start_local": "2026-09-17T19:00:00+03:00",
                }
            ],
        }
    )


def _turn(operation: TurnOperation) -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(operations=[operation], safety_signals=[])


def test_semantic_service_ambiguity_becomes_labeled_choices_without_ids() -> None:
    context = _semantic_context()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(text="ليزر", ref=None, candidate_refs=["S1", "S2"])
        ),
        selection=None,
        package_usage="unspecified",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="clarify",
        response_goal="ask_service_choice",
        clarification_field="service",
    )

    outcome = build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=context,
    )
    visible = customer_visible_outcome(outcome)

    assert outcome.status == "needs_input"
    assert [choice.label for choice in outcome.choices] == ["ليزر إبط", "ليزر بكيني"]
    assert visible["choices"] == [
        {"label": "ليزر إبط", "facts": {}},
        {"label": "ليزر بكيني", "facts": {}},
    ]
    assert "S1" not in str(visible)
    assert "service-1" not in str(visible)


def test_availability_outcome_uses_windows_and_hides_canonical_ids() -> None:
    context = _semantic_context()
    operation = TurnOperation(
        type="availability",
        entities=TurnEntities(
            service=EntityReference(text=None, ref="S1", candidate_refs=[]),
        ),
        selection=None,
        package_usage="unspecified",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="availability",
        disposition="read",
        reads=[ReadRequest(kind="availability")],
        response_goal="present_availability",
        facts={"service_id": "service-1"},
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=True,
                payload={
                    "service_id": "service-1",
                    "service_name": "ليزر إبط",
                    "checked_dates": ["2026-09-17"],
                    "slots": [
                        {
                            "branch_id": "branch-1",
                            "doctor_id": "doctor-1",
                            "doctor_name": "مريم",
                            "service_id": "service-1",
                            "service_name": "ليزر إبط",
                            "start_local": "2026-09-17T19:00:00+03:00",
                            "end_local": "2026-09-17T19:30:00+03:00",
                            "price_minor": 50000,
                            "currency": "EGP",
                            "laser_device_key": "candela_gentle",
                            "laser_device_name": "Candela Gentle",
                        },
                        {
                            "branch_id": "branch-1",
                            "doctor_id": "doctor-1",
                            "doctor_name": "مريم",
                            "service_id": "service-1",
                            "service_name": "ليزر إبط",
                            "start_local": "2026-09-17T19:30:00+03:00",
                            "end_local": "2026-09-17T20:00:00+03:00",
                            "price_minor": 50000,
                            "currency": "EGP",
                            "laser_device_key": "candela_gentle",
                            "laser_device_name": "Candela Gentle",
                        },
                    ],
                    "search_truncated": False,
                },
            )
        ]
    )

    outcome = build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=context,
        reads=reads,
    )
    visible = customer_visible_outcome(outcome)
    availability = visible["facts"]["availability"]

    assert outcome.status == "answered"
    assert availability["price"] == "500.00 EGP"
    assert availability["availability_windows"] == [
        {
            "doctor_name": "مريم",
            "laser_device_name": "Candela Gentle",
            "start_local": "2026-09-17T19:00:00+03:00",
            "end_local": "2026-09-17T19:30:00+03:00",
            "start_time_24h": "19:00",
            "end_time_24h": "19:30",
        }
    ]
    assert "doctor-1" not in str(visible)
    assert "branch-1" not in str(visible)
    assert "service-1" not in str(visible)
    assert "candela_gentle" not in str(visible)


def _availability_outcome_for_slots(slots: list[dict[str, object]]) -> TurnOutcome:
    operation = TurnOperation(
        type="availability",
        entities=TurnEntities(
            service=EntityReference(text="ليزر إبط", ref="S1", candidate_refs=[]),
        ),
        selection=None,
        package_usage="unspecified",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="availability",
        disposition="read",
        reads=[ReadRequest(kind="availability")],
        response_goal="present_availability",
        facts={"service_id": "service-1"},
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=True,
                payload={
                    "service_id": "service-1",
                    "service_name": "ليزر إبط",
                    "checked_dates": ["2026-09-17"],
                    "slots": slots,
                    "search_truncated": False,
                },
            )
        ]
    )
    return build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=_semantic_context(),
        reads=reads,
    )


def _laser_slot(
    *,
    key: str,
    name: str,
    price_minor: int,
    start: str,
) -> dict[str, object]:
    return {
        "branch_id": "branch-1",
        "doctor_id": "doctor-1",
        "doctor_name": "مريم",
        "service_id": "service-1",
        "service_name": "ليزر إبط",
        "start_local": f"2026-09-17T{start}:00+03:00",
        "end_local": f"2026-09-17T{start}:00+03:00",
        "price_minor": price_minor,
        "currency": "EGP",
        "laser_device_key": key,
        "laser_device_name": name,
    }


def test_availability_preserves_exact_device_price_binding() -> None:
    outcome = _availability_outcome_for_slots(
        [
            _laser_slot(
                key="candela_gentle",
                name="Candela Gentle",
                price_minor=65_000,
                start="19:00",
            ),
            _laser_slot(
                key="prime_lase",
                name="Prime Lase",
                price_minor=55_000,
                start="19:30",
            ),
        ]
    )
    availability = outcome.facts["availability"]

    assert availability["laser_device_options"] == [
        {"device_name": "Candela Gentle", "price": "650.00 EGP"},
        {"device_name": "Prime Lase", "price": "550.00 EGP"},
    ]
    assert "prices" not in availability
    assert "price" not in availability


def test_device_price_binding_does_not_depend_on_slot_order() -> None:
    outcome = _availability_outcome_for_slots(
        [
            _laser_slot(
                key="prime_lase",
                name="Prime Lase",
                price_minor=55_000,
                start="19:30",
            ),
            _laser_slot(
                key="candela_gentle",
                name="Candela Gentle",
                price_minor=65_000,
                start="19:00",
            ),
        ]
    )

    assert outcome.facts["availability"]["laser_device_options"] == [
        {"device_name": "Candela Gentle", "price": "650.00 EGP"},
        {"device_name": "Prime Lase", "price": "550.00 EGP"},
    ]


def test_repeated_slots_for_same_device_produce_one_price_option() -> None:
    outcome = _availability_outcome_for_slots(
        [
            _laser_slot(
                key="candela_gentle",
                name="Candela Gentle",
                price_minor=65_000,
                start="19:00",
            ),
            _laser_slot(
                key="candela_gentle",
                name="Candela Gentle",
                price_minor=65_000,
                start="19:30",
            ),
        ]
    )
    availability = outcome.facts["availability"]

    assert availability["laser_device_options"] == [
        {"device_name": "Candela Gentle", "price": "650.00 EGP"}
    ]
    assert availability["price"] == "650.00 EGP"


def test_conflicting_prices_for_same_device_fail_closed() -> None:
    outcome = _availability_outcome_for_slots(
        [
            _laser_slot(
                key="candela_gentle",
                name="Candela Gentle",
                price_minor=65_000,
                start="19:00",
            ),
            _laser_slot(
                key="candela_gentle",
                name="Candela Gentle",
                price_minor=70_000,
                start="19:30",
            ),
        ]
    )
    availability = outcome.facts["availability"]

    assert availability["laser_device_options"] == [{"device_name": "Candela Gentle"}]
    assert availability["device_price_conflicts"] == ["Candela Gentle"]
    assert "price" not in availability
    assert "prices" not in availability


def test_two_devices_with_same_price_remain_explicitly_bound() -> None:
    outcome = _availability_outcome_for_slots(
        [
            _laser_slot(
                key="candela_gentle",
                name="Candela Gentle",
                price_minor=55_000,
                start="19:00",
            ),
            _laser_slot(
                key="prime_lase",
                name="Prime Lase",
                price_minor=55_000,
                start="19:30",
            ),
        ]
    )
    availability = outcome.facts["availability"]

    assert availability["laser_device_options"] == [
        {"device_name": "Candela Gentle", "price": "550.00 EGP"},
        {"device_name": "Prime Lase", "price": "550.00 EGP"},
    ]
    assert "price" not in availability
    assert "prices" not in availability


def test_non_laser_availability_keeps_single_price_behavior() -> None:
    slot = _laser_slot(
        key="",
        name="",
        price_minor=180_000,
        start="19:00",
    )
    outcome = _availability_outcome_for_slots([slot])
    availability = outcome.facts["availability"]

    assert availability["price"] == "1800.00 EGP"
    assert "laser_device_options" not in availability


def test_single_laser_device_has_explicit_binding_and_legacy_single_price() -> None:
    outcome = _availability_outcome_for_slots(
        [
            _laser_slot(
                key="prime_lase",
                name="Prime Lase",
                price_minor=55_000,
                start="19:00",
            )
        ]
    )
    availability = outcome.facts["availability"]

    assert availability["laser_device_options"] == [
        {"device_name": "Prime Lase", "price": "550.00 EGP"}
    ]
    assert availability["price"] == "550.00 EGP"


def test_visible_availability_device_prices_hide_internal_device_keys() -> None:
    outcome = _availability_outcome_for_slots(
        [
            _laser_slot(
                key="candela_gentle",
                name="Candela Gentle",
                price_minor=65_000,
                start="19:00",
            ),
            _laser_slot(
                key="prime_lase",
                name="Prime Lase",
                price_minor=55_000,
                start="19:30",
            ),
        ]
    )
    visible = customer_visible_outcome(outcome)
    payload = str(visible)

    assert "candela_gentle" not in payload
    assert "prime_lase" not in payload
    assert "service-1" not in payload
    assert "doctor-1" not in payload
    assert "branch-1" not in payload
    assert "Candela Gentle" in payload
    assert "650.00 EGP" in payload
    assert "Prime Lase" in payload
    assert "550.00 EGP" in payload


def test_empty_exact_availability_becomes_requested_time_unavailable() -> None:
    context = _semantic_context()
    operation = TurnOperation(
        type="availability",
        entities=TurnEntities(time=TimeConstraint(mode="exact", start_time="19:00", end_time=None)),
        selection=None,
        package_usage="unspecified",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="availability",
        disposition="read",
        response_goal="present_availability",
        facts={"time": {"mode": "exact", "start_time": "19:00", "end_time": None}},
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=True,
                payload={"slots": [], "checked_dates": ["2026-09-17"]},
            )
        ]
    )

    outcome = build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=context,
        reads=reads,
    )
    assert outcome.status == "blocked"
    assert outcome.response_goal == "requested_time_unavailable"


def test_ambiguous_verified_appointments_become_customer_choices() -> None:
    context = _semantic_context()
    operation = TurnOperation(
        type="cancel_appointment",
        entities=TurnEntities(),
        selection=None,
        package_usage="unspecified",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="cancel_appointment",
        disposition="clarify",
        response_goal="ask_appointment_choice",
        clarification_field="appointment",
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="appointments",
                ok=True,
                payload={
                    "appointments": [
                        {
                            "appointment_id": "a-1",
                            "service_name": "ليزر إبط",
                            "doctor_name": "مريم",
                            "start_local": "2026-09-17T19:00:00+03:00",
                        },
                        {
                            "appointment_id": "a-2",
                            "service_name": "هيدرافيشل",
                            "doctor_name": "سارة",
                            "start_local": "2026-09-18T17:00:00+03:00",
                        },
                    ]
                },
            )
        ]
    )

    outcome = build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=context,
        reads=reads,
    )
    visible = customer_visible_outcome(outcome)

    assert outcome.status == "needs_input"
    assert len(outcome.choices) == 2
    assert "a-1" not in str(visible)
    assert "a-2" not in str(visible)
    assert "ليزر إبط" in outcome.choices[0].label


def test_write_ready_step_cannot_claim_success_without_execution_result() -> None:
    context = _semantic_context()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        selection=None,
        package_usage="unspecified",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        write_intent=WriteIntent(kind="booking", authorized=True, parameters={}),
        response_goal="booking_completed",
    )

    with pytest.raises(OutcomeBuildError):
        build_step_outcome(
            step,
            turn=_turn(operation),
            semantic_context=context,
        )


def test_successful_write_outcome_hides_action_identifiers_and_formats_money() -> None:
    context = _semantic_context()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        selection=None,
        package_usage="unspecified",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        response_goal="booking_completed",
        write_intent=WriteIntent(kind="booking", authorized=True, parameters={}),
    )
    outcome = build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=context,
        action_result={
            "ok": True,
            "appointment_id": "appointment-secret",
            "service_name": "ليزر إبط",
            "price_minor": 50000,
            "currency": "EGP",
        },
    )
    visible = customer_visible_outcome(outcome)

    assert outcome.status == "completed"
    assert visible["action_result"]["price"] == "500.00 EGP"
    assert "appointment-secret" not in str(visible)


def _service_price_outcome(
    *,
    price_minor: int,
    service_name: str = "PRP للبشرة",
) -> TurnOutcome:
    operation = TurnOperation(
        type="pricing",
        entities=TurnEntities(
            service=EntityReference(text=service_name, ref="S1", candidate_refs=[]),
        ),
        selection=None,
        package_usage="unspecified",
        requested_service_details=["price"],
        execution_intent="informational",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog", parameters={"service_id": "service-1"})],
        response_goal="answer_price",
        facts={"service_id": "service-1"},
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="service_catalog",
                ok=True,
                payload={
                    "service": {
                        "id": "service-1",
                        "name": service_name,
                        "price_minor": price_minor,
                        "currency": "EGP",
                    }
                },
            )
        ]
    )
    return build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=_semantic_context(),
        reads=reads,
    )


@pytest.mark.parametrize(
    ("price_minor", "expected"),
    [(200_000, "2000.00 EGP"), (210_000, "2100.00 EGP")],
)
def test_normal_service_pricing_is_derived_from_verified_read(
    price_minor: int,
    expected: str,
) -> None:
    outcome = _service_price_outcome(price_minor=price_minor)
    service = outcome.facts["service_catalog"]["service"]

    assert service["name"] == "PRP للبشرة"
    assert service["price"] == expected


def test_selected_laser_device_price_uses_matching_verified_device() -> None:
    operation = TurnOperation(
        type="pricing",
        entities=TurnEntities(
            service=EntityReference(text="ليزر إبط", ref="S1", candidate_refs=[]),
        ),
        selection=None,
        package_usage="unspecified",
        requested_service_details=["price"],
        execution_intent="informational",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog", parameters={"service_id": "service-1"})],
        response_goal="answer_price",
        facts={"service_id": "service-1", "device_key": "candela_gentle"},
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="service_catalog",
                ok=True,
                payload={
                    "service": {
                        "id": "service-1",
                        "name": "ليزر إبط",
                        "price_minor": 55_000,
                        "currency": "EGP",
                        "requires_laser_device": True,
                        "laser_devices": [
                            {
                                "device_key": "prime_lase",
                                "device_name": "Prime Lase",
                                "price_minor": 55_000,
                                "currency": "EGP",
                                "configured": True,
                            },
                            {
                                "device_key": "candela_gentle",
                                "device_name": "Candela Gentle",
                                "price_minor": 65_000,
                                "currency": "EGP",
                                "configured": True,
                            },
                        ],
                    }
                },
            )
        ]
    )

    outcome = build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=_semantic_context(),
        reads=reads,
    )
    service = outcome.facts["service_catalog"]["service"]

    assert service["selected_laser_device"] == {
        "device_name": "Candela Gentle",
        "price": "650.00 EGP",
    }
    assert "price" not in service


def test_multi_device_pricing_without_selection_exposes_all_verified_prices() -> None:
    operation = TurnOperation(
        type="pricing",
        entities=TurnEntities(
            service=EntityReference(text="ليزر إبط", ref="S1", candidate_refs=[]),
        ),
        selection=None,
        package_usage="unspecified",
        requested_service_details=["price"],
        execution_intent="informational",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog", parameters={"service_id": "service-1"})],
        response_goal="answer_price",
        facts={"service_id": "service-1"},
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="service_catalog",
                ok=True,
                payload={
                    "service": {
                        "id": "service-1",
                        "name": "ليزر إبط",
                        "price_minor": 55_000,
                        "currency": "EGP",
                        "requires_laser_device": True,
                        "laser_devices": [
                            {
                                "device_key": "prime_lase",
                                "device_name": "Prime Lase",
                                "price_minor": 55_000,
                                "currency": "EGP",
                            },
                            {
                                "device_key": "candela_gentle",
                                "device_name": "Candela Gentle",
                                "price_minor": 65_000,
                                "currency": "EGP",
                            },
                        ],
                    }
                },
            )
        ]
    )

    outcome = build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=_semantic_context(),
        reads=reads,
    )
    service = outcome.facts["service_catalog"]["service"]

    assert service["requires_laser_device"] is True
    assert service["laser_devices"] == [
        {"device_name": "Prime Lase", "price": "550.00 EGP"},
        {"device_name": "Candela Gentle", "price": "650.00 EGP"},
    ]
    assert "selected_laser_device" not in service
    assert "price" not in service


def test_single_configured_laser_device_can_supply_verified_price_without_extra_choice() -> None:
    operation = TurnOperation(
        type="pricing",
        entities=TurnEntities(
            service=EntityReference(text="ليزر خدمة واحدة", ref="S1", candidate_refs=[]),
        ),
        selection=None,
        package_usage="unspecified",
        requested_service_details=["price"],
        execution_intent="informational",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog", parameters={"service_id": "service-1"})],
        response_goal="answer_price",
        facts={"service_id": "service-1"},
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="service_catalog",
                ok=True,
                payload={
                    "service": {
                        "id": "service-1",
                        "name": "ليزر خدمة واحدة",
                        "price_minor": 50_000,
                        "currency": "EGP",
                        "requires_laser_device": True,
                        "laser_devices": [
                            {
                                "device_key": "prime_lase",
                                "device_name": "Prime Lase",
                                "price_minor": 55_000,
                                "currency": "EGP",
                                "configured": True,
                            }
                        ],
                    }
                },
            )
        ]
    )

    outcome = build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=_semantic_context(),
        reads=reads,
    )
    service = outcome.facts["service_catalog"]["service"]

    assert service["selected_laser_device"] == {
        "device_name": "Prime Lase",
        "price": "550.00 EGP",
    }
    assert "price" not in service


def test_compatibility_failure_reaches_responder_as_grounded_visible_fact() -> None:
    context = _semantic_context()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(text="ليزر إبط", ref="S1", candidate_refs=[]),
            doctor=EntityReference(text="مريم", ref="D1", candidate_refs=[]),
        ),
        selection=None,
        package_usage="unspecified",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="clarify",
        response_goal="clarification",
        clarification_field="doctor",
        facts={"service_id": "service-1", "doctor_id": None},
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=False,
                error_code="doctor_service_incompatible",
                payload={
                    "compatibility_failure": {
                        "dimension": "doctor",
                        "service_name": "ليزر إبط",
                        "requested_name": "مريم",
                        "compatible_options": ["سارة"],
                        "candidate_ids": ["doctor-2"],
                    }
                },
            )
        ]
    )
    outcome = build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=context,
        reads=reads,
    )
    visible = customer_visible_outcome(outcome)

    assert outcome.status == "needs_input"
    assert outcome.response_goal == "clarification"
    assert visible["facts"]["compatibility_failure"] == {
        "dimension": "doctor",
        "service_name": "ليزر إبط",
        "requested_name": "مريم",
        "compatible_options": ["سارة"],
    }
    assert "doctor-2" not in str(visible)


def test_selected_laser_device_without_verified_price_does_not_fall_back_to_other_devices() -> None:
    operation = TurnOperation(
        type="pricing",
        entities=TurnEntities(
            service=EntityReference(text="ليزر إبط", ref="S1", candidate_refs=[]),
        ),
        selection=None,
        package_usage="unspecified",
        requested_service_details=["price"],
        execution_intent="informational",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog", parameters={"service_id": "service-1"})],
        response_goal="answer_price",
        facts={"service_id": "service-1", "device_key": "candela_gentle"},
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="service_catalog",
                ok=True,
                payload={
                    "service": {
                        "id": "service-1",
                        "name": "ليزر إبط",
                        "price_minor": 55_000,
                        "currency": "EGP",
                        "requires_laser_device": True,
                        "laser_devices": [
                            {
                                "device_key": "prime_lase",
                                "device_name": "Prime Lase",
                                "price_minor": 55_000,
                                "currency": "EGP",
                                "configured": True,
                            },
                            {
                                "device_key": "candela_gentle",
                                "device_name": "Candela Gentle",
                                "price_minor": None,
                                "currency": "EGP",
                                "configured": False,
                            },
                        ],
                    }
                },
            )
        ]
    )

    outcome = build_step_outcome(
        step,
        turn=_turn(operation),
        semantic_context=_semantic_context(),
        reads=reads,
    )
    service = outcome.facts["service_catalog"]["service"]
    contract = build_customer_response_contract([outcome])

    assert service["requires_laser_device"] is True
    assert "selected_laser_device" not in service
    assert "laser_devices" not in service
    assert contract.units[0].commercial_truth is not None
    assert contract.units[0].commercial_truth.kind == "price_unavailable"
