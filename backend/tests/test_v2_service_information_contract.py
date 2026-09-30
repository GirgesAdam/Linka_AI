from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.v2 import responder
from app.agents.v2.responder import _build_responder_messages, compose_v2_customer_reply
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.service_info_composer import deterministic_service_contract_reply
from app.agents.v2.turn_contract import (
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import build_step_outcome
from app.services.agent_v2.planner import PlanStep, ReadRequest
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.response_contract import (
    build_customer_response_contract,
    is_pure_supported_service_contract,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _outcome(service: dict[str, object], *, requested: list[str]) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_service",
        facts={
            "service_catalog": {"service": service},
            "service_requested_details": requested,
        },
    )


def _list_outcome(names: list[str]) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_service",
        facts={
            "service_catalog": {"services": [{"name": name} for name in names]},
            "service_requested_details": [],
        },
    )


def test_exact_service_identity_and_description_are_backend_owned() -> None:
    contract = build_customer_response_contract([
        _outcome({"name": "Hydrafacial", "description": "Clinic-authored text."}, requested=["description"])
    ])
    truth = contract.units[0].service_truth
    assert truth is not None and truth.kind == "service_detail"
    assert truth.services[0].name == "Hydrafacial"
    assert truth.services[0].description == "Clinic-authored text."
    text = deterministic_service_contract_reply(contract, arabic=False)
    assert "Hydrafacial" in text
    assert "Clinic-authored text." in text


def test_missing_description_never_invokes_general_medical_knowledge() -> None:
    contract = build_customer_response_contract([_outcome({"name": "PRP"}, requested=["description"])])
    text = deterministic_service_contract_reply(contract, arabic=False)
    assert text == "Service: PRP. No additional clinic-provided description is stored for this service."
    for invented in ("collagen", "healing", "benefit", "safe", "suitable", "contraindication"):
        assert invented not in text.casefold()


def test_duration_preserves_customer_text_or_configured_appointment_minutes() -> None:
    preferred = build_customer_response_contract([
        _outcome({"name": "Laser", "customer_duration_text": "حوالي 20 دقيقة"}, requested=["duration"])
    ])
    assert "مدة الموعد المعروضة للعميل: حوالي 20 دقيقة." in deterministic_service_contract_reply(preferred, arabic=True)

    fallback = build_customer_response_contract([
        _outcome({"name": "Laser", "duration_minutes": 30}, requested=["duration"])
    ])
    text = deterministic_service_contract_reply(fallback, arabic=False)
    assert "Configured appointment duration: 30 minutes." in text
    assert "treatment" not in text.casefold()


def test_device_association_is_structural_and_contains_no_commercial_fields() -> None:
    contract = build_customer_response_contract([
        _outcome(
            {
                "name": "Laser Underarm",
                "laser_devices": [
                    {"device_name": "Candela Gentle"},
                    {"device_name": "Prime Lase"},
                ],
            },
            requested=["devices"],
        )
    ])
    truth = contract.units[0].service_truth
    assert truth is not None
    assert truth.services[0].device_names == ("Candela Gentle", "Prime Lase")
    assert truth.services[0].devices_complete_set is True
    assert "price" not in str(truth.model_dump(mode="json")).casefold()
    assert "currency" not in str(truth.model_dump(mode="json")).casefold()


def test_complete_service_list_renders_every_service_once() -> None:
    contract = build_customer_response_contract([_list_outcome(["Hydrafacial", "PRP", "Laser"])])
    truth = contract.units[0].service_truth
    assert truth is not None and truth.complete_set is True
    text = deterministic_service_contract_reply(contract, arabic=False)
    for name in ("Hydrafacial", "PRP", "Laser"):
        assert text.count(name) == 1


def test_duplicate_service_list_is_rejected_from_pure_contract() -> None:
    contract = build_customer_response_contract([_list_outcome(["PRP", "PRP"])])
    assert contract.units[0].service_truth is None
    assert is_pure_supported_service_contract(contract) is False


