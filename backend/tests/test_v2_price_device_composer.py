from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.agents.llm_runtime import LLMProviderError
from app.agents.structured_output import StructuredOutputError
from app.agents.v2 import price_device_composer, responder
from app.agents.v2.price_device_composer import (
    PriceDeviceComposerDraft,
    PriceDeviceComposerUnitDraft,
    PriceDeviceComposerValidationError,
    _build_price_device_composer_messages,
    deterministic_price_device_fallback,
    validate_price_device_composer_draft,
)
from app.agents.v2.responder import compose_v2_customer_reply
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.response_contract import (
    CustomerResponseContract,
    build_customer_response_contract,
    is_pure_supported_price_device_contract,
)

NOW = datetime(2026, 9, 28, 16, 0, tzinfo=UTC)


def _base_price(
    *,
    service: str = "Hydrafacial",
    price: str = "1200.00 EGP",
) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": service,
                    "price": price,
                    "currency": "EGP",
                }
            }
        },
    )


def _selected_device_price(
    *,
    service: str = "Laser Underarm",
    device: str = "Candela Gentle",
    price: str = "650.00 EGP",
) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": service,
                    "requires_laser_device": True,
                    "selected_laser_device": {
                        "device_name": device,
                        "price": price,
                    },
                }
            }
        },
    )


def _multi_device_price() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "Laser Underarm",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {
                            "device_name": "Prime Lase",
                            "price": "550.00 EGP",
                        },
                        {
                            "device_name": "Candela Gentle",
                            "price": "650.00 EGP",
                        },
                    ],
                }
            }
        },
    )


def _package_price(*, two_devices: bool = False) -> TurnOutcome:
    offers = [
        {
            "service_name": "Laser Underarm",
            "device_name": "Prime Lase",
            "sessions_count": 6,
            "price": "3000.00 EGP",
            "currency": "EGP",
        }
    ]
    if two_devices:
        offers.append(
            {
                "service_name": "Laser Underarm",
                "device_name": "Candela Gentle",
                "sessions_count": 6,
                "price": "3500.00 EGP",
                "currency": "EGP",
            }
        )
    return TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={"package_offers": {"offers": offers}},
    )


def _device_clarification() -> TurnOutcome:
    return TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={
            "needed": "device",
            "availability": {
                "service_name": "Laser Underarm",
                "laser_device_options": [
                    {
                        "device_name": "Candela Gentle",
                        "price": "650.00 EGP",
                    },
                    {
                        "device_name": "Prime Lase",
                        "price": "550.00 EGP",
                    },
                ],
            },
        },
    )


def _draft(contract: CustomerResponseContract) -> PriceDeviceComposerDraft:
    units = []
    for index, unit in enumerate(contract.units):
        truth = unit.commercial_truth
        assert truth is not None
        units.append(
            PriceDeviceComposerUnitDraft(
                unit_index=index,
                commercial_ref="unit_commercial",
                style="warm",
                option_refs=[
                    f"unit_{index}_price_option_{option_index}"
                    for option_index, _option in enumerate(truth.options)
                ],
                presentation="list" if len(truth.options) > 1 else "sentence",
                transition="sentence" if index == 0 else "and",
            )
        )
    return PriceDeviceComposerDraft(units=units)


def test_exact_base_price_is_typed_and_backend_owned() -> None:
    contract = build_customer_response_contract([_base_price()])
    truth = contract.units[0].commercial_truth

    assert is_pure_supported_price_device_contract(contract) is True
    assert truth is not None
    assert truth.kind == "service_base_price"
    assert truth.complete_set is False
    assert len(truth.options) == 1
    option = truth.options[0]
    assert option.qualifier == "base"
    assert option.service_name == "Hydrafacial"
    assert option.device_name is None
    assert option.amount == "1200.00"
    assert option.currency == "EGP"


def test_explicit_device_price_is_bound_to_requested_device() -> None:
    contract = build_customer_response_contract([_selected_device_price()])
    truth = contract.units[0].commercial_truth

    assert truth is not None
    assert truth.kind == "service_device_price"
    option = truth.options[0]
    assert option.qualifier == "device"
    assert option.device_name == "Candela Gentle"
    assert option.amount == "650.00"
    assert option.currency == "EGP"


def test_multi_device_prices_are_structural_complete_bindings() -> None:
    contract = build_customer_response_contract([_multi_device_price()])
    truth = contract.units[0].commercial_truth

    assert truth is not None
    assert truth.kind == "service_device_price_options"
    assert truth.complete_set is True
    assert [
        (option.device_name, option.amount, option.currency)
        for option in truth.options
    ] == [
        ("Prime Lase", "550.00", "EGP"),
        ("Candela Gentle", "650.00", "EGP"),
    ]


