from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2 import responder
from app.agents.v2.responder import ResponderDraft, compose_v2_customer_reply
from app.services.agent_v2.outcome import TurnOutcome

NOW = datetime.fromisoformat("2026-09-28T15:00:00+03:00")


def _complete_window() -> dict[str, object]:
    return {
        "doctor_name": "أحمد محمود",
        "laser_device_name": "Prime Lase",
        "start_local": "2026-09-28T19:00:00+03:00",
        "end_local": "2026-09-28T21:00:00+03:00",
        "start_time_24h": "19:00",
        "end_time_24h": "21:00",
    }


def _start_only_window() -> dict[str, object]:
    return {
        "doctor_name": "أحمد محمود",
        "laser_device_name": "Prime Lase",
        "start_local": "2026-09-28T19:00:00+03:00",
        "start_time_24h": "19:00",
    }


def _availability(*, count: int = 1, start_only: bool = False) -> dict[str, object]:
    return {
        "available_option_count": count,
        "availability_windows": (
            [_start_only_window() if start_only else _complete_window()] if count else []
        ),
    }


def _run_with_draft(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome: TurnOutcome,
    reply: str,
    claim: str,
    force_mixed_availability: bool = False,
) -> tuple[str, str]:
    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(responder, "model_label", lambda name: str(name))
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=ResponderDraft(reply=reply, availability_claim=claim),
            model_name="test-model",
        ),
    )
    outcomes = [outcome]
    if force_mixed_availability:
        outcomes.append(
            TurnOutcome(
                status="answered",
                response_goal="answer_service",
                facts={"service_catalog": {"service": {"name": "Hydrafacial"}}},
            )
        )
    return compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="ايوة")],
        outcomes=outcomes,
    )


@pytest.mark.parametrize("goal", ["booking_completed", "reschedule_completed"])
def test_terminal_scheduling_success_ignores_supporting_availability_facts(
    monkeypatch: pytest.MonkeyPatch,
    goal: str,
) -> None:
    action = "booking" if goal == "booking_completed" else "reschedule"
    natural = (
        "تم تأكيد حجزك اليوم الساعة 7 مساءً على Prime Lase."
        if action == "booking"
        else "تم تغيير ميعادك للساعة 7 مساءً على Prime Lase."
    )
    outcome = TurnOutcome(
        status="completed",
        response_goal=goal,
        facts={"availability": _availability(start_only=True)},
        action_result={"ok": True},
    )

    monkeypatch.setattr(
        responder,
        "compose_terminal_contract_reply",
        lambda **_kwargs: (
            natural,
            "contract-composer:test-model",
        ),
    )
    text, source = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="ايوة")],
        outcomes=[outcome],
    )

    assert text == natural
    assert source == "contract-composer:test-model"


def test_terminal_start_only_verification_evidence_never_triggers_availability_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    natural = "تم تأكيد حجزك بنجاح."
    outcome = TurnOutcome(
        status="completed",
        response_goal="booking_completed",
        facts={"availability": _availability(start_only=True)},
        action_result={"ok": True},
    )

    monkeypatch.setattr(
        responder,
        "compose_terminal_contract_reply",
        lambda **_kwargs: (
            natural,
            "contract-composer:test-model",
        ),
    )
    text, source = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="ايوة")],
        outcomes=[outcome],
    )

    assert "تفاصيل الفترة مش متاحة" not in text
    assert text == natural
    assert source == "contract-composer:test-model"


@pytest.mark.parametrize(
    ("goal", "status", "facts", "bad_claim", "expected_fragment"),
    [
        ("present_availability", "answered", {"availability": _availability()}, "no_availability", "7 م"),
        (
            "requested_time_unavailable",
            "blocked",
            {"availability": {"available_option_count": 0}},
            "not_applicable",
            "مش متاح",
        ),
        (
            "no_availability",
            "blocked",
            {"availability": {"available_option_count": 0}},
            "not_applicable",
            "مفيش مواعيد",
        ),
    ],
)
def test_true_availability_outcomes_still_activate_guard(
    monkeypatch: pytest.MonkeyPatch,
    goal: str,
    status: str,
    facts: dict[str, object],
    bad_claim: str,
    expected_fragment: str,
) -> None:
    outcome = TurnOutcome(status=status, response_goal=goal, facts=facts)

    text, source = _run_with_draft(
        monkeypatch,
        outcome=outcome,
        reply="رد غير متوافق مع الحقيقة المؤكدة.",
        claim=bad_claim,
        force_mixed_availability=True,
    )

    assert source.startswith("deterministic:availability-guard:")
    assert expected_fragment in text


@pytest.mark.parametrize(
    ("goal", "status"),
    [
        ("answer_clinic_info", "answered"),
        ("package_information", "answered"),
        ("clarification", "needs_input"),
    ],
)
def test_non_availability_outcome_does_not_infer_response_intent_from_stale_availability_facts(
    monkeypatch: pytest.MonkeyPatch,
    goal: str,
    status: str,
) -> None:
    natural = "ده رد الـoutcome الحالي، والمواعيد القديمة مجرد verification evidence."
    outcome = TurnOutcome(
        status=status,
        response_goal=goal,
        facts={"availability": _availability()},
    )

    text, source = _run_with_draft(
        monkeypatch,
        outcome=outcome,
        reply=natural,
        claim="not_applicable",
    )

    assert text == natural
    assert source == "test-model"


