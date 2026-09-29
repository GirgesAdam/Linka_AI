from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2 import responder
from app.agents.v2.doctor_composer import deterministic_doctor_contract_reply
from app.agents.v2.responder import compose_v2_customer_reply
from app.services.agent_v2.outcome import OutcomeChoice, TurnOutcome
from app.services.agent_v2.response_contract import (
    build_customer_response_contract,
    is_pure_supported_doctor_contract,
)

NOW = datetime.fromisoformat("2026-09-28T20:00:00+03:00")


def _answer(rows: list[dict[str, object]]) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_doctor",
        facts={"doctors": {"doctors": rows}},
    )


def _choice(*names: str) -> TurnOutcome:
    return TurnOutcome(
        status="needs_input",
        response_goal="ask_doctor_choice",
        facts={"needed": "doctor"},
        choices=[
            OutcomeChoice(ref=f"D{index}", label=name)
            for index, name in enumerate(names, start=1)
        ],
    )


def test_doctor_truth_keeps_verified_identity_and_specialization_only() -> None:
    contract = build_customer_response_contract(
        [_answer([{
            "name": "د. مريم",
            "specialization": "Dermatology",
            "working_hours": [{"weekday": 1, "start": "10:00", "end": "18:00"}],
            "doctor_id": "secret",
        }])]
    )
    truth = contract.units[0].doctor_truth

    assert is_pure_supported_doctor_contract(contract) is True
    assert truth is not None
    assert truth.kind == "doctor_result_set"
    assert truth.complete_set is True
    assert [(item.name, item.specialization) for item in truth.options] == [
        ("د. مريم", "Dermatology")
    ]
    assert "secret" not in contract.model_dump_json()


def test_complete_doctor_result_set_is_rendered_once_without_invention() -> None:
    contract = build_customer_response_contract(
        [_answer([
            {"name": "د. مريم", "specialization": "Dermatology"},
            {"name": "د. سارة", "specialization": "Laser"},
            {"name": "د. نور"},
        ])]
    )
    text = deterministic_doctor_contract_reply(contract, arabic=True)

    for name in ("د. مريم", "د. سارة", "د. نور"):
        assert text.count(name) == 1
    assert "Dermatology" in text
    assert "Laser" in text
    assert "د. خالد" not in text


def test_explicit_known_doctor_cannot_turn_into_another_identity() -> None:
    contract = build_customer_response_contract(
        [_answer([{"name": "د. مريم", "specialization": "Dermatology"}])]
    )
    text = deterministic_doctor_contract_reply(contract, arabic=True)

    assert "د. مريم" in text
    assert "د. سارة" not in text
    assert "د. خالد" not in text


def test_empty_verified_doctor_result_fails_closed_without_invention() -> None:
    contract = build_customer_response_contract([_answer([])])

    assert is_pure_supported_doctor_contract(contract) is True
    text = deterministic_doctor_contract_reply(contract, arabic=True)
    assert "مش لاقية دكاترة مطابقين" in text
    assert "د." not in text


def test_doctor_choice_preserves_complete_verified_choices() -> None:
    contract = build_customer_response_contract(
        [_choice("د. مريم", "د. سارة", "د. نور")]
    )
    truth = contract.units[0].doctor_truth

    assert is_pure_supported_doctor_contract(contract) is True
    assert truth is not None
    assert truth.kind == "doctor_choice"
    assert [item.name for item in truth.options] == [
        "د. مريم", "د. سارة", "د. نور"
    ]

    text = deterministic_doctor_contract_reply(contract, arabic=True)
    for name in ("د. مريم", "د. سارة", "د. نور"):
        assert text.count(name) == 1


def test_duplicate_visible_identity_fails_closed_without_fake_disambiguation() -> None:
    contract = build_customer_response_contract(
        [_answer([
            {"name": "د. مريم", "specialization": "Dermatology"},
            {"name": "د. مريم", "specialization": "Dermatology"},
        ])]
    )
    text = deterministic_doctor_contract_reply(contract, arabic=True)

    assert "بنفس الاسم والبيانات الظاهرة" in text
    assert "تمييز إضافي" in text


def test_best_doctor_question_does_not_create_ranking_or_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("pure doctor response must stay deterministic")
        ),
    )
    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="مين أحسن دكتور؟")],
        outcomes=[_answer([
            {"name": "د. مريم", "specialization": "Dermatology"},
            {"name": "د. سارة", "specialization": "Laser"},
        ])],
    )

    assert source == "deterministic:doctor-contract"
    assert "د. مريم" in text and "د. سارة" in text
    for unsupported in ("أحسن", "أفضل", "أنسب"):
        assert unsupported not in text


def test_pure_doctor_choice_bypasses_generic_model_and_legacy_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("model must be unreachable")),
    )
    monkeypatch.setattr(
        responder,
        "_ensure_verified_doctor_list",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy guard must be unreachable")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="مريم ولا سارة؟")],
        outcomes=[_choice("د. مريم", "د. سارة")],
    )

    assert source == "deterministic:doctor-contract"
    assert "د. مريم" in text and "د. سارة" in text


def test_doctor_availability_remains_owned_by_availability_contract() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={"availability": {
            "service_name": "PRP",
            "checked_dates": ["2026-10-01"],
            "availability_windows": [{
                "doctor_name": "د. مريم",
                "start_local": "2026-10-01T18:00:00+03:00",
                "end_local": "2026-10-01T18:00:00+03:00",
                "start_time_24h": "18:00",
                "end_time_24h": "18:00",
            }],
            "available_option_count": 1,
        }},
    )
    contract = build_customer_response_contract([outcome])

    assert contract.units[0].doctor_truth is None
    assert is_pure_supported_doctor_contract(contract) is False


def test_doctor_compatibility_failure_stays_outside_doctor_contract() -> None:
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={"compatibility_failure": {
            "dimension": "doctor",
            "service_name": "PRP",
            "requested_name": "د. سارة",
            "compatible_options": ["د. مريم"],
        }},
    )
    contract = build_customer_response_contract([outcome])

    assert contract.units[0].doctor_truth is None
    assert is_pure_supported_doctor_contract(contract) is False


def test_mixed_doctor_and_service_response_stays_entirely_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [
        _answer([{"name": "د. مريم"}, {"name": "د. سارة"}]),
        TurnOutcome(
            status="answered",
            response_goal="answer_service",
            facts={"service_catalog": {"service": {
                "name": "Hydrafacial",
                "description": "تنظيف عميق",
            }}},
        ),
    ]
    monkeypatch.setattr(
        responder,
        "compose_doctor_contract_reply",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("mixed set must not enter pure doctor composer")
        ),
    )
    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(responder, "model_label", lambda name: str(name))
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=responder.ResponderDraft(
                reply="الدكاترة د. مريم ود. سارة، والخدمة تنظيف عميق.",
                availability_claim="not_applicable",
            ),
            model_name="test-model",
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="مين الدكاترة وإيه تفاصيل الخدمة؟")],
        outcomes=outcomes,
    )

    assert source == "test-model"
    assert "د. مريم" in text and "د. سارة" in text
    assert "تنظيف عميق" in text


def test_doctor_working_hours_do_not_become_appointment_availability() -> None:
    contract = build_customer_response_contract(
        [_answer([{
            "name": "د. مريم",
            "specialization": "Dermatology",
            "working_hours": [{"weekday": 1, "start": "10:00", "end": "18:00"}],
        }])]
    )
    text = deterministic_doctor_contract_reply(contract, arabic=True)

    assert "10:00" not in text
    assert "18:00" not in text
    assert "متاح" not in text
