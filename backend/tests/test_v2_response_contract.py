from __future__ import annotations

import pytest

from app.services.agent_v2.outcome import OutcomeChoice, TurnOutcome
from app.services.agent_v2.outcome_builder import customer_visible_outcome
from app.services.agent_v2.response_contract import build_customer_response_contract


def _fact_map(unit):
    return {fact.key: fact for fact in unit.facts}


def _availability():
    return {
        "service_name": "Laser Underarm",
        "availability_windows": [
            {
                "doctor_name": "Dr. Mary",
                "laser_device_name": "Candela Gentle",
                "start_local": "2026-10-01T18:00:00+03:00",
                "end_local": "2026-10-01T18:30:00+03:00",
            }
        ],
        "checked_dates": ["2026-10-01"],
        "available_option_count": 1,
        "price": "500.00 EGP",
    }


def test_booking_completion_projects_success_and_not_availability_semantics() -> None:
    outcome = TurnOutcome(
        status="completed",
        response_goal="booking_completed",
        facts={
            "service_name": "Laser Underarm",
            "doctor_name": "Dr. Mary",
            "device_name": "Candela Gentle",
            "start_local": "2026-10-01T18:00:00+03:00",
            "package_used": True,
            "availability": _availability(),
        },
        action_result={"ok": True},
    )

    unit = build_customer_response_contract([outcome]).units[0]
    facts = _fact_map(unit)

    assert unit.response_goal == "booking_completed"
    assert unit.action_truth is not None
    assert unit.action_truth.action == "booking"
    assert unit.action_truth.succeeded is True
    assert facts["service_name"].value == "Laser Underarm"
    assert facts["start_local"].value == "2026-10-01T18:00:00+03:00"
    assert facts["device_name"].value == "Candela Gentle"
    assert facts["package_used"].value is True
    assert "availability_windows" not in facts
    assert "checked_dates" not in facts
    assert "available_option_count" not in facts


def test_reschedule_completion_projects_success_not_availability() -> None:
    outcome = TurnOutcome(
        status="completed",
        response_goal="reschedule_completed",
        facts={"availability": _availability()},
        action_result={"ok": True, "start_local": "2026-10-01T18:00:00+03:00"},
    )

    unit = build_customer_response_contract([outcome]).units[0]
    facts = _fact_map(unit)

    assert unit.action_truth is not None
    assert unit.action_truth.action == "reschedule"
    assert unit.action_truth.succeeded is True
    assert facts["start_local"].value == "2026-10-01T18:00:00+03:00"
    assert "availability_windows" not in facts


def test_availability_keeps_verified_windows_as_required_complete_set() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={"availability": _availability()},
    )

    fact = _fact_map(build_customer_response_contract([outcome]).units[0])["availability_windows"]

    assert fact.requirement == "required"
    assert fact.complete_set is True
    assert fact.value == _availability()["availability_windows"]


@pytest.mark.parametrize(
    ("goal", "marker"),
    [
        ("requested_time_unavailable", "requested_time_unavailable"),
        ("no_availability", "no_availability"),
    ],
)
def test_unavailable_outcomes_have_explicit_required_truth(goal: str, marker: str) -> None:
    outcome = TurnOutcome(
        status="blocked",
        response_goal=goal,
        facts={"availability": {"checked_dates": ["2026-10-01"]}},
    )

    facts = _fact_map(build_customer_response_contract([outcome]).units[0])
    assert facts[marker].value is True
    assert facts[marker].requirement == "required"


def test_pure_price_preserves_service_price_currency() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "Hydrafacial",
                    "price": "1200.00 EGP",
                    "currency": "EGP",
                }
            }
        },
    )

    facts = _fact_map(build_customer_response_contract([outcome]).units[0])

    assert facts["service_name"].value == "Hydrafacial"
    assert facts["price"].value == "1200.00 EGP"
    assert facts["price"].requirement == "required"
    assert facts["currency"].value == "EGP"


def test_device_price_relationship_is_kept_as_one_required_complete_fact() -> None:
    pairs = [
        {"device_name": "Prime Lase", "price": "550.00 EGP"},
        {"device_name": "Candela Gentle", "price": "650.00 EGP"},
    ]
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "Laser Underarm",
                    "requires_laser_device": True,
                    "laser_devices": pairs,
                }
            }
        },
    )

    fact = _fact_map(build_customer_response_contract([outcome]).units[0])["device_price_options"]

    assert fact.requirement == "required"
    assert fact.complete_set is True
    assert fact.value == pairs


