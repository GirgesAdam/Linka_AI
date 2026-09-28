from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.llm_runtime import LLMProviderError
from app.agents.v2 import responder
from app.agents.v2.responder import _build_responder_messages, compose_v2_customer_reply
from app.services.agent_v2.outcome import TurnOutcome

NOW = datetime(2026, 9, 11, 16, 0, tzinfo=UTC)
INTERNAL_UUID = "123e4567-e89b-12d3-a456-426614174000"


def _price_outcome() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "id": INTERNAL_UUID,
                    "service_id": INTERNAL_UUID,
                    "name": "ليزر إبط",
                    "price_minor": 50_000,
                    "currency": "EGP",
                }
            }
        },
    )


def _doctor_outcome() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_doctor",
        facts={
            "doctors": {
                "doctors": [
                    {"name": "د. مريم"},
                    {"name": "د. سارة"},
                    {"name": "د. نور"},
                ]
            }
        },
    )


def test_responder_preserves_native_dialogue_roles_and_keeps_latest_customer_last() -> None:
    history = [
        HumanMessage(content="عايزة ليزر إبط"),
        AIMessage(content="تحبي تعرفي السعر ولا المواعيد؟"),
        HumanMessage(content="السعر كام؟"),
    ]

    messages = _build_responder_messages(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=history,
        outcomes=[_price_outcome()],
    )

    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert isinstance(messages[2], AIMessage)
    assert isinstance(messages[3], SystemMessage)
    assert isinstance(messages[4], HumanMessage)
    assert messages[4].content == "السعر كام؟"


def test_responder_payload_removes_internal_ids_and_formats_money_before_llm() -> None:
    messages = _build_responder_messages(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="ليزر الإبط بكام؟")],
        outcomes=[_price_outcome()],
    )
    payload = str(messages[-2].content)

    assert INTERNAL_UUID not in payload
    assert "service_id" not in payload
    assert "500.00 EGP" in payload
    assert "ليزر إبط" in payload


def test_responder_receives_explicit_device_price_pairs_for_availability() -> None:
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={
            "availability": {
                "service_name": "ليزر إزالة الشعر - إبط",
                "available_option_count": 2,
                "availability_windows": [
                    {"laser_device_name": "Candela Gentle", "start_time_24h": "12:00"},
                    {"laser_device_name": "Prime Lase", "start_time_24h": "12:00"},
                ],
                "laser_device_options": [
                    {"device_name": "Candela Gentle", "price": "650.00 EGP"},
                    {"device_name": "Prime Lase", "price": "550.00 EGP"},
                ],
            }
        },
    )
    messages = _build_responder_messages(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="اختار جهاز إيه؟")],
        outcomes=[outcome],
    )
    payload = str(messages[-2].content)

    assert '"device_name":"Candela Gentle","price":"650.00 EGP"' in payload
    assert '"device_name":"Prime Lase","price":"550.00 EGP"' in payload
    assert '"prices":' not in payload
    assert "laser_device_key" not in payload


def _device_price_clarification_outcome() -> TurnOutcome:
    return TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={
            "needed": "device",
            "availability": {
                "service_name": "ليزر إزالة الشعر - إبط",
                "available_option_count": 2,
                "availability_windows": [
                    {"laser_device_name": "Candela Gentle", "start_time_24h": "13:00"},
                    {"laser_device_name": "Prime Lase", "start_time_24h": "13:00"},
                ],
                "laser_device_options": [
                    {"device_name": "Candela Gentle", "price": "650.00 EGP"},
                    {"device_name": "Prime Lase", "price": "550.00 EGP"},
                ],
            },
        },
    )


def _mixed_device_price_guard_outcomes() -> list[TurnOutcome]:
    return [
        _device_price_clarification_outcome(),
        TurnOutcome(
            status="answered",
            response_goal="answer_service",
            facts={
                "service_catalog": {
                    "service": {
                        "name": "ليزر إزالة الشعر - إبط",
                        "description": "تفاصيل موثقة",
                    }
                }
            },
        ),
    ]


def test_device_price_guard_replaces_unbound_transliterated_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(responder, "model_label", lambda name: str(name))
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=responder.ResponderDraft(
                reply="اختاري كانديلا جنتل بـ650 جنيه أو برايم ليز بـ550 جنيه.",
                availability_claim="options_available",
            ),
            model_name="test-model",
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="اختار جهاز إيه؟")],
        outcomes=_mixed_device_price_guard_outcomes(),
    )

    assert source == "deterministic:device-price-guard:test-model"
    assert "Candela Gentle" in text and "650 جنيه" in text
    assert "Prime Lase" in text and "550 جنيه" in text
    assert "كانديلا" not in text
    assert "برايم" not in text


