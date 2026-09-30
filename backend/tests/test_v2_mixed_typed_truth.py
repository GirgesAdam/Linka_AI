from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2 import responder
from app.agents.v2.responder import compose_v2_customer_reply
from app.services.agent_v2.outcome import TurnOutcome

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _service(name: str = "Hydrafacial") -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_service",
        facts={
            "service_catalog": {
                "service": {
                    "name": name,
                    "description": "Verified service description",
                }
            },
            "service_requested_details": ["description"],
        },
    )


def _price(amount: str = "500.00 EGP") -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "Hydrafacial",
                    "price": amount,
                    "currency": "EGP",
                }
            }
        },
    )


def _availability(start: str = "2026-10-01T18:00:00+03:00") -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={
            "availability": {
                "service_name": "Hydrafacial",
                "availability_windows": [
                    {
                        "doctor_name": "Dr Mary",
                        "start_local": start,
                        "end_local": "2026-10-01T18:30:00+03:00",
                    },
                    {
                        "doctor_name": "Dr Sara",
                        "start_local": "2026-10-01T19:00:00+03:00",
                        "end_local": "2026-10-01T19:30:00+03:00",
                    },
                ],
                "available_option_count": 2,
                "checked_dates": ["2026-10-01"],
            }
        },
    )


def _clinic() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_clinic_info",
        facts={
            "clinic_info": {
                "clinic_name": "Linka Clinic",
                "locations": [{"phone": "01012345678"}],
            },
            "clinic_requested_details": ["contact"],
        },
    )


def _pulse(remaining: int = 1000) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="pulse_information",
        facts={
            "pulse_requested_details": ["balance"],
            "pulse_balance": {
                "balances": [
                    {
                        "device_name": "Candela Gentle",
                        "pulses_remaining": remaining,
                        "active_pack_count": 1,
                    }
                ]
            },
        },
    )


def _appointment(start: str = "2026-10-02T17:00:00+03:00") -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_customer_history",
        facts={
            "appointments": {
                "visits": [
                    {
                        "status": "confirmed",
                        "start_local": start,
                        "end_local": "2026-10-02T17:30:00+03:00",
                        "doctor_name": "Dr Mary",
                        "services": [
                            {
                                "service_name": "Laser",
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


def _package() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="package_information",
        facts={
            "customer_packages": {
                "packages": [
                    {
                        "name": "Laser 6 Sessions",
                        "laser_device_name": "Candela Gentle",
                        "sessions_purchased": 6,
                        "sessions_remaining": 3,
                        "effective_status": "active",
                    }
                ]
            }
        },
    )


def _doctor() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_doctor",
        facts={
            "doctors": {
                "doctors": [
                    {"name": "Dr Mary", "specialization": "Dermatology"},
                    {"name": "Dr Sara", "specialization": "Dermatology"},
                ]
            }
        },
    )


def _terminal() -> TurnOutcome:
    return TurnOutcome(
        status="completed",
        response_goal="booking_completed",
        facts={
            "service_name": "Hydrafacial",
            "start_local": "2026-10-03T16:00:00+03:00",
            "doctor_name": "Dr Mary",
        },
        action_result={"ok": True},
    )


def _forbid_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("mixed typed truth must not invoke a response model")
        ),
    )
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("mixed typed truth must not invoke a response model")
        ),
    )


def _compose(message: str, outcomes: list[TurnOutcome]) -> tuple[str, str]:
    return compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content=message)],
        outcomes=outcomes,
    )


def test_price_corruption_probe_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_model(monkeypatch)
    text, source = _compose("الخدمة وسعرها؟", [_service(), _price("500.00 EGP")])
    assert "500" in text
    assert "900" not in text
    assert source == "deterministic:mixed-typed-contract"


def test_availability_corruption_probe_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_model(monkeypatch)
    text, source = _compose("رقم العيادة والمواعيد؟", [_clinic(), _availability()])
    assert "6 مساء" in text
    assert "7 مساء" in text
    assert "8 مساء" not in text
    assert "01012345678" in text
    assert source == "deterministic:mixed-typed-contract"


def test_pulse_and_appointment_corruption_probe_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)
    text, source = _compose("رصيدي وميعادي؟", [_pulse(1000), _appointment()])
    assert "1000" in text
    assert "700" not in text
    assert "5:00 مساء" in text
    assert "7:00 مساء" not in text
    assert source == "deterministic:mixed-typed-contract"