def test_service_device_outcome_shaping_removes_prices_keys_and_internal_ids() -> None:
    operation = TurnOperation(
        type="service_info",
        entities=TurnEntities(service=EntityReference(ref="S1")),
        requested_service_details=["devices"],
        execution_intent="informational",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="service_info",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog", parameters={"service_id": "service-1"})],
        response_goal="answer_service",
        facts={"service_id": "service-1"},
    )
    reads = ReadExecutionBundle(results=[ReadResult(
        kind="service_catalog",
        ok=True,
        payload={"service": {
            "id": "service-1",
            "workspace_id": "workspace-1",
            "name": "Laser Underarm",
            "price_minor": 50000,
            "currency": "EGP",
            "laser_devices": [
                {"device_key": "candela", "device_name": "Candela Gentle", "price_minor": 65000, "currency": "EGP", "configured": True},
                {"device_key": "prime", "device_name": "Prime Lase", "price_minor": 55000, "currency": "EGP", "configured": True},
            ],
        }},
    )])
    outcome = build_step_outcome(
        step,
        turn=TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        semantic_context=build_semantic_context({"services": [], "doctors": []}),
        reads=reads,
    )
    dumped = str(outcome.model_dump(mode="json"))
    assert outcome.facts["service_catalog"] == {
        "service": {
            "name": "Laser Underarm",
            "laser_devices": [
                {"device_name": "Candela Gentle"},
                {"device_name": "Prime Lase"},
            ],
        },
    }
    assert outcome.facts["service_requested_details"] == ["devices"]
    for forbidden in ("price", "currency", "device_key", "service_id", "workspace_id"):
        assert forbidden not in dumped


def test_pure_service_path_bypasses_generic_responder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        responder,
        "_build_responder_messages",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("generic responder must not run")),
    )
    text, label = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="خدمة الهيدرافيشل بتعمل إيه؟")],
        outcomes=[_outcome({"name": "Hydrafacial", "description": "وصف العيادة المعتمد."}, requested=["description"])],
    )
    assert label == "deterministic:service-information-contract"
    assert "Hydrafacial" in text and "وصف العيادة المعتمد." in text


def test_stale_assistant_service_fact_cannot_override_verified_truth() -> None:
    text, label = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[
            AIMessage(content="الخدمة القديمة Old Service مدتها 90 دقيقة."),
            HumanMessage(content="طب مدة الخدمة الحالية كام؟"),
        ],
        outcomes=[_outcome({"name": "Verified Service", "duration_minutes": 30}, requested=["duration"])],
    )
    assert label == "deterministic:service-information-contract"
    assert "Verified Service" in text and "30" in text
    assert "Old Service" not in text and "90" not in text


def test_mixed_legacy_path_receives_only_safe_service_facts() -> None:
    service = _outcome(
        {"name": "Laser", "laser_devices": [{"device_name": "Candela Gentle"}]},
        requested=["devices"],
    )
    other = TurnOutcome(status="answered", response_goal="answer_clinic_info", facts={"clinic_info": {"clinic_name": "Linka"}})
    messages = _build_responder_messages(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="الخدمة دي بتشتغل على أنهي أجهزة؟")],
        outcomes=[service, other],
    )
    outcome_message = next(
        message
        for message in messages
        if isinstance(message.content, str) and message.content.startswith("TURN_OUTCOMES")
    )
    content = str(outcome_message.content)
    assert "Candela Gentle" in content
    for forbidden in ("price_minor", "currency", "device_key", "service_id", "workspace_id"):
        assert forbidden not in content


def test_service_contract_never_owns_price_availability_or_doctor_claims() -> None:
    outcome = _outcome({"name": "Laser", "description": "Clinic text."}, requested=["description"])
    contract = build_customer_response_contract([outcome])
    dumped = str(contract.units[0].service_truth.model_dump(mode="json"))
    for forbidden in ("price", "currency", "availability", "doctor", "suitable", "contraindication"):
        assert forbidden not in dumped.casefold()


def test_non_service_info_answer_service_is_not_claimed_by_service_contract() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_service",
        facts={"service_catalog": {"service": {"name": "Laser"}}},
    )
    contract = build_customer_response_contract([outcome])
    assert contract.units[0].service_truth is None
    assert is_pure_supported_service_contract(contract) is False


def test_arabic_service_name_only_is_readable() -> None:
    contract = build_customer_response_contract([
        _outcome({"name": "PRP للبشرة"}, requested=[])
    ])

    text = deterministic_service_contract_reply(contract, arabic=True)

    assert text == "الخدمة: PRP للبشرة."
    assert "????" not in text


def test_arabic_service_description_and_missing_description_are_readable() -> None:
    described = build_customer_response_contract([
        _outcome(
            {"name": "PRP للبشرة", "description": "وصف معتمد من العيادة."},
            requested=["description"],
        )
    ])
    missing = build_customer_response_contract([
        _outcome({"name": "PRP للبشرة"}, requested=["description"])
    ])

    described_text = deterministic_service_contract_reply(described, arabic=True)
    missing_text = deterministic_service_contract_reply(missing, arabic=True)

    assert "الخدمة: PRP للبشرة." in described_text
    assert "وصف معتمد من العيادة." in described_text
    assert "لا يوجد وصف إضافي مقدم من العيادة" in missing_text
    assert "????" not in described_text
    assert "????" not in missing_text