def test_doctor_list_is_a_complete_required_set() -> None:
    doctors = [{"name": "Dr. Mary"}, {"name": "Dr. Sara"}]
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_doctor",
        facts={"doctors": {"doctors": doctors}},
    )

    fact = _fact_map(build_customer_response_contract([outcome]).units[0])["doctors"]

    assert fact.value == doctors
    assert fact.requirement == "required"
    assert fact.complete_set is True


def test_device_choice_preserves_verified_pairing_without_refs() -> None:
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="ask_device_choice",
        facts={"needed": "device"},
        choices=[
            OutcomeChoice(
                ref="device:prime",
                label="Prime Lase",
                facts={"device_name": "Prime Lase", "price": "550.00 EGP", "device_id": "secret"},
            ),
            OutcomeChoice(
                ref="device:candela",
                label="Candela Gentle",
                facts={"device_name": "Candela Gentle", "price": "650.00 EGP"},
            ),
        ],
    )

    unit = build_customer_response_contract([outcome]).units[0]

    assert [choice.label for choice in unit.choices] == ["Prime Lase", "Candela Gentle"]
    assert _fact_map(unit.choices[0])["price"].value == "550.00 EGP"
    assert "secret" not in unit.model_dump_json()


def test_payment_info_keeps_policy_and_strips_financial_ledger() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_clinic_info",
        facts={
            "booking_requires_payment": False,
            "payment_execution_owner": "reception",
            "clinic_info": {"payment_methods": ["cash", "card"]},
            "customer_ledger": {
                "balance_due": "500.00 EGP",
                "transaction_id": "txn-secret",
                "patient_id": "patient-secret",
            },
        },
    )

    contract = build_customer_response_contract([outcome])
    payload = contract.model_dump_json()

    assert "payment_methods" in payload
    assert "booking_requires_payment" in payload
    assert "customer_ledger" not in payload
    assert "balance_due" not in payload
    assert "txn-secret" not in payload
    assert "patient-secret" not in payload


def test_package_offer_and_owned_package_remain_distinct() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="package_information",
        facts={
            "package_offers": {"offers": [{"name": "6 Sessions", "sessions": 6}]},
            "customer_packages": {"packages": [{"name": "Owned 3", "sessions_remaining": 3}]},
        },
    )

    facts = _fact_map(build_customer_response_contract([outcome]).units[0])

    assert facts["package_offers"].value != facts["customer_packages"].value
    assert "offers" in facts["package_offers"].value
    assert "packages" in facts["customer_packages"].value


def test_pulse_offer_owned_and_balance_remain_distinct() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="pulse_information",
        facts={
            "pulse_pack_offers": {"offers": [{"pulses_count": 1000, "price": "1500.00 EGP"}]},
            "pulse_packs": {"packs": [{"device_name": "Candela", "pulses_remaining": 1250}]},
            "pulse_balance": {"pulses_remaining": 1250},
        },
    )

    facts = _fact_map(build_customer_response_contract([outcome]).units[0])

    assert set(facts) == {"pulse_pack_offers", "pulse_packs", "pulse_balance"}
    assert facts["pulse_pack_offers"].value != facts["pulse_packs"].value


def test_clarification_and_handoff_are_representable() -> None:
    clarification = TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={"needed": "date", "reason": "missing"},
    )
    handoff = TurnOutcome(
        status="handoff",
        response_goal="handoff",
        facts={"category": "payment", "priority": "normal"},
    )

    contract = build_customer_response_contract([clarification, handoff])

    assert [unit.response_goal for unit in contract.units] == ["clarification", "handoff"]
    assert _fact_map(contract.units[1])["category"].requirement == "required"


def test_compound_turn_preserves_order_and_semantic_units() -> None:
    price = TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={"service_catalog": {"service": {"name": "Laser", "price": "500.00 EGP"}}},
    )
    availability = TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={"availability": _availability()},
    )

    contract = build_customer_response_contract([price, availability])

    assert [unit.response_goal for unit in contract.units] == [
        "answer_price",
        "present_availability",
    ]


