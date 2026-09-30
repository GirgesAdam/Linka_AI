from types import SimpleNamespace

import pytest

from app.agents import onboarding_planner
from app.agents.llm_runtime import LLMProviderError

PROVIDER_DECISION = SimpleNamespace(marker="provider-decision")


def test_503_uses_fallback_after_primary_failure(monkeypatch) -> None:
    calls: list[str] = []
    primary = object()
    fallback = object()

    monkeypatch.setattr(
        onboarding_planner,
        "build_onboarding_model",
        lambda: primary,
    )
    monkeypatch.setattr(
        onboarding_planner,
        "build_onboarding_fallback_model",
        lambda: fallback,
    )

    def invoke(*, model, schema, messages):
        calls.append("primary" if model is primary else "fallback")
        if model is primary:
            raise LLMProviderError(
                "capacity",
                status_code=503,
                retryable=True,
            )
        return PROVIDER_DECISION

    monkeypatch.setattr(
        onboarding_planner,
        "invoke_typed_structured_output",
        invoke,
    )

    result = onboarding_planner._invoke_onboarding_provider_decision(
        messages=[],
    )

    assert result is PROVIDER_DECISION
    assert calls == ["primary", "fallback"]


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 429])
def test_client_or_quota_errors_do_not_switch_models(
    monkeypatch,
    status_code: int,
) -> None:
    calls: list[str] = []
    primary = object()

    monkeypatch.setattr(
        onboarding_planner,
        "build_onboarding_model",
        lambda: primary,
    )
    monkeypatch.setattr(
        onboarding_planner,
        "build_onboarding_fallback_model",
        lambda: pytest.fail("fallback must not be built"),
    )

    def invoke(*, model, schema, messages):
        calls.append("primary")
        raise LLMProviderError(
            "provider error",
            status_code=status_code,
            retryable=status_code == 429,
        )

    monkeypatch.setattr(
        onboarding_planner,
        "invoke_typed_structured_output",
        invoke,
    )

    with pytest.raises(LLMProviderError) as exc_info:
        onboarding_planner._invoke_onboarding_provider_decision(
            messages=[],
        )

    assert exc_info.value.status_code == status_code
    assert calls == ["primary"]


def test_no_configured_fallback_surfaces_primary_503(monkeypatch) -> None:
    primary = object()
    monkeypatch.setattr(
        onboarding_planner,
        "build_onboarding_model",
        lambda: primary,
    )
    monkeypatch.setattr(
        onboarding_planner,
        "build_onboarding_fallback_model",
        lambda: None,
    )

    def invoke(*, model, schema, messages):
        raise LLMProviderError(
            "capacity",
            status_code=503,
            retryable=True,
        )

    monkeypatch.setattr(
        onboarding_planner,
        "invoke_typed_structured_output",
        invoke,
    )

    with pytest.raises(LLMProviderError) as exc_info:
        onboarding_planner._invoke_onboarding_provider_decision(
            messages=[],
        )

    assert exc_info.value.status_code == 503


def test_default_models_use_luna_primary_and_gpt5_mini_fallback() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["openai_model"].default == "gpt-6-luna"
    assert Settings.model_fields["openai_fallback_model"].default == "gpt-5-mini"