def test_arabic_configured_service_duration_is_readable_and_not_treatment_claim() -> None:
    contract = build_customer_response_contract([
        _outcome(
            {"name": "PRP للبشرة", "duration_minutes": 45},
            requested=["duration"],
        )
    ])

    text = deterministic_service_contract_reply(contract, arabic=True)

    assert "الخدمة: PRP للبشرة." in text
    assert "مدة الموعد المحددة في النظام: 45 دقيقة." in text
    assert "مدة العلاج" not in text
    assert "????" not in text


def test_arabic_service_devices_and_complete_list_are_readable() -> None:
    devices_contract = build_customer_response_contract([
        _outcome(
            {
                "name": "ليزر إزالة الشعر",
                "laser_devices": [
                    {"device_name": "Candela Gentle"},
                    {"device_name": "Prime Lase"},
                ],
            },
            requested=["devices"],
        )
    ])
    list_contract = build_customer_response_contract([
        _list_outcome(["Hydrafacial", "PRP", "Laser"])
    ])

    devices_text = deterministic_service_contract_reply(devices_contract, arabic=True)
    list_text = deterministic_service_contract_reply(list_contract, arabic=True)

    assert "الأجهزة المرتبطة بالخدمة: Candela Gentle، Prime Lase." in devices_text
    assert "الخدمات الموجودة في كتالوج العيادة: Hydrafacial، PRP، Laser." in list_text
    assert "????" not in devices_text
    assert "????" not in list_text


def test_arabic_service_mixed_with_price_preserves_readability_and_exact_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("generic responder must not run")),
    )
    service = _outcome(
        {"name": "PRP للبشرة", "description": "وصف معتمد من العيادة."},
        requested=["description"],
    )
    price = TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "PRP للبشرة",
                    "price": "2000.00 EGP",
                    "currency": "EGP",
                }
            }
        },
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="جلسة PRP للبشرة بتعمل إيه وسعرها كام؟")],
        outcomes=[service, price],
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "الخدمة: PRP للبشرة." in text
    assert "وصف معتمد من العيادة." in text
    assert "2000" in text
    assert "????" not in text


def test_arabic_service_mixed_with_availability_is_readable_and_grounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("generic responder must not run")),
    )
    service = _outcome(
        {"name": "PRP للبشرة", "description": "وصف معتمد من العيادة."},
        requested=["description"],
    )
    availability = TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={
            "availability": {
                "service_name": "PRP للبشرة",
                "availability_windows": [
                    {
                        "doctor_name": "مها",
                        "start_local": "2026-10-02T14:00:00+03:00",
                        "end_local": "2026-10-02T15:00:00+03:00",
                    }
                ],
                "available_option_count": 1,
                "checked_dates": ["2026-10-02"],
            }
        },
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="جلسة PRP للبشرة بتعمل إيه وإمتى فيه ميعاد متاح؟")],
        outcomes=[service, availability],
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "الخدمة: PRP للبشرة." in text
    assert "وصف معتمد من العيادة." in text
    assert "مها" in text
    assert "2 مساء" in text
    assert "????" not in text


def test_arabic_appointment_mixed_with_service_is_readable_and_preserves_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("generic responder must not run")),
    )
    appointment = TurnOutcome(
        status="answered",
        response_goal="answer_customer_history",
        facts={
            "appointments": {
                "visits": [
                    {
                        "status": "confirmed",
                        "start_local": "2026-10-01T13:30:00+03:00",
                        "end_local": "2026-10-01T14:30:00+03:00",
                        "doctor_name": "يوسف سمير",
                        "services": [
                            {
                                "service_name": "ليزر إزالة الشعر - جسم كامل سيدات",
                                "laser_device_name": "Candela Gentle",
                            }
                        ],
                    }
                ],
                "visit_count": 1,
                "presentation_unit": "visit",
                "complete_set": True,
            }
        },
    )
    service = _outcome(
        {"name": "PRP للبشرة", "duration_minutes": 45},
        requested=["duration"],
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="ميعادي الجاي إمتى ومدة جلسة PRP للبشرة قد إيه؟")],
        outcomes=[appointment, service],
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "يوسف سمير" in text
    assert "Candela Gentle" in text
    assert "الخدمة: PRP للبشرة." in text
    assert "مدة الموعد المحددة في النظام: 45 دقيقة." in text
    assert "????" not in text