def test_device_price_guard_applies_after_availability_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(responder, "model_label", lambda name: str(name))
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=responder.ResponderDraft(
                reply="للأسف مفيش مواعيد متاحة.",
                availability_claim="no_availability",
            ),
            model_name="test-model",
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="اختار جهاز إيه؟")],
        outcomes=_mixed_device_price_guard_outcomes(),
    )

    assert source == "deterministic:device-price-guard:test-model"
    assert "Candela Gentle" in text and "650 جنيه" in text
    assert "Prime Lase" in text and "550 جنيه" in text
    assert "مفيش مواعيد" not in text


def test_device_price_guard_keeps_correct_canonical_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    natural = "اختاري Candela Gentle بسعر 650 جنيه أو Prime Lase بسعر 550 جنيه."
    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(responder, "model_label", lambda name: str(name))
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=responder.ResponderDraft(
                reply=natural,
                availability_claim="options_available",
            ),
            model_name="test-model",
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="اختار جهاز إيه؟")],
        outcomes=_mixed_device_price_guard_outcomes(),
    )

    assert text == natural
    assert source == "test-model"


def test_compound_outcomes_are_given_to_one_responder_call() -> None:
    outcomes = [
        _price_outcome(),
        TurnOutcome(
            status="answered",
            response_goal="present_availability",
            facts={
                "availability": {
                    "service_name": "ليزر إبط",
                    "checked_dates": ["2026-09-12"],
                    "availability_windows": [
                        {
                            "doctor_name": "د. مريم",
                            "start_time_24h": "18:00",
                            "end_time_24h": "20:00",
                        }
                    ],
                }
            },
        ),
    ]
    messages = _build_responder_messages(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="الإبط بكام وإيه المتاح بكرة؟")],
        outcomes=outcomes,
    )
    payload = str(messages[-2].content)

    assert '"response_goal":"answer_price"' in payload
    assert '"response_goal":"present_availability"' in payload
    assert len([message for message in messages if isinstance(message, HumanMessage)]) == 1


def test_pure_price_reply_is_deterministic_and_skips_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("pure verified pricing must not invoke the responder model")
        ),
    )

    text, model = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="الإبط بكام؟")],
        outcomes=[_price_outcome()],
    )

    assert text == "جلسة ليزر إبط سعرها 500 جنيه."
    assert model == "deterministic:price-device-contract"


def test_pure_price_reply_includes_verified_requested_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("verified price+duration must stay deterministic")),
    )
    outcome = _price_outcome()
    outcome.facts["service_catalog"]["service"]["duration_minutes"] = 60

    text, model = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="الإبط بكام وبيستغرق قد إيه؟")],
        outcomes=[outcome],
    )

    assert text == "جلسة ليزر إبط سعرها 500 جنيه، ومدتها 60 دقيقة."
    assert model == "deterministic:verified-price"


def test_pure_doctor_list_is_complete_once_and_skips_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_built():
        raise AssertionError("pure doctor discovery should not build the responder model")

    monkeypatch.setattr(responder, "build_realtime_composer_model", fail_if_built)

    text, model = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="مين الدكاترة اللي بيعملوا ليزر الإبط؟")],
        outcomes=[_doctor_outcome()],
    )

    assert model == "deterministic:doctor-contract"
    assert text.count("د. مريم") == 1
    assert text.count("د. سارة") == 1
    assert text.count("د. نور") == 1


def test_doctor_list_does_not_bypass_model_for_compound_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=responder.ResponderDraft(
                reply="الدكاترة د. مريم ود. سارة ود. نور، وسعر ليزر الإبط 500 جنيه.",
                availability_claim="not_applicable",
            ),
            model_name="test-model",
        ),
    )

    text, model = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="مين الدكاترة والسعر كام؟")],
        outcomes=[_doctor_outcome(), _price_outcome()],
    )

    assert model == "openai:test-model"
    assert "500 جنيه" in text
    assert text.count("د. مريم") == 1
    assert text.count("د. سارة") == 1
    assert text.count("د. نور") == 1


def test_empty_responder_output_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=responder.ResponderDraft(
                reply="   ",
                availability_claim="not_applicable",
            ),
            model_name="test-model",
        ),
    )

    with pytest.raises(LLMProviderError):
        compose_v2_customer_reply(
            clinic_name="Tia Clinic",
            timezone_name="Africa/Cairo",
            local_now=NOW,
            history=[HumanMessage(content="قولي تفاصيل الخدمة")],
            outcomes=[
                TurnOutcome(
                    status="answered",
                    response_goal="answer_service",
                    facts={
                        "service_catalog": {
                            "service": {
                                "name": "ليزر إبط",
                                "description": "تفاصيل موثقة",
                            }
                        }
                    },
                )
            ],
        )