def test_base_price_is_not_relabelled_as_device_price() -> None:
    base = build_customer_response_contract([_base_price()]).units[0].commercial_truth
    device = build_customer_response_contract(
        [_selected_device_price()]
    ).units[0].commercial_truth

    assert base is not None and device is not None
    assert base.options[0].qualifier == "base"
    assert base.options[0].device_name is None
    assert device.options[0].qualifier == "device"
    assert device.options[0].device_name == "Candela Gentle"


def test_package_price_keeps_service_device_sessions_amount_currency_together() -> None:
    contract = build_customer_response_contract([_package_price(two_devices=True)])
    truth = contract.units[0].commercial_truth

    assert truth is not None
    assert truth.kind == "package_price_options"
    assert truth.complete_set is True
    assert [
        (
            option.qualifier,
            option.service_name,
            option.device_name,
            option.sessions_count,
            option.amount,
            option.currency,
        )
        for option in truth.options
    ] == [
        (
            "package",
            "Laser Underarm",
            "Prime Lase",
            6,
            "3000.00",
            "EGP",
        ),
        (
            "package",
            "Laser Underarm",
            "Candela Gentle",
            6,
            "3500.00",
            "EGP",
        ),
    ]


def test_device_clarification_price_pairs_become_typed_commercial_truth() -> None:
    contract = build_customer_response_contract([_device_clarification()])
    truth = contract.units[0].commercial_truth

    assert is_pure_supported_price_device_contract(contract) is True
    assert truth is not None
    assert truth.kind == "device_price_clarification"
    assert truth.complete_set is True
    assert [(item.device_name, item.amount) for item in truth.options] == [
        ("Candela Gentle", "650.00"),
        ("Prime Lase", "550.00"),
    ]


def test_device_price_conflict_stays_out_of_new_path() -> None:
    outcome = _device_clarification()
    outcome.facts["availability"]["device_price_conflicts"] = ["Candela Gentle"]

    contract = build_customer_response_contract([outcome])

    assert contract.units[0].commercial_truth is None
    assert is_pure_supported_price_device_contract(contract) is False


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "duplicate", "missing", "cross_unit", "wrong_unit"],
)
def test_symbolic_price_option_validation_rejects_invalid_refs(
    mutation: str,
) -> None:
    contract = build_customer_response_contract(
        [_multi_device_price(), _selected_device_price()]
    )
    draft = _draft(contract)

    if mutation == "unknown":
        draft.units[0].option_refs = ["unit_0_price_option_99"]
    elif mutation == "duplicate":
        draft.units[0].option_refs = [
            "unit_0_price_option_0",
            "unit_0_price_option_0",
        ]
    elif mutation == "missing":
        draft.units[0].option_refs = ["unit_0_price_option_0"]
    elif mutation == "cross_unit":
        draft.units[0].option_refs = [
            "unit_1_price_option_0",
            "unit_0_price_option_1",
        ]
    else:
        draft.units[0].unit_index = 1

    with pytest.raises(PriceDeviceComposerValidationError):
        validate_price_device_composer_draft(contract, draft)


def test_composer_model_input_contains_refs_but_no_commercial_values() -> None:
    contract = build_customer_response_contract(
        [_package_price(two_devices=True)]
    )
    messages = _build_price_device_composer_messages(
        history=[
            HumanMessage(
                content=(
                    "باكدج 6 جلسات Laser Underarm على Candela Gentle "
                    "بـ3500 جنيه؟"
                )
            )
        ],
        contract=contract,
    )
    payload = "\n".join(str(message.content) for message in messages)

    assert "unit_0_price_option_0" in payload
    assert "unit_0_price_option_1" in payload
    for forbidden in (
        "Laser Underarm",
        "Candela Gentle",
        "Prime Lase",
        "3000",
        "3500",
        "EGP",
        "6 جلسات",
    ):
        assert forbidden not in payload


def test_exact_service_price_stays_zero_llm_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        price_device_composer,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("exact service price must not add an LLM call")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="الهيدرافيشل بكام؟")],
        outcomes=[_base_price()],
    )

    assert source == "deterministic:price-device-contract"
    assert text == "جلسة Hydrafacial سعرها 1200 جنيه."


def test_booking_price_confirmation_mentions_verified_availability_not_unverified_appointment() -> None:
    outcome = _selected_device_price()
    outcome.facts.update(
        {
            "booking_next_field": "booking",
            "exact_time_requested": True,
            "availability": {
                "available_option_count": 1,
                "availability_windows": [{"doctor_name": "مريم"}],
            },
        }
    )
    contract = build_customer_response_contract([outcome])

    text = deterministic_price_device_fallback(contract, arabic=True)

    assert "الوقت المطلوب متاح مع د. مريم" in text
    assert "تحب أحجز؟" in text
    assert "الموعد ده" not in text


