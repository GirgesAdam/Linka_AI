from __future__ import annotations

from uuid import uuid4

from app.agents.v2.terminal_composer import (
    TerminalComposerDraft,
    TerminalComposerUnitDraft,
    deterministic_terminal_fallback,
    resolve_terminal_composer_draft,
)
from app.services.agent_v2.orchestrator import _booking_acknowledgment_step
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.planner import PlanStep, WriteIntent
from app.services.agent_v2.response_contract import build_customer_response_contract
from app.services.whatsapp_interactions import (
    _booking_proposal_success_reply,
    parse_whatsapp_booking_action,
)

AR_PENDING = "\u0642\u064a\u062f \u0627\u0644\u0627\u0646\u062a\u0638\u0627\u0631"
AR_REGISTERED = "\u062a\u0633\u062c\u064a\u0644 \u0627\u0644\u062d\u062c\u0632"
AR_REGISTERED_COLLOQUIAL = "\u0627\u0644\u062d\u062c\u0632 \u0627\u062a\u0633\u062c\u0644"
AR_BOOKING_CONFIRM = "\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u062d\u062c\u0632"
AR_BOOKING_CONFIRMED = "\u062d\u062c\u0632\u0643 \u0627\u062a\u0623\u0643\u062f"
AR_BOOKING_FIXED = "\u062b\u0628\u062a\u0646\u0627\u0644\u0643 \u0627\u0644\u062d\u062c\u0632"
AR_APPOINTMENT_CONFIRMED = "\u0645\u064a\u0639\u0627\u062f\u0643 \u0627\u062a\u0623\u0643\u062f"
AR_APPOINTMENT_CONFIRM = "\u062a\u0645 \u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0645\u0648\u0639\u062f"


def _booking_contract(status: str | None, *, package_used: bool = False):
    action_result: dict[str, object] = {"ok": True, "write_kind": "booking"}
    if status is not None:
        action_result["status"] = status
    facts: dict[str, object] = {}
    if package_used:
        facts.update({"package_used": True, "package_name": "6 Sessions"})
    outcome = TurnOutcome(
        status="completed",
        response_goal="booking_completed",
        facts=facts,
        action_result=action_result,
    )
    return build_customer_response_contract([outcome])


def _draft(*fact_keys: str, style: str = "friendly") -> TerminalComposerDraft:
    return TerminalComposerDraft(
        units=[
            TerminalComposerUnitDraft(
                unit_index=0,
                action_ref="unit_action",
                style=style,
                fact_keys=list(fact_keys),
                transition="sentence",
            )
        ]
    )


def _assert_no_ar_confirmation(text: str) -> None:
    for marker in (AR_BOOKING_CONFIRM, AR_BOOKING_CONFIRMED, AR_BOOKING_FIXED):
        assert marker not in text


def _assert_no_en_confirmation(text: str) -> None:
    for marker in (
        "Your booking is confirmed",
        "Your booking is all set",
        "I've confirmed your booking",
        "I\u2019ve confirmed your booking",
    ):
        assert marker not in text


def test_s1_pending_booking_creation_uses_pending_truth_arabic() -> None:
    text = deterministic_terminal_fallback(_booking_contract("pending"), arabic=True)
    assert AR_PENDING in text
    assert AR_REGISTERED in text or AR_REGISTERED_COLLOQUIAL in text
    _assert_no_ar_confirmation(text)


def test_s2_confirmed_booking_creation_allows_confirmed_wording() -> None:
    text = deterministic_terminal_fallback(_booking_contract("confirmed"), arabic=True)
    assert any(marker in text for marker in (AR_BOOKING_CONFIRM, AR_BOOKING_CONFIRMED, AR_BOOKING_FIXED))


def test_s3_missing_booking_status_uses_neutral_creation_wording() -> None:
    text = deterministic_terminal_fallback(_booking_contract(None), arabic=True)
    assert AR_PENDING not in text
    assert AR_REGISTERED in text or AR_REGISTERED_COLLOQUIAL in text
    _assert_no_ar_confirmation(text)


