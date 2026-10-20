from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2 import responder
from app.agents.v2.responder import ResponderDraft, compose_v2_customer_reply
from app.agents.v2.terminal_composer import deterministic_terminal_fallback
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.response_contract import build_customer_response_contract

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _already_completed(action: str) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="social_ack",
        facts={
            "acknowledgment": {
                "action": action,
                "already_completed": True,
            }
        },
    )


def test_already_completed_action_projects_server_owned_action_truth() -> None:
    contract = build_customer_response_contract([_already_completed("cancel_appointment")])

    unit = contract.units[0]
    assert unit.action_truth is not None
    assert unit.action_truth.action == "cancel_appointment"
    assert unit.action_truth.succeeded is True
    acknowledgment = next(fact.value for fact in unit.facts if fact.key == "acknowledgment")
    assert acknowledgment == {
        "action": "cancel_appointment",
        "already_completed": True,
    }


def test_already_completed_action_cannot_be_reversed_by_generic_composer(monkeypatch) -> None:
    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **kwargs: SimpleNamespace(
            value=ResponderDraft(
                reply="The cancellation did not happen.",
                availability_claim="not_applicable",
            ),
            model_name="adversarial-stub",
        ),
    )

    reply, source = compose_v2_customer_reply(
        clinic_name="Linka Test Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="Is it cancelled?")],
        outcomes=[_already_completed("cancel_appointment")],
    )

    assert "did not happen" not in reply.casefold()
    assert "already" in reply.casefold()
    assert source.startswith("deterministic:")


def _terminal_outcome(*, ok: bool) -> TurnOutcome:
    return TurnOutcome(
        status="completed",
        response_goal="cancellation_completed",
        action_result={"ok": ok},
    )


def test_current_turn_completed_action_keeps_current_turn_wording(monkeypatch) -> None:
    from app.agents.v2 import terminal_composer
    from app.agents.v2.terminal_composer import (
        TerminalComposerDraft,
        TerminalComposerUnitDraft,
    )

    monkeypatch.setattr(terminal_composer, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(
        terminal_composer,
        "invoke_with_model_chain",
        lambda **kwargs: SimpleNamespace(
            value=TerminalComposerDraft(
                units=[
                    TerminalComposerUnitDraft(
                        unit_index=0,
                        action_ref="unit_action",
                        style="plain",
                        fact_keys=[],
                        transition="sentence",
                    )
                ]
            ),
            model_name="structured-style-stub",
        ),
    )

    reply, _ = compose_v2_customer_reply(
        clinic_name="Linka Test Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="Cancel it")],
        outcomes=[_terminal_outcome(ok=True)],
    )

    assert reply == "Your appointment has been cancelled."
    assert "already" not in reply.casefold()


def test_failed_action_truth_cannot_be_rendered_as_success(monkeypatch) -> None:
    from app.agents.v2 import terminal_composer
    from app.agents.v2.terminal_composer import (
        TerminalComposerDraft,
        TerminalComposerUnitDraft,
    )

    contract = build_customer_response_contract([_terminal_outcome(ok=False)])
    assert contract.units[0].action_truth is not None
    assert contract.units[0].action_truth.succeeded is False

    monkeypatch.setattr(terminal_composer, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(
        terminal_composer,
        "invoke_with_model_chain",
        lambda **kwargs: SimpleNamespace(
            value=TerminalComposerDraft(
                units=[
                    TerminalComposerUnitDraft(
                        unit_index=0,
                        action_ref="unit_action",
                        style="friendly",
                        fact_keys=[],
                        transition="sentence",
                    )
                ]
            ),
            model_name="structured-style-stub",
        ),
    )

    reply, _ = compose_v2_customer_reply(
        clinic_name="Linka Test Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="Cancel it")],
        outcomes=[_terminal_outcome(ok=False)],
    )

    assert "not completed" in reply.casefold()
    assert "has been cancelled" not in reply.casefold()


def test_other_existing_reschedule_acknowledgment_uses_same_grounding() -> None:
    contract = build_customer_response_contract([_already_completed("reschedule")])
    unit = contract.units[0]
    assert unit.action_truth is not None
    assert unit.action_truth.action == "reschedule"
    assert unit.action_truth.succeeded is True

    reply, source = compose_v2_customer_reply(
        clinic_name="Linka Test Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="Was it moved already?")],
        outcomes=[_already_completed("reschedule")],
    )

    assert "already" in reply.casefold()
    assert "rescheduled" in reply.casefold()
    assert source == "deterministic:mixed-typed-contract"


def test_existing_same_booking_acknowledgment_keeps_specialized_status_wording() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="social_ack",
        facts={
            "acknowledgment": {
                "action": "booking",
                "already_completed": True,
                "same_booking": True,
            },
            "appointments": {
                "appointments": [{"status": "confirmed"}],
            },
        },
    )
    unit = build_customer_response_contract([outcome]).units[0]
    assert unit.action_truth is not None
    assert unit.action_truth.action == "booking"

    reply, _ = compose_v2_customer_reply(
        clinic_name="Linka Test Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="Is that booking done?")],
        outcomes=[outcome],
    )

    assert reply == "The booking is already confirmed."


def test_ordinary_social_ack_still_uses_generic_responder(monkeypatch) -> None:
    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **kwargs: SimpleNamespace(
            value=ResponderDraft(
                reply="You're welcome.",
                availability_claim="not_applicable",
            ),
            model_name="ordinary-social-stub",
        ),
    )
    outcome = TurnOutcome(status="answered", response_goal="social_ack")
    assert build_customer_response_contract([outcome]).units[0].action_truth is None

    reply, source = compose_v2_customer_reply(
        clinic_name="Linka Test Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="Thanks")],
        outcomes=[outcome],
    )

    assert reply == "You're welcome."
    assert source == "openai:ordinary-social-stub"