def test_multi_device_service_price_stays_zero_llm_calls_and_not_swapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        price_device_composer,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("verified service device prices must stay deterministic")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="سعر الليزر كام حسب الجهاز؟")],
        outcomes=[_multi_device_price()],
    )

    assert source == "deterministic:price-device-contract"
    assert "Prime Lase — 550 جنيه" in text
    assert "Candela Gentle — 650 جنيه" in text
    assert "Prime Lase — 650 جنيه" not in text
    assert "Candela Gentle — 550 جنيه" not in text


def test_package_price_model_selects_refs_only_backend_resolves_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _package_price(two_devices=True)
    contract = build_customer_response_contract([outcome])
    draft = _draft(contract)

    monkeypatch.setattr(
        price_device_composer,
        "build_realtime_composer_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        price_device_composer,
        "model_label",
        lambda name: str(name),
    )
    monkeypatch.setattr(
        price_device_composer,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=draft,
            model_name="test-model",
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="باكدج الست جلسات بكام؟")],
        outcomes=[outcome],
    )

    assert source == "price-device-contract:test-model"
    assert "Prime Lase" in text and "3000 جنيه" in text
    assert "Candela Gentle" in text and "3500 جنيه" in text
    assert "Prime Lase" in text and "3500 جنيه" not in text.split("Prime Lase", 1)[1].split("\n", 1)[0]


def test_device_clarification_new_path_bypasses_legacy_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _device_clarification()
    contract = build_customer_response_contract([outcome])
    draft = _draft(contract)
    monkeypatch.setattr(
        price_device_composer,
        "build_realtime_composer_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        price_device_composer,
        "model_label",
        lambda name: str(name),
    )
    monkeypatch.setattr(
        price_device_composer,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(value=draft, model_name="test-model"),
    )
    monkeypatch.setattr(
        responder,
        "_deterministic_device_price_guard_reply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pure migrated device-price path must bypass legacy guard")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="اختار جهاز إيه؟")],
        outcomes=[outcome],
    )

    assert source == "price-device-contract:test-model"
    assert "Candela Gentle" in text
    assert "Prime Lase" in text


def test_invalid_model_refs_use_same_contract_deterministic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _package_price(two_devices=True)
    contract = build_customer_response_contract([outcome])
    bad = _draft(contract)
    bad.units[0].option_refs = ["unit_0_price_option_0"]

    monkeypatch.setattr(
        price_device_composer,
        "build_realtime_composer_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        price_device_composer,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(value=bad, model_name="test-model"),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="الباكدج بكام؟")],
        outcomes=[outcome],
    )

    assert source == "deterministic:price-device-contract-fallback"
    assert "Prime Lase" in text and "3000 جنيه" in text
    assert "Candela Gentle" in text and "3500 جنيه" in text


def test_provider_failure_uses_same_contract_deterministic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _device_clarification()
    monkeypatch.setattr(
        price_device_composer,
        "build_realtime_composer_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        price_device_composer,
        "invoke_with_model_chain",
        lambda **_kwargs: (_ for _ in ()).throw(
            LLMProviderError("provider down", retryable=False)
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="اختار جهاز إيه؟")],
        outcomes=[outcome],
    )

    assert source == "deterministic:price-device-contract-fallback"
    assert "Candela Gentle" in text and "650 جنيه" in text
    assert "Prime Lase" in text and "550 جنيه" in text



def test_mixed_price_and_unsupported_family_preserves_verified_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [
        _base_price(),
        TurnOutcome(
            status="answered",
            response_goal="answer_service",
            facts={
                "service_catalog": {
                    "service": {
                        "name": "Hydrafacial",
                        "description": "وصف موثق",
                    }
                }
            },
        ),
    ]
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("mixed typed price must not invoke the generic model")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="السعر والتفاصيل؟")],
        outcomes=outcomes,
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "1200" in text

def test_deterministic_fallback_preserves_all_required_price_options() -> None:
    contract = build_customer_response_contract([_multi_device_price()])
    text = deterministic_price_device_fallback(contract, arabic=True)

    assert text.count("Prime Lase") == 1
    assert text.count("Candela Gentle") == 1
    assert "550 جنيه" in text
    assert "650 جنيه" in text


def test_structured_output_failure_uses_same_contract_deterministic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _device_clarification()
    monkeypatch.setattr(
        price_device_composer,
        "build_realtime_composer_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        price_device_composer,
        "invoke_with_model_chain",
        lambda **_kwargs: (_ for _ in ()).throw(
            StructuredOutputError("malformed structured output")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="اختار جهاز إيه؟")],
        outcomes=[outcome],
    )

    assert source == "deterministic:price-device-contract-fallback"
    assert "Candela Gentle" in text and "650 جنيه" in text
    assert "Prime Lase" in text and "550 جنيه" in text
