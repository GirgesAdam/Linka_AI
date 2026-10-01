from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2 import responder
from app.agents.v2.choice_composer import (
    deduplicate_equivalent_choice_outcomes,
    deterministic_verified_choice_contract_reply,
    is_pure_supported_verified_choice_contract,
)
from app.agents.v2.responder import compose_v2_customer_reply
from app.services.agent_v2.outcome import OutcomeChoice, TurnOutcome
from app.services.agent_v2.response_contract import build_customer_response_contract

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _compose(outcomes: list[TurnOutcome], message: str = "اختار من الموجود") -> tuple[str, str]:
    return compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content=message)],
        outcomes=outcomes,
    )


def _forbid_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("verified choices must not invoke the generic responder")
        ),
    )
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verified choices must not invoke a response model")
        ),
    )


def _choice_outcome(goal: str, labels: list[str]) -> TurnOutcome:
    return TurnOutcome(
        status="needs_input",
        response_goal=goal,
        facts={"needed": goal.removeprefix("ask_").removesuffix("_choice")},
        choices=[
            OutcomeChoice(
                ref=f"internal-{index}",
                label=label,
                facts={
                    "service_id": f"secret-service-{index}",
                    "appointment_id": f"secret-appointment-{index}",
                },
            )
            for index, label in enumerate(labels, start=1)
        ],
    )


def test_service_choice_adversarial_replacement_is_structurally_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)
    text, source = _compose(
        [_choice_outcome("ask_service_choice", ["Hydrafacial", "PRP"])],
        "تقصد أنهي خدمة؟",
    )

    assert text.count("Hydrafacial") == 1
    assert text.count("PRP") == 1
    assert "Botox" not in text
    assert "Filler" not in text
    assert source == "deterministic:verified-choice-contract"


def test_complete_choice_set_is_rendered_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)
    text, _source = _compose(
        [_choice_outcome("ask_service_choice", ["A", "B", "C"])]
    )

    assert [line for line in text.splitlines() if line.startswith("- ")] == [
        "- A",
        "- B",
        "- C",
    ]


@pytest.mark.parametrize(
    ("goal", "labels"),
    [
        ("ask_device_choice", ["Candela Gentle", "Prime Lase"]),
        ("ask_time_choice", ["17:00", "18:00"]),
        ("ask_appointment_choice", ["Laser · Dr Mary · 17:00", "PRP · Dr Sara · 18:00"]),
        ("ask_package_choice", ["Laser 6 Sessions", "Laser 8 Sessions"]),
    ],
)
def test_nonfinancial_choice_families_render_verified_labels_without_model(
    monkeypatch: pytest.MonkeyPatch,
    goal: str,
    labels: list[str],
) -> None:
    _forbid_model(monkeypatch)
    text, source = _compose([_choice_outcome(goal, labels)])

    for label in labels:
        assert text.count(label) == 1
    assert source == "deterministic:verified-choice-contract"
    assert "secret-service" not in text
    assert "secret-appointment" not in text
    assert "internal-" not in text


def test_same_semantic_ambiguity_from_two_units_renders_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)
    outcomes = [
        _choice_outcome("ask_service_choice", ["PRP للبشرة", "PRP للشعر"]),
        _choice_outcome("ask_service_choice", ["PRP للبشرة", "PRP للشعر"]),
    ]

    text, source = _compose(outcomes, "سعر PRP والباكدج بتاعته؟")

    assert text.count("تقصد أنهي خدمة من دول؟") == 1
    assert text.count("PRP للبشرة") == 1
    assert text.count("PRP للشعر") == 1
    assert source == "deterministic:verified-choice-contract"


def test_independent_service_and_doctor_ambiguities_are_not_collapsed() -> None:
    service = _choice_outcome("ask_service_choice", ["A", "B"])
    doctor = _choice_outcome("ask_doctor_choice", ["د. مريم", "د. سارة"])

    deduped = deduplicate_equivalent_choice_outcomes([service, doctor])

    assert deduped == [service, doctor]


def test_appointment_choice_uses_human_datetime_not_iso(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="ask_appointment_choice",
        facts={"needed": "appointment"},
        choices=[
            OutcomeChoice(
                ref="appointment-1",
                label="PRP للبشرة · 2026-10-05T10:00:00+03:00",
                facts={
                    "appointment_id": "internal-id",
                    "service_name": "PRP للبشرة",
                    "doctor_name": "د. مريم",
                    "start_local": "2026-10-05T10:00:00+03:00",
                },
            )
        ],
    )

    text, _source = _compose([outcome], "قصدي أنهي معاد؟")

    assert "PRP للبشرة، يوم 5 أكتوبر 2026 الساعة 10 صباحًا، مع د. مريم" in text
    assert "2026-10-05T10:00:00+03:00" not in text


def test_duplicate_visible_labels_fail_safe_without_internal_identity() -> None:
    contract = build_customer_response_contract(
        [_choice_outcome("ask_service_choice", ["Hydrafacial", "Hydrafacial"])]
    )

    text = deterministic_verified_choice_contract_reply(contract, arabic=True)

    assert "Hydrafacial" not in text
    assert "internal-" not in text
    assert "تمييز إضافي" in text


def test_cross_unit_choices_remain_in_their_own_verified_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)
    outcomes = [
        _choice_outcome("ask_service_choice", ["Hydrafacial", "PRP"]),
        _choice_outcome(
            "ask_appointment_choice",
            ["Laser · Dr Mary · 17:00", "PRP · Dr Sara · 18:00"],
        ),
    ]

    text, source = _compose(outcomes)

    assert text.count("Hydrafacial") == 1
    assert text.count("Laser · Dr Mary · 17:00") == 1
    assert text.count("PRP") == 2
    assert source == "deterministic:verified-choice-contract"


def test_refund_package_choice_stays_outside_choice_hardening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="ask_package_choice",
        facts={"needed": "package", "available_quote_count": 2},
        choices=[
            OutcomeChoice(
                ref="package-choice-1",
                label="Package A",
                facts={"refund_amount": "1800.00 EGP"},
            ),
            OutcomeChoice(
                ref="package-choice-2",
                label="Package B",
                facts={"refund_amount": "2600.00 EGP"},
            ),
        ],
    )
    contract = build_customer_response_contract([outcome])

    assert is_pure_supported_verified_choice_contract(contract) is False

    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=responder.ResponderDraft(
                reply="اختار الباكدج المناسبة للاسترجاع.",
                availability_claim="not_applicable",
            ),
            model_name="financial-legacy-model",
        ),
    )
    monkeypatch.setattr(responder, "model_label", lambda name: str(name))

    text, source = _compose([outcome])

    assert text == "اختار الباكدج المناسبة للاسترجاع."
    assert source == "financial-legacy-model"


def test_reachable_ordinary_device_clarification_choices_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_model(monkeypatch)
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={"needed": "device"},
        choices=[
            OutcomeChoice(ref="V1", label="Candela Gentle"),
            OutcomeChoice(ref="V2", label="Prime Lase"),
        ],
    )

    text, source = _compose([outcome], "تقصد أنهي جهاز؟")

    assert text.count("Candela Gentle") == 1
    assert text.count("Prime Lase") == 1
    assert source == "deterministic:verified-choice-contract"