def test_compound_action_acknowledgment_preserves_separate_factual_unit() -> None:
    clinic = TurnOutcome(
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

    reply, source = compose_v2_customer_reply(
        clinic_name="Linka Test Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="Is it cancelled already, and what's the phone number?")],
        outcomes=[_already_completed("cancel_appointment"), clinic],
    )

    assert "already cancelled" in reply.casefold()
    assert "01012345678" in reply
    assert source == "deterministic:mixed-typed-contract"


def test_unsupported_social_ack_action_does_not_invent_action_truth() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="social_ack",
        facts={
            "acknowledgment": {
                "action": "refund",
                "already_completed": True,
            }
        },
    )
    unit = build_customer_response_contract([outcome]).units[0]
    assert unit.action_truth is None


@pytest.mark.parametrize(
    "goal",
    [
        "booking_completed",
        "reschedule_completed",
        "cancellation_completed",
        "appointment_confirmed",
        "package_purchased",
        "pulse_pack_purchased",
        "follow_up_created",
        "marketing_updated",
    ],
)
def test_all_existing_terminal_action_failures_render_verified_failure(goal: str) -> None:
    outcome = TurnOutcome(
        status="completed",
        response_goal=goal,
        action_result={"ok": False},
    )
    contract = build_customer_response_contract([outcome])
    unit = contract.units[0]
    assert unit.action_truth is not None
    assert unit.action_truth.succeeded is False

    reply = deterministic_terminal_fallback(contract, arabic=False)
    assert "not" in reply.casefold()


def test_already_completed_action_arabic_render_preserves_prior_completion() -> None:
    contract = build_customer_response_contract([_already_completed("cancel_appointment")])
    reply = deterministic_terminal_fallback(contract, arabic=True)
    assert "\u0628\u0627\u0644\u0641\u0639\u0644" in reply
    assert "\u0644\u063a\u064a\u0646\u0627\u0644\u0643" not in reply
