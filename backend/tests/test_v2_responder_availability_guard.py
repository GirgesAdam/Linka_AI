from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from app.agents.v2 import responder
from app.agents.v2.responder import ResponderDraft, compose_v2_customer_reply
from app.services.agent_v2.outcome import TurnOutcome


def _window(start: str, end: str) -> dict[str, object]:
    return {
        "doctor_name": "د. مريم",
        "start_local": f"2026-09-13T{start}:00+03:00",
        "end_local": f"2026-09-13T{end}:00+03:00",
        "start_time_24h": start,
        "end_time_24h": end,
    }


def _availability_outcome(*, count: int, windows: list[dict[str, object]]) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={
            "availability": {
                "available_option_count": count,
                "availability_windows": windows,
            }
        },
    )


def _run_with_draft(monkeypatch, *, draft: ResponderDraft, outcomes: list[TurnOutcome]) -> tuple[str, str]:
    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(responder, "model_label", lambda name: str(name))
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **kwargs: SimpleNamespace(value=draft, model_name="test-model"),
    )
    return compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=datetime.fromisoformat("2026-09-13T18:00:00+03:00"),
        history=[HumanMessage(content="لو الساعة 6 مش متاحة وريني البدائل")],
        outcomes=outcomes,
    )



def test_legacy_untyped_availability_guard_stays_reachable(monkeypatch) -> None:
    outcome = TurnOutcome(
        status="blocked",
        response_goal="requested_time_unavailable",
        facts={"availability": {"available_option_count": 0}},
    )

    text, source = _run_with_draft(
        monkeypatch,
        draft=ResponderDraft(
            reply="مفيش مواعيد متاحة.",
            availability_claim="no_availability",
        ),
        outcomes=[outcome],
    )

    assert "الوقت اللي طلبته مش متاح" in text
    assert "مفيش مواعيد متاحة." not in text
    assert source.startswith("deterministic:availability-guard:")


def test_legacy_untyped_matching_availability_claim_keeps_natural_reply(monkeypatch) -> None:
    outcome = TurnOutcome(
        status="blocked",
        response_goal="requested_time_unavailable",
        facts={"availability": {"available_option_count": 0}},
    )
    natural = "الوقت المطلوب مش متاح، أقدر أشوف بديل."

    text, source = _run_with_draft(
        monkeypatch,
        draft=ResponderDraft(
            reply=natural,
            availability_claim="requested_time_unavailable",
        ),
        outcomes=[outcome],
    )

    assert text == natural
    assert source == "test-model"


def test_typed_availability_is_not_forced_through_legacy_guard(monkeypatch) -> None:
    outcome = _availability_outcome(count=5, windows=[_window("18:00", "20:00")])
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("responder legacy model must not own typed availability")
        ),
    )

    contract = responder.build_customer_response_contract([outcome])
    assert responder.is_pure_supported_availability_contract(contract) is True

def test_positive_fallback_options_override_exact_time_miss_semantically() -> None:
    exact_miss = TurnOutcome(
        status="blocked",
        response_goal="requested_time_unavailable",
        facts={"availability": {"available_option_count": 0}},
    )
    alternatives = _availability_outcome(
        count=3,
        windows=[_window("18:30", "19:30")],
    )

    assert responder._verified_availability_claim([exact_miss, alternatives]) == "options_available"


def test_exact_time_miss_without_verified_alternative_has_distinct_claim() -> None:
    exact_miss = TurnOutcome(
        status="blocked",
        response_goal="requested_time_unavailable",
        facts={"availability": {"available_option_count": 0}},
    )

    assert responder._verified_availability_claim([exact_miss]) == "requested_time_unavailable"
