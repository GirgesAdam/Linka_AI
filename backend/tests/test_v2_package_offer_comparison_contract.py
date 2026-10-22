from __future__ import annotations

from datetime import UTC, datetime

from app.agents.v2.package_composer import deterministic_package_contract_reply
from app.agents.v2.semantic_context import SemanticContext, SemanticReferenceTarget
from app.agents.v2.turn_contract import (
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import _interpreter_system_prompt
from app.services.agent_v2.outcome_builder import build_step_outcome
from app.services.agent_v2.planner import PlannerContext, plan_turn
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.response_contract import build_customer_response_contract

NOW = datetime(2026, 10, 22, 3, 0, tzinfo=UTC)


def _semantic() -> SemanticContext:
    return SemanticContext(
        model_input={},
        reference_map={
            "S1": SemanticReferenceTarget(kind="service", canonical_id="svc-underarm"),
            "V1": SemanticReferenceTarget(kind="device", canonical_id="prime_lase"),
            "V2": SemanticReferenceTarget(kind="device", canonical_id="candela_gentle"),
            "V3": SemanticReferenceTarget(kind="device", canonical_id="soprano_titanium"),
        },
    )


def _context() -> PlannerContext:
    return PlannerContext(semantic_context=_semantic(), active_task=None, now=NOW)


def _offer_compare_operation(*, device_refs: list[str]) -> TurnOperation:
    return TurnOperation(
        type="package_info",
        entities=TurnEntities(
            service=EntityReference(text="Laser Underarm", ref="S1"),
            device=EntityReference(
                text="requested offer devices",
                candidate_refs=device_refs,
                candidate_mode="set",
            ),
            package_sessions=6,
        ),
        requested_package_details=["offers"],
        execution_intent="informational",
    )


def _offer(
    *,
    offer_id: str,
    device_key: str,
    device_name: str,
    price_minor: int,
) -> dict[str, object]:
    return {
        "id": offer_id,
        "workspace_id": "workspace-1",
        "service_id": "svc-underarm",
        "service_name": "Laser Underarm",
        "device_key": device_key,
        "device_name": device_name,
        "sessions_count": 6,
        "price_minor": price_minor,
        "currency": "EGP",
        "is_active": True,
        "standalone_session_price_minor": 65000,
        "savings_minor": 0,
    }


def _offer_reads(*offers: dict[str, object]) -> ReadExecutionBundle:
    return ReadExecutionBundle(
        results=[ReadResult(kind="package_offers", ok=True, payload={"offers": list(offers)})]
    )


def test_p1_two_available_offers_use_package_offers_and_render_canonical_prices() -> None:
    semantic = _semantic()
    turn = TiaTurnUnderstanding(
        operations=[_offer_compare_operation(device_refs=["V1", "V2"])]
    )
    plan = plan_turn(
        turn,
        PlannerContext(semantic_context=semantic, active_task=None, now=NOW),
    )

    step = plan.steps[0]
    assert step.operation_type == "package_info"
    assert step.response_goal == "package_information"
    assert [read.kind for read in step.reads] == ["package_offers"]
    assert step.reads[0].parameters["service_id"] == "svc-underarm"
    assert step.reads[0].parameters["device_keys"] == ["prime_lase", "candela_gentle"]
    assert step.reads[0].parameters["package_sessions"] == 6

    outcome = build_step_outcome(
        step,
        turn=turn,
        semantic_context=semantic,
        reads=_offer_reads(
            _offer(
                offer_id="offer-prime-6",
                device_key="prime_lase",
                device_name="Prime Lase",
                price_minor=295000,
            ),
            _offer(
                offer_id="offer-candela-6",
                device_key="candela_gentle",
                device_name="Candela Gentle",
                price_minor=350000,
            ),
            _offer(
                offer_id="offer-soprano-6",
                device_key="soprano_titanium",
                device_name="Soprano Titanium",
                price_minor=410000,
            ),
        ),
    )

    assert "customer_packages" not in outcome.facts
    offers = outcome.facts["package_offers"]["offers"]
    assert [offer["device_name"] for offer in offers] == ["Prime Lase", "Candela Gentle"]
    assert [offer["price"] for offer in offers] == ["2950.00 EGP", "3500.00 EGP"]
    assert outcome.facts["package_offers"]["requested_scope_complete"] is True

    contract = build_customer_response_contract([outcome])
    truth = contract.units[0].package_truth
    assert truth is not None
    assert truth.owned_requested is False
    assert truth.offers_requested is True
    assert truth.requested_offer_scope_complete is True
    assert [offer.price for offer in truth.package_offers] == ["2950.00 EGP", "3500.00 EGP"]
    reply = deterministic_package_contract_reply(contract, arabic=False)
    assert "Prime Lase" in reply
    assert "2950.00 EGP" in reply
    assert "Candela Gentle" in reply
    assert "3500.00 EGP" in reply
    assert "Soprano Titanium" not in reply


def test_p2_offer_comparison_does_not_require_owned_packages() -> None:
    semantic = _semantic()
    turn = TiaTurnUnderstanding(
        operations=[_offer_compare_operation(device_refs=["V1", "V2"])]
    )
    step = plan_turn(
        turn,
        PlannerContext(semantic_context=semantic, active_task=None, now=NOW),
    ).steps[0]

    assert all(read.kind != "customer_packages" for read in step.reads)
    outcome = build_step_outcome(
        step,
        turn=turn,
        semantic_context=semantic,
        reads=_offer_reads(
            _offer(
                offer_id="offer-prime-6",
                device_key="prime_lase",
                device_name="Prime Lase",
                price_minor=295000,
            ),
            _offer(
                offer_id="offer-candela-6",
                device_key="candela_gentle",
                device_name="Candela Gentle",
                price_minor=350000,
            ),
        ),
    )
    contract = build_customer_response_contract([outcome])
    reply = deterministic_package_contract_reply(contract, arabic=False)

    assert "owned" not in reply.casefold()
    assert "Prime Lase" in reply
    assert "Candela Gentle" in reply


def test_p3_owned_package_vs_standalone_keeps_existing_package_compare_contract() -> None:
    operation = TurnOperation(
        type="package_compare",
        entities=TurnEntities(service=EntityReference(text="Laser Underarm", ref="S1")),
        execution_intent="informational",
    )
    plan = plan_turn(TiaTurnUnderstanding(operations=[operation]), _context())

    step = plan.steps[0]
    assert step.operation_type == "package_compare"
    assert step.response_goal == "package_comparison"
    assert [read.kind for read in step.reads] == ["customer_packages", "service_catalog"]
    assert all(read.kind != "package_offers" for read in step.reads)


def test_p4_missing_requested_offer_reports_offer_scope_not_ownership() -> None:
    semantic = _semantic()
    turn = TiaTurnUnderstanding(
        operations=[_offer_compare_operation(device_refs=["V1", "V3"])]
    )
    step = plan_turn(
        turn,
        PlannerContext(semantic_context=semantic, active_task=None, now=NOW),
    ).steps[0]
    outcome = build_step_outcome(
        step,
        turn=turn,
        semantic_context=semantic,
        reads=_offer_reads(
            _offer(
                offer_id="offer-prime-6",
                device_key="prime_lase",
                device_name="Prime Lase",
                price_minor=295000,
            ),
            _offer(
                offer_id="offer-candela-6",
                device_key="candela_gentle",
                device_name="Candela Gentle",
                price_minor=350000,
            ),
        ),
    )

    assert outcome.facts["package_offers"]["requested_scope_complete"] is False
    assert [row["device_name"] for row in outcome.facts["package_offers"]["offers"]] == [
        "Prime Lase"
    ]
    contract = build_customer_response_contract([outcome])
    truth = contract.units[0].package_truth
    assert truth is not None
    assert truth.requested_offer_scope_complete is False
    reply = deterministic_package_contract_reply(contract, arabic=False)
    assert "Some requested package offers are not currently available" in reply
    assert "Prime Lase" in reply
    assert "owned" not in reply.casefold()


def test_p4_explicit_ungrounded_offer_device_fails_closed_to_clarification() -> None:
    operation = TurnOperation(
        type="package_info",
        entities=TurnEntities(
            service=EntityReference(text="Laser Underarm", ref="S1"),
            device=EntityReference(text="Unknown Device"),
            package_sessions=6,
        ),
        requested_package_details=["offers"],
        execution_intent="informational",
    )
    step = plan_turn(TiaTurnUnderstanding(operations=[operation]), _context()).steps[0]

    assert step.disposition == "clarify"
    assert step.clarification_field == "device"
    assert step.reads == []


def test_interpreter_contract_routes_clinic_offer_comparison_away_from_package_compare() -> None:
    prompt = _interpreter_system_prompt(timezone_name="Africa/Cairo", local_now=NOW)

    assert "owned-entitlement-vs-standalone comparison" in prompt
    assert "comparison between clinic package offers" in prompt
    assert "package_info with requested_package_details=[offers]" in prompt
    assert "do not silently drop it" in prompt
