from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.v2 import responder
from app.agents.v2.responder import _build_responder_messages, compose_v2_customer_reply
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.service_info_composer import deterministic_service_info_reply
from app.agents.v2.turn_contract import (
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import build_step_outcome
from app.services.agent_v2.planner import PlannerContext, PlanStep, ReadRequest, plan_turn
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.response_contract import (
    build_customer_response_contract,
    is_pure_supported_service_contract,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _semantic_context():
    return build_semantic_context(
        {
            "services": [
                {"id": "service-a", "name": "Laser Underarm"},
                {"id": "service-b", "name": "Hydrafacial"},
            ],
            "doctors": [],
        }
    )


def _service_operation(
    *details: str,
    entity: EntityReference | None = None,
) -> TurnOperation:
    return TurnOperation(
        type="service_info",
        entities=TurnEntities(service=entity),
        requested_service_details=list(details),
        execution_intent="informational",
    )


def _step(*, service_id: str | None = "service-a") -> PlanStep:
    params = {"service_id": service_id} if service_id else {}
    return PlanStep(
        operation_index=0,
        operation_type="service_info",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog", parameters=params)],
        response_goal="answer_service",
        facts=params,
    )


def _service_read(
    *,
    description: str | None = "Clinic verified guidance.",
) -> ReadExecutionBundle:
    service = {
        "id": "service-a",
        "workspace_id": "workspace-internal",
        "name": "Laser Underarm",
        "category": "Laser",
        "description": description,
        "duration_minutes": 30,
        "price_minor": 50_000,
        "price": "500.00 EGP",
        "currency": "EGP",
        "requires_medical_review": False,
        "requires_laser_device": True,
        "doctor_ids": ["doctor-internal"],
        "laser_devices": [
            {
                "device_key": "candela",
                "device_name": "Candela Gentle",
                "price_minor": 65_000,
                "duration_minutes": 20,
                "currency": "EGP",
                "configured": True,
            },
            {
                "device_key": "prime",
                "device_name": "Prime Lase",
                "price_minor": 55_000,
                "duration_minutes": 25,
                "currency": "EGP",
                "configured": False,
            },
        ],
    }
    return ReadExecutionBundle(
        results=[ReadResult(kind="service_catalog", ok=True, payload={"service": service})]
    )


def _build_detail_outcome(
    *details: str,
    description: str | None = "Clinic verified guidance.",
) -> TurnOutcome:
    operation = _service_operation(
        *details,
        entity=EntityReference(text="Laser Underarm", ref="S1", candidate_refs=[]),
    )
    turn = TiaTurnUnderstanding(operations=[operation], safety_signals=[])
    return build_step_outcome(
        _step(),
        turn=turn,
        semantic_context=_semantic_context(),
        reads=_service_read(description=description),
    )


def _build_catalog_outcome(names: list[str]) -> TurnOutcome:
    operation = _service_operation()
    turn = TiaTurnUnderstanding(operations=[operation], safety_signals=[])
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="service_catalog",
                ok=True,
                payload={"services": [{"name": name} for name in names]},
            )
        ]
    )
    return build_step_outcome(
        _step(service_id=None),
        turn=turn,
        semantic_context=_semantic_context(),
        reads=reads,
    )


def test_service_information_safe_shapes_only_requested_customer_facts() -> None:
    outcome = _build_detail_outcome("description", "duration", "devices")
    catalog = outcome.facts["service_catalog"]
    assert isinstance(catalog, dict)
    assert catalog == {
        "service": {
            "name": "Laser Underarm",
            "clinic_explanation": "Clinic verified guidance.",
            "booking_duration_minutes": 30,
            "devices": [{"name": "Candela Gentle"}],
        },
        "requested_details": ["description", "devices", "duration"],
        "complete_set": False,
    }

    encoded = str(outcome.model_dump(mode="json"))
    for forbidden in (
        "500.00",
        "650.00",
        "550.00",
        "EGP",
        "price",
        "currency",
        "workspace-internal",
        "doctor-internal",
        "device_key",
        "configured",
        "requires_medical_review",
    ):
        assert forbidden not in encoded


def test_service_truth_preserves_exact_identity_description_duration_and_device() -> None:
    contract = build_customer_response_contract(
        [_build_detail_outcome("description", "duration", "devices")]
    )
    truth = contract.units[0].service_truth
    assert truth is not None
    assert truth.kind == "service_detail"
    assert truth.complete_set is False
    assert truth.requested_details == ("description", "devices", "duration")
    assert len(truth.services) == 1

    item = truth.services[0]
    assert item.name == "Laser Underarm"
    assert item.clinic_explanation == "Clinic verified guidance."
    assert item.booking_duration_minutes == 30
    assert item.devices == ("Candela Gentle",)
    assert is_pure_supported_service_contract(contract) is True


def test_pure_service_detail_renders_deterministically_without_commercial_claims() -> None:
    contract = build_customer_response_contract(
        [_build_detail_outcome("description", "duration", "devices")]
    )
    text = deterministic_service_info_reply(contract, arabic=False)

    assert "Laser Underarm" in text
    assert "Clinic verified guidance." in text
    assert "Recorded booking duration: 30 minutes." in text
    assert "Candela Gentle" in text
    assert "Prime Lase" not in text
    assert "EGP" not in text
    assert "price" not in text.casefold()
    assert "available" not in text.casefold()
    assert "doctor" not in text.casefold()


def test_missing_description_is_not_filled_from_model_knowledge() -> None:
    contract = build_customer_response_contract(
        [_build_detail_outcome("description", description=None)]
    )
    text = deterministic_service_info_reply(contract, arabic=False)

    assert "Laser Underarm" in text
    assert "no clinic-saved explanatory information" in text
    for invented in ("benefit", "safe", "suitable", "contraindication", "result"):
        assert invented not in text.casefold()