def test_prp_verified_price_cannot_be_reinvented_by_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("pure pricing should be deterministic")),
    )
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "PRP للبشرة",
                    "price": "2000.00 EGP",
                }
            }
        },
    )

    text, model = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="PRP للبشرة بكام؟")],
        outcomes=[outcome],
    )

    assert model == "deterministic:price-device-contract"
    assert "2000" in text
    assert "2500" not in text


def test_verified_normal_price_changes_without_code_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("pure pricing should be deterministic")),
    )
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "خدمة أخرى",
                    "price": "2100.00 EGP",
                }
            }
        },
    )

    text, model = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="السعر كام؟")],
        outcomes=[outcome],
    )

    assert model == "deterministic:price-device-contract"
    assert "2100" in text
    assert "2000" not in text


def test_selected_candela_price_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("pure device pricing should be deterministic")),
    )
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "ليزر إبط",
                    "requires_laser_device": True,
                    "selected_laser_device": {
                        "device_name": "Candela Gentle",
                        "price": "650.00 EGP",
                    },
                }
            }
        },
    )

    text, model = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="طب Candela؟")],
        outcomes=[outcome],
    )

    assert model == "deterministic:price-device-contract"
    assert "Candela Gentle" in text
    assert "650" in text
    assert "550" not in text


def test_multiple_device_prices_are_presented_without_silent_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("pure device pricing should be deterministic")),
    )
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "ليزر إبط",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_name": "Prime Lase", "price": "550.00 EGP"},
                        {"device_name": "Candela Gentle", "price": "650.00 EGP"},
                    ],
                }
            }
        },
    )

    text, model = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="ليزر الإبط بكام؟")],
        outcomes=[outcome],
    )

    assert model == "deterministic:price-device-contract"
    assert "Prime Lase" in text and "550" in text
    assert "Candela Gentle" in text and "650" in text
    assert "أي جهاز" in text


def test_package_backed_booking_keeps_package_fact_and_scopes_pulse_guidance() -> None:
    outcome = TurnOutcome(
        status="completed",
        response_goal="booking_completed",
        facts={
            "package_usage": "use_existing",
            "availability": {
                "service_name": "PRP للبشرة",
                "availability_windows": [
                    {
                        "doctor_name": "د. مها",
                        "start_local": "2026-09-26T10:00:00+03:00",
                        "start_time_24h": "10:00",
                    }
                ],
                "available_option_count": 1,
            },
        },
        action_result={
            "ok": True,
            "write_kind": "booking",
            "status": "confirmed",
            "package_used": True,
            "package_name": "PRP package",
        },
    )

    messages = _build_responder_messages(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[
            HumanMessage(
                content="عندي باكدج وعندي Pulses كمان، احجزي الجلسة من الباكدج"
            )
        ],
        outcomes=[outcome],
    )

    system_prompt = str(messages[0].content)
    outcome_payload = str(messages[-2].content)

    assert '"package_used":true' in outcome_payload
    assert '"package_usage":"use_existing"' in outcome_payload
    assert "do not add Pulse" in system_prompt
    assert "another TURN_OUTCOME in this same turn explicitly carries a Pulse" in system_prompt


def test_doctor_compatibility_reply_is_deterministic_and_does_not_claim_time_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("LLM must not run")),
    )
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={
            "compatibility_failure": {
                "dimension": "doctor",
                "service_name": "PRP للبشرة",
                "requested_name": "أحمد محمود",
                "compatible_options": ["مها", "نور علي"],
            }
        },
    )

    reply, model = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="احجزيلي مع أحمد محمود")],
        outcomes=[outcome],
    )

    assert model == "deterministic:compatibility"
    assert "مش متوافق" in reply
    assert "مها" in reply and "نور علي" in reply
    assert "الساعة" not in reply
    assert "متاحين يوم" not in reply


def test_device_compatibility_reply_is_deterministic_and_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("LLM must not run")),
    )
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={
            "compatibility_failure": {
                "dimension": "device",
                "service_name": "ليزر إزالة الشعر - إبط",
                "requested_name": "Candela Gentle",
                "compatible_options": ["Prime Lase"],
            }
        },
    )

    reply, model = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="عايزاه على Candela Gentle")],
        outcomes=[outcome],
    )

    assert model == "deterministic:compatibility"
    assert "مش متوافق" in reply
    assert "Prime Lase" in reply
    assert "اختاري جهاز" in reply