def test_internal_ids_and_refs_are_removed_recursively() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_customer_profile",
        facts={
            "profile": {
                "name": "Customer",
                "appointment_id": "a-secret",
                "service_id": "s-secret",
                "doctor_id": "d-secret",
                "patient_id": "p-secret",
                "workspace_id": "w-secret",
                "package_id": "pkg-secret",
                "transaction_id": "txn-secret",
                "selected_ref": "R1",
                "candidate_refs": ["R1", "R2"],
            }
        },
    )

    payload = build_customer_response_contract([outcome]).model_dump_json()

    for secret in (
        "a-secret", "s-secret", "d-secret", "p-secret", "w-secret",
        "pkg-secret", "txn-secret", "R1", "R2",
    ):
        assert secret not in payload


def test_builder_is_deterministic() -> None:
    outcomes = [
        TurnOutcome(
            status="answered",
            response_goal="social_ack",
            facts={"message_type": "thanks"},
        )
    ]

    assert build_customer_response_contract(outcomes) == build_customer_response_contract(outcomes)


@pytest.mark.parametrize(
    ("goal", "status"),
    [
        ("booking_completed", "completed"),
        ("reschedule_completed", "completed"),
        ("cancellation_completed", "completed"),
        ("appointment_confirmed", "completed"),
        ("package_purchased", "completed"),
        ("pulse_pack_purchased", "completed"),
        ("follow_up_created", "completed"),
        ("marketing_updated", "completed"),
        ("active_task_cancelled", "answered"),
    ],
)
def test_terminal_outcome_coverage(goal: str, status: str) -> None:
    kwargs = {"facts": {"active_task_cancelled": True}} if goal == "active_task_cancelled" else {"action_result": {"ok": True}}
    outcome = TurnOutcome(status=status, response_goal=goal, **kwargs)

    unit = build_customer_response_contract([outcome]).units[0]

    assert unit.response_goal == goal
    if status == "completed":
        assert unit.action_truth is not None
        assert unit.action_truth.succeeded is True


@pytest.mark.parametrize(
    "goal",
    [
        "answer_service",
        "answer_clinic_info",
        "answer_customer_profile",
        "answer_customer_history",
        "clarification",
        "package_information",
        "pulse_information",
        "package_refund_quote",
        "handoff",
        "social_ack",
    ],
)
def test_non_choice_nonterminal_goal_coverage(goal: str) -> None:
    status = "handoff" if goal == "handoff" else "answered"
    outcome = TurnOutcome(status=status, response_goal=goal, facts={"text": "safe"})

    unit = build_customer_response_contract([outcome]).units[0]

    assert unit.response_goal == goal


@pytest.mark.parametrize(
    "goal",
    [
        "ask_service_choice",
        "ask_doctor_choice",
        "ask_device_choice",
        "ask_time_choice",
        "ask_appointment_choice",
        "ask_package_choice",
    ],
)
def test_choice_goal_coverage(goal: str) -> None:
    outcome = TurnOutcome(
        status="needs_input",
        response_goal=goal,
        facts={"needed": "choice"},
        choices=[OutcomeChoice(ref="safe-internal-ref", label="Verified option")],
    )

    unit = build_customer_response_contract([outcome]).units[0]

    assert unit.response_goal == goal
    assert [choice.label for choice in unit.choices] == ["Verified option"]
    assert "safe-internal-ref" not in unit.model_dump_json()


def test_shadow_projection_preserves_critical_visible_truth_without_new_business_facts() -> None:
    outcome = TurnOutcome(
        status="completed",
        response_goal="booking_completed",
        facts={
            "service_name": "Laser Underarm",
            "availability": _availability(),
        },
        action_result={
            "ok": True,
            "start_local": "2026-10-01T18:00:00+03:00",
            "appointment_id": "appointment-secret",
        },
    )

    visible = customer_visible_outcome(outcome)
    contract = build_customer_response_contract([outcome])
    payload = contract.model_dump_json()

    assert visible["response_goal"] == contract.units[0].response_goal
    assert visible["status"] == contract.units[0].status
    assert "Laser Underarm" in payload
    assert "2026-10-01T18:00:00+03:00" in payload
    assert "appointment-secret" not in payload
    assert "availability_windows" not in payload