def test_service_catalog_is_a_complete_deduplicated_verified_set() -> None:
    contract = build_customer_response_contract(
        [_build_catalog_outcome(["Laser Underarm", "Hydrafacial", "Laser Underarm"])]
    )
    truth = contract.units[0].service_truth
    assert truth is not None
    assert truth.kind == "service_catalog"
    assert truth.complete_set is True
    assert [item.name for item in truth.services] == ["Laser Underarm", "Hydrafacial"]

    text = deterministic_service_info_reply(contract, arabic=False)
    assert text.count("Laser Underarm") == 1
    assert text.count("Hydrafacial") == 1


def test_empty_catalog_claim_is_scoped_to_current_clinic_catalog() -> None:
    contract = build_customer_response_contract([_build_catalog_outcome([])])
    text = deterministic_service_info_reply(contract, arabic=True)
    assert text == "مفيش خدمات مسجلة في كتالوج العيادة الحالي."
    assert "مواعيد" not in text
    assert "متاحة" not in text


def test_duration_is_rendered_as_recorded_booking_duration_not_treatment_guarantee() -> None:
    contract = build_customer_response_contract([_build_detail_outcome("duration")])
    text = deterministic_service_info_reply(contract, arabic=True)
    assert "مدة الحجز المسجلة للخدمة: 30 دقيقة." in text
    assert "مدة العلاج" not in text
    assert "مضمونة" not in text


def test_stale_assistant_service_wording_cannot_override_verified_truth() -> None:
    outcome = _build_detail_outcome("duration")
    text, label = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[
            AIMessage(content="الخدمة عندنا اسمها Hydrafacial ومدتها 90 دقيقة."),
            HumanMessage(content="طيب الخدمة المسجلة ومدتها إيه؟"),
        ],
        outcomes=[outcome],
    )
    assert label == "deterministic:service-information-contract"
    assert "Laser Underarm" in text
    assert "30 دقيقة" in text
    assert "Hydrafacial" not in text
    assert "90" not in text


def test_pure_service_path_bypasses_generic_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "_build_responder_messages",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic responder must not run")
        ),
    )
    text, label = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="الخدمة دي بتاخد قد إيه؟")],
        outcomes=[_build_detail_outcome("duration")],
    )
    assert label == "deterministic:service-information-contract"
    assert "30 دقيقة" in text


def test_mixed_generic_path_receives_only_safe_service_facts() -> None:
    service = _build_detail_outcome("devices")
    clinic = TurnOutcome(
        status="answered",
        response_goal="answer_clinic_info",
        facts={"clinic_info": {"clinic_name": "Tia Clinic"}},
    )
    messages = _build_responder_messages(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="الجهاز إيه واسم العيادة؟")],
        outcomes=[service, clinic],
    )
    outcome_message = next(
        message
        for message in messages
        if isinstance(message.content, str)
        and message.content.startswith("TURN_OUTCOMES")
    )
    content = str(outcome_message.content)
    assert "Candela Gentle" in content
    assert "Tia Clinic" in content
    for forbidden in (
        "650.00",
        "550.00",
        "EGP",
        "device_key",
        "duration_minutes",
        "configured",
        "workspace_id",
        "service_id",
    ):
        assert forbidden not in content


def _planner_context() -> PlannerContext:
    return PlannerContext(
        semantic_context=_semantic_context(),
        active_task=None,
        now=NOW,
    )


def test_unknown_explicit_service_fails_closed_to_clarification() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _service_operation(
                entity=EntityReference(
                    text="Unknown Treatment",
                    ref=None,
                    candidate_refs=[],
                )
            )
        ],
        safety_signals=[],
    )
    plan = plan_turn(turn, _planner_context())
    assert len(plan.steps) == 1
    assert plan.steps[0].disposition == "clarify"
    assert plan.steps[0].clarification_field == "service"
    assert plan.steps[0].reads == []


def test_ambiguous_service_keeps_canonical_choice_flow() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _service_operation(
                entity=EntityReference(
                    text="service",
                    ref=None,
                    candidate_refs=["S1", "S2"],
                )
            )
        ],
        safety_signals=[],
    )
    step = plan_turn(turn, _planner_context()).steps[0]
    assert step.disposition == "clarify"
    assert step.response_goal == "ask_service_choice"


def test_canonical_service_set_is_preserved_as_scoped_catalog_read() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _service_operation(
                entity=EntityReference(
                    text="الخدمات دي",
                    ref=None,
                    candidate_refs=["S1", "S2"],
                    candidate_mode="set",
                )
            )
        ],
        safety_signals=[],
    )
    step = plan_turn(turn, _planner_context()).steps[0]
    assert step.disposition == "read"
    assert step.reads[0].kind == "service_catalog"
    assert step.reads[0].parameters == {
        "service_ids": ["service-a", "service-b"]
    }


def test_medical_suitability_signal_never_reaches_service_information_responder() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _service_operation(
                "description",
                entity=EntityReference(
                    text="Laser Underarm",
                    ref="S1",
                    candidate_refs=[],
                ),
            )
        ],
        safety_signals=["medical"],
    )
    plan = plan_turn(turn, _planner_context())
    assert plan.steps == []
    assert plan.handoff_category == "medical"
    assert plan.handoff_priority == "high"


def test_price_outcome_is_not_claimed_by_service_contract() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {"name": "Laser Underarm", "price": "500.00 EGP"}
            }
        },
    )
    contract = build_customer_response_contract([outcome])
    assert contract.units[0].service_truth is None
    assert is_pure_supported_service_contract(contract) is False