def test_s4_explicit_appointment_confirmation_preserves_confirmation_wording() -> None:
    outcome = TurnOutcome(
        status="completed",
        response_goal="appointment_confirmed",
        facts={},
        action_result={"ok": True, "status": "confirmed"},
    )
    text = deterministic_terminal_fallback(build_customer_response_contract([outcome]), arabic=True)
    assert AR_APPOINTMENT_CONFIRMED in text or AR_APPOINTMENT_CONFIRM in text


def test_s5_proposal_confirmation_is_not_existing_appointment_confirmation() -> None:
    proposal_id = str(uuid4())
    action = parse_whatsapp_booking_action(
        {
            "interactive_reply": {
                "type": "button_reply",
                "id": f"tia.booking.proposal.confirm:{proposal_id}",
                "title": "\u0623\u0643\u062f",
            }
        }
    )
    assert action is not None
    assert action.action == "proposal_confirm"
    assert action.proposal_id == proposal_id
    assert action.appointment_id is None


def test_s6_arabic_deterministic_fallback_never_confirms_pending_booking() -> None:
    text = deterministic_terminal_fallback(_booking_contract("pending"), arabic=True)
    assert AR_PENDING in text
    _assert_no_ar_confirmation(text)


def test_s7_arabic_terminal_composer_draft_never_confirms_pending_booking() -> None:
    contract = _booking_contract("pending")
    text = resolve_terminal_composer_draft(contract, _draft(), arabic=True)
    assert AR_PENDING in text
    _assert_no_ar_confirmation(text)


def test_s8_english_deterministic_fallback_never_confirms_pending_booking() -> None:
    text = deterministic_terminal_fallback(_booking_contract("pending"), arabic=False)
    assert "pending" in text.lower()
    _assert_no_en_confirmation(text)


def test_s9_english_terminal_composer_draft_never_confirms_pending_booking() -> None:
    contract = _booking_contract("pending")
    text = resolve_terminal_composer_draft(contract, _draft(), arabic=False)
    assert "pending" in text.lower()
    _assert_no_en_confirmation(text)


def test_s10_multiservice_single_appointment_pending_truth_is_stated_once() -> None:
    outcome = TurnOutcome(
        status="completed",
        response_goal="booking_completed",
        facts={
            "service_name": "Laser Underarm + Full Arms",
            "multi_service_single_appointment": True,
        },
        action_result={"ok": True, "status": "pending"},
    )
    contract = build_customer_response_contract([outcome])
    text = deterministic_terminal_fallback(contract, arabic=False)
    assert text.lower().count("pending") == 1
    _assert_no_en_confirmation(text)


def test_s11_package_backed_pending_booking_keeps_package_truth_without_confirmation() -> None:
    contract = _booking_contract("pending", package_used=True)
    fact_keys = [fact.key for fact in contract.units[0].facts]
    text = resolve_terminal_composer_draft(
        contract,
        _draft("package_used", "package_name"),
        arabic=False,
    )
    assert "package_used" in fact_keys
    assert "pending" in text.lower()
    assert "6 Sessions" in text
    _assert_no_en_confirmation(text)


def test_s12_repeat_booking_acknowledgment_drops_duplicate_write_intent() -> None:
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        reads=[],
        write_intent=WriteIntent(kind="booking", authorized=True, parameters={}),
        state_action="none",
        response_goal="booking_completed",
        facts={},
    )
    acknowledged = _booking_acknowledgment_step(step)
    assert acknowledged.disposition == "respond"
    assert acknowledged.write_intent is None
    assert acknowledged.reads == []
    assert acknowledged.response_goal == "social_ack"
    assert acknowledged.facts["acknowledgment"]["already_completed"] is True


def test_structured_proposal_pending_reply_uses_persisted_status_truth() -> None:
    text = _booking_proposal_success_reply({"ok": True, "status": "pending"})
    assert AR_PENDING in text
    _assert_no_ar_confirmation(text)


def test_structured_proposal_confirmed_reply_allows_confirmed_wording() -> None:
    text = _booking_proposal_success_reply({"ok": True, "status": "confirmed"})
    assert AR_BOOKING_CONFIRM in text


def test_structured_proposal_missing_status_is_conservative() -> None:
    text = _booking_proposal_success_reply({"ok": True})
    assert AR_REGISTERED in text
    _assert_no_ar_confirmation(text)