@pytest.mark.parametrize(
    ("outcomes", "required"),
    [
        ([_service(), _availability()], ("Hydrafacial", "6 مساء", "7 مساء")),
        ([_appointment(), _service("Laser")], ("5:00 مساء", "Laser")),
        ([_package(), _service("Laser")], ("3", "Laser 6 Sessions", "Laser")),
        ([_doctor(), _service()], ("Dr Mary", "Dr Sara", "Hydrafacial")),
        ([_terminal(), _service()], ("4 مساء", "Hydrafacial", "Dr Mary")),
    ],
)
def test_representative_typed_mixes_preserve_complete_verified_units(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[TurnOutcome],
    required: tuple[str, ...],
) -> None:
    _forbid_model(monkeypatch)
    text, source = _compose("هاتلي التفاصيل كلها", outcomes)
    for value in required:
        assert value in text
    assert source == "deterministic:mixed-typed-contract"


def test_low_risk_companion_cannot_reintroduce_free_form_fact_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    social = TurnOutcome(
        status="answered",
        response_goal="social_ack",
        facts={"acknowledgment": "thanks"},
    )
    model_calls = 0

    def corrupting_model(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        return SimpleNamespace(
            value=responder.ResponderDraft(
                reply="تمام، وبالمناسبة السعر 900 جنيه.",
                availability_claim="not_applicable",
            ),
            model_name="corrupting-model",
        )

    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(responder, "invoke_with_model_chain", corrupting_model)

    text, source = _compose("شكرًا، والسعر؟", [social, _price("500.00 EGP")])

    assert model_calls == 0
    assert "500" in text
    assert "900" not in text
    assert "تمام" in text
    assert source == "deterministic:mixed-typed-contract"


def test_single_hybrid_price_unit_preserves_verified_commercial_truth_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "PRP للبشرة",
                    "price": "2000.00 EGP",
                    "currency": "EGP",
                    "description": "Verified service description",
                }
            }
        },
    )

    text, source = _compose("الخدمة بتعمل إيه وسعرها كام؟", [outcome])

    assert "2000" in text
    assert "900" not in text
    assert source == "deterministic:verified-price"


def _pulse_unavailable() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="pulse_information",
        facts={"pulse_requested_details": ["balance"]},
    )


def _price_unavailable() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={"service_catalog": {"service": {"name": "Hydrafacial"}}},
    )


def _availability_unavailable() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={
            "availability": {
                "service_name": "Hydrafacial",
                "availability_windows": [],
                "available_option_count": 0,
                "checked_dates": ["2026-10-01"],
            }
        },
    )


def _service_without_typed_truth() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_service",
        facts={
            "service_requested_details": ["description"],
        },
    )


def test_missing_pulse_unit_is_covered_next_to_verified_appointment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)

    text, source = _compose(
        "رصيدي من الـPulses كام، وميعادي الجاي إمتى؟",
        [_pulse_unavailable(), _appointment()],
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "رصيد الـPulses الحالي مش ظاهر" in text
    assert "5:00 مساء" in text
    assert text.count("رصيد الـPulses الحالي مش ظاهر") == 1
    assert text.count("ميعادك الجاي") == 1


def test_unavailable_price_unit_does_not_disappear_next_to_service_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)

    text, source = _compose(
        "خدمة Hydrafacial بتعمل إيه وسعرها كام؟",
        [_service(), _price_unavailable()],
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "الخدمة: Hydrafacial." in text
    assert "Verified service description" in text
    assert "السعر المؤكد لخدمة Hydrafacial مش متاح" in text


def test_unavailable_availability_unit_gets_scoped_fallback_next_to_clinic_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)

    text, source = _compose(
        "رقم العيادة كام وإيه المواعيد المتاحة للهيدرافيشل؟",
        [_clinic(), _availability_unavailable()],
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "01012345678" in text
    assert "مش قادر أعرض مواعيد متاحة مؤكدة" in text


def test_multiple_missing_requested_units_get_bounded_deterministic_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)

    text, source = _compose(
        "رصيدي من الـPulses كام، وتفاصيل خدمة Hydrafacial إيه؟",
        [_pulse_unavailable(), _service_without_typed_truth()],
    )

    assert source == "deterministic:mixed-unit-completeness"
    assert "رصيد الـPulses الحالي مش ظاهر" in text
    assert "تفاصيل الخدمة المطلوبة مش ظاهرة" in text


def test_typed_missing_and_social_units_are_all_covered_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)
    social = TurnOutcome(
        status="answered",
        response_goal="social_ack",
        facts={"acknowledgment": "thanks"},
    )

    text, source = _compose(
        "شكرًا، ورصيدي من الـPulses كام وميعادي الجاي إمتى؟",
        [social, _pulse_unavailable(), _appointment()],
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "تمام." in text
    assert "رصيد الـPulses الحالي مش ظاهر" in text
    assert "5:00 مساء" in text