def test_device_price_guard_remains_specific_when_clarification_contains_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={
            "needed": "device",
            "availability": {
                **_availability(),
                "laser_device_options": [
                    {"device_name": "Candela Gentle", "price": "2100.00 EGP"},
                    {"device_name": "Prime Lase", "price": "1800.00 EGP"},
                ],
            },
        },
    )

    text, source = _run_with_draft(
        monkeypatch,
        outcome=outcome,
        reply="اختار الجهاز المناسب.",
        claim="not_applicable",
    )

    assert source == "deterministic:device-price-guard:test-model"
    assert "Candela Gentle" in text and "2100 جنيه" in text
    assert "Prime Lase" in text and "1800 جنيه" in text


def test_availability_claim_is_driven_by_response_semantics_not_fact_presence() -> None:
    terminal = TurnOutcome(
        status="completed",
        response_goal="booking_completed",
        facts={"availability": _availability()},
        action_result={"ok": True},
    )
    informational = TurnOutcome(
        status="answered",
        response_goal="package_information",
        facts={"availability": _availability()},
    )
    presentation = TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={"availability": _availability()},
    )

    assert responder._verified_availability_claim([terminal]) == "not_applicable"
    assert responder._verified_availability_claim([informational]) == "not_applicable"
    assert responder._verified_availability_claim([presentation]) == "options_available"


def test_medical_handoff_guard_requires_explicit_medical_handoff_semantics() -> None:
    medical = TurnOutcome(
        status="handoff",
        response_goal="handoff",
        facts={"category": "medical", "priority": "urgent"},
    )
    payment = TurnOutcome(
        status="handoff",
        response_goal="handoff",
        facts={"category": "payment", "priority": "normal"},
    )

    medical_reply = responder._deterministic_medical_handoff_reply(
        [HumanMessage(content="عندي مشكلة طبية")],
        [medical],
    )
    payment_reply = responder._deterministic_medical_handoff_reply(
        [HumanMessage(content="عندي سؤال دفع")],
        [payment],
    )

    assert medical_reply is not None and "عاجلة" in medical_reply
    assert payment_reply is None


def test_compatibility_guard_requires_needs_input_compatibility_failure() -> None:
    relevant = TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={
            "compatibility_failure": {
                "dimension": "device",
                "service_name": "ليزر",
                "requested_name": "Old Device",
                "compatible_options": ["Prime Lase"],
            }
        },
    )
    unrelated = TurnOutcome(
        status="answered",
        response_goal="answer_service",
        facts=relevant.facts,
    )

    assert responder._deterministic_compatibility_reply(
        [HumanMessage(content="احجز")], [relevant]
    ) is not None
    assert responder._deterministic_compatibility_reply(
        [HumanMessage(content="احجز")], [unrelated]
    ) is None


def test_doctor_completion_guard_does_not_append_doctors_to_unrelated_outcome() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_service",
        facts={"doctors": {"doctors": [{"name": "د. مريم"}, {"name": "د. أحمد"}]}},
    )
    original = "الخدمة متاحة."

    assert responder._ensure_verified_doctor_list(
        original,
        history=[HumanMessage(content="الخدمة متاحة؟")],
        outcomes=[outcome],
    ) == original


def test_device_price_guard_does_not_infer_clarification_from_price_fact_presence() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="package_information",
        facts={
            "needed": "device",
            "availability": {
                "laser_device_options": [
                    {"device_name": "Candela Gentle", "price": "2100.00 EGP"},
                    {"device_name": "Prime Lase", "price": "1800.00 EGP"},
                ]
            },
        },
    )

    assert responder._deterministic_device_price_guard_reply(
        "الباكدج متاحة.",
        history=[HumanMessage(content="الباكدج؟")],
        outcomes=[outcome],
    ) is None


def test_post_llm_guard_precedence_is_semantically_disjoint_after_availability_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={
            "needed": "device",
            "availability": {
                "available_option_count": 2,
                "availability_windows": [_complete_window()],
                "laser_device_options": [
                    {"device_name": "Candela Gentle", "price": "2100.00 EGP"},
                    {"device_name": "Prime Lase", "price": "1800.00 EGP"},
                ],
            },
        },
    )

    assert responder._verified_availability_claim([outcome]) == "not_applicable"
    text, source = _run_with_draft(
        monkeypatch,
        outcome=outcome,
        reply="اختار الجهاز.",
        claim="not_applicable",
    )
    assert source == "deterministic:device-price-guard:test-model"
    assert "Candela Gentle" in text and "Prime Lase" in text


def test_handoff_ack_exception_is_narrowly_scoped_to_new_unclaimed_ai_handoff() -> None:
    from app.services.agent_v2.live_chat import _v2_handoff_ack_allowed

    pending_ai = SimpleNamespace(source="ai", status="pending", assigned_user_id=None)
    claimed_ai = SimpleNamespace(source="ai", status="claimed", assigned_user_id="staff-1")
    pending_staff = SimpleNamespace(source="staff", status="pending", assigned_user_id=None)

    assert _v2_handoff_ack_allowed(pending_ai, created_this_turn=True) is True
    assert _v2_handoff_ack_allowed(pending_ai, created_this_turn=False) is False
    assert _v2_handoff_ack_allowed(claimed_ai, created_this_turn=True) is False
    assert _v2_handoff_ack_allowed(pending_staff, created_this_turn=True) is False
