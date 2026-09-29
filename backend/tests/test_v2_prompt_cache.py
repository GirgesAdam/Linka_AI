from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.model_provider import (
    _cached_openai_model,
    _supports_explicit_prompt_cache,
    build_realtime_interpreter_fallback_model,
    build_realtime_interpreter_model,
)
from app.agents.structured_output import StructuredOutputError
from app.agents.v2 import turn_interpreter as interpreter
from app.agents.v2.responder import _system_prompt
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.agents.v2.turn_interpreter import (
    _messages_for_prompt_cache,
    _with_interpreter_prompt_cache_breakpoint,
)
from app.core.config import settings

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=ZoneInfo("Africa/Cairo"))


def _context():
    return build_semantic_context({"services": [], "doctors": [], "appointments": []})


def test_explicit_prompt_cache_support_is_model_family_scoped() -> None:
    assert _supports_explicit_prompt_cache("gpt-5.6-luna") is True
    assert _supports_explicit_prompt_cache("gpt-6-luna") is True
    assert _supports_explicit_prompt_cache("gpt-5-mini") is False
    assert _supports_explicit_prompt_cache("gpt-4.1") is False
    assert _supports_explicit_prompt_cache("other-provider-model") is False


def test_interpreter_primary_and_fallback_cache_configs_are_independent(monkeypatch) -> None:
    _cached_openai_model.cache_clear()
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-not-a-real-key")
    monkeypatch.setattr(settings, "openai_model", "gpt-5.6-luna")
    monkeypatch.setattr(settings, "openai_fallback_model", "gpt-5-mini")
    monkeypatch.setattr(settings, "openai_reasoning_effort", "low")
    monkeypatch.setattr(settings, "openai_fallback_reasoning_effort", "low")

    primary = build_realtime_interpreter_model()
    fallback = build_realtime_interpreter_fallback_model()

    assert primary.prompt_cache_options == {"mode": "explicit", "ttl": "30m"}
    assert "prompt_cache_key" not in primary.model_kwargs
    assert fallback is not None
    assert fallback.prompt_cache_options is None

    _cached_openai_model.cache_clear()
    monkeypatch.setattr(settings, "openai_fallback_model", "gpt-5.6-sol")
    supported_fallback = build_realtime_interpreter_fallback_model()
    assert supported_fallback is not None
    assert supported_fallback.prompt_cache_options == {"mode": "explicit", "ttl": "30m"}
    assert "prompt_cache_key" not in supported_fallback.model_kwargs
    _cached_openai_model.cache_clear()


def test_cache_breakpoint_preserves_prompt_text_and_excludes_dynamic_context() -> None:
    original = [
        SystemMessage(
            content=(
                "stable semantic contract\n"
                "Clinic timezone: Africa/Cairo\n"
                f"Clinic local time: {NOW.isoformat()}\n"
            )
        ),
        SystemMessage(content="SEMANTIC_CONTEXT:PRIVATE_WORKSPACE_DATA"),
        HumanMessage(content="PRIVATE_CUSTOMER_TEXT"),
    ]

    wrapped = _with_interpreter_prompt_cache_breakpoint(
        original,
        timezone_name="Africa/Cairo",
        local_now=NOW,
    )

    blocks = wrapped[0].content
    assert isinstance(blocks, list)
    assert "".join(block["text"] for block in blocks) == original[0].content
    assert blocks[0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert "PRIVATE_WORKSPACE_DATA" not in blocks[0]["text"]
    assert "PRIVATE_CUSTOMER_TEXT" not in blocks[0]["text"]
    assert wrapped[1:] == original[1:]


def test_cached_prefix_contains_current_pulse_ownership_boundary() -> None:
    messages = interpreter._build_interpreter_messages(
        history=[HumanMessage(content="عندي Pulses وعايزة أحجز ليزر الإبط على كانديلا")],
        semantic_context=_context(),
        timezone_name="Africa/Cairo",
        local_now=NOW,
    )
    wrapped = _with_interpreter_prompt_cache_breakpoint(
        messages,
        timezone_name="Africa/Cairo",
        local_now=NOW,
    )
    blocks = wrapped[0].content
    assert isinstance(blocks, list)
    stable = blocks[0]["text"]

    assert "Pulse balance/payment selection is not part of appointment booking." in stable
    assert "Reception handles" in stable
    assert "Pulse settlement" in stable
    assert "buy_pulse_pack means" in stable
    assert "Pulse pack is not a session" in stable
    assert "must not set package_usage" in stable
    assert "money was paid" in stable
    assert "financial_ledger" in stable
    assert "amounts already paid" in stable
    assert "balance due" in stable
    assert "payment transactions" in stable
    assert "payment status" in stable


def test_responder_contract_separates_offers_from_owned_pulse_state() -> None:
    text = _system_prompt(
        clinic_name="Linka Test",
        timezone_name="Africa/Cairo",
        local_now=NOW,
    )
    assert "Pulse-pack offer facts describe clinic offers available for purchase" in text
    assert "they are never evidence" in text
    assert "the customer owns that pack" in text
    assert "pulse_balance or pulse_packs" in text


def test_models_without_explicit_cache_keep_plain_messages() -> None:
    messages = [
        SystemMessage(
            content=(
                "stable\n"
                "Clinic timezone: Africa/Cairo\n"
                f"Clinic local time: {NOW.isoformat()}\n"
            )
        ),
        HumanMessage(content="hello"),
    ]
    model = SimpleNamespace(prompt_cache_options=None)

    selected = _messages_for_prompt_cache(
        model,
        messages,
        timezone_name="Africa/Cairo",
        local_now=NOW,
    )

    assert selected is messages
    assert isinstance(selected[0].content, str)


def test_structured_retry_reuses_same_cache_wrapped_messages(monkeypatch) -> None:
    model = SimpleNamespace(prompt_cache_options={"mode": "explicit", "ttl": "30m"})
    seen_messages = []

    monkeypatch.setattr(interpreter, "build_realtime_interpreter_model", lambda: model)
    monkeypatch.setattr(settings, "openai_model", "gpt-5.6-luna")
    monkeypatch.setattr(settings, "openai_fallback_model", "")

    def fake_structured_output(*, model, schema, messages):
        seen_messages.append(messages)
        if len(seen_messages) == 1:
            raise StructuredOutputError("retry")
        return TiaTurnUnderstanding(
            operations=[
                TurnOperation(
                    type="clinic_info",
                    entities=TurnEntities(),
                    selection=None,
                    package_usage="unspecified",
                    execution_intent="informational",
                )
            ],
            safety_signals=[],
        )

    monkeypatch.setattr(interpreter, "invoke_typed_structured_output", fake_structured_output)
    monkeypatch.setattr(
        interpreter,
        "invoke_with_model_chain",
        lambda *, model_calls, **_kwargs: SimpleNamespace(
            value=model_calls[0][1](),
            model_name="gpt-5.6-luna",
            used_fallback=False,
        ),
    )

    interpreter.interpret_customer_turn_v2(
        history=[HumanMessage(content="PRP للبشرة بكام؟")],
        semantic_context=_context(),
        timezone_name="Africa/Cairo",
        local_now=NOW,
    )

    assert len(seen_messages) == 2
    assert seen_messages[0] is seen_messages[1]
    assert isinstance(seen_messages[0][0].content, list)
    assert seen_messages[0][0].content[0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }
