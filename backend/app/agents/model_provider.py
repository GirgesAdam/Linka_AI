from __future__ import annotations

from functools import lru_cache

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.core.config import settings


class LLMConfigurationError(RuntimeError):
    pass


def _require_openai_key() -> str:
    if not settings.openai_api_key:
        raise LLMConfigurationError(
            "OPENAI_API_KEY is not configured. Add it to the backend environment."
        )
    return settings.openai_api_key


@lru_cache(maxsize=32)
def _cached_openai_model(
    *,
    api_key: str,
    model: str,
    reasoning_effort: str,
    max_tokens: int,
    timeout_seconds: int,
    max_retries: int,
    prompt_cache_mode: str | None = None,
    prompt_cache_ttl: str | None = None,
) -> BaseChatModel:
    """Reuse stateless OpenAI Responses API clients across customer turns.

    Linka keeps conversation/workflow state in its own database and messages, so the
    provider response is not stored server-side by OpenAI. The v0 LangChain output
    shape keeps the existing LangGraph/tool-message contract stable while the
    transport uses the Responses API.
    """
    prompt_cache_options = None
    if prompt_cache_mode is not None:
        prompt_cache_options = {"mode": prompt_cache_mode}
        if prompt_cache_ttl is not None:
            prompt_cache_options["ttl"] = prompt_cache_ttl

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        use_responses_api=True,
        output_version="v0",
        reasoning={"effort": reasoning_effort},
        max_tokens=max_tokens,
        timeout=timeout_seconds,
        max_retries=max_retries,
        streaming=False,
        store=False,
        prompt_cache_options=prompt_cache_options,
    )


def _reasoning_effort_for(model: str) -> str:
    if settings.openai_fallback_model and model == settings.openai_fallback_model:
        return settings.openai_fallback_reasoning_effort
    return settings.openai_reasoning_effort


def _supports_explicit_prompt_cache(model: str) -> bool:
    """Return whether the configured GPT family supports explicit breakpoints."""

    normalized = str(model).strip().lower()
    if not normalized.startswith("gpt-"):
        return False
    version = normalized.removeprefix("gpt-").split("-", 1)[0]
    parts = version.split(".", 1)
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) == 2 else 0
    except ValueError:
        return False
    return (major, minor) >= (5, 6)


def _build_openai_model(
    *,
    model: str,
    max_tokens: int,
    max_retries: int | None = None,
    prompt_cache_mode: str | None = None,
    prompt_cache_ttl: str | None = None,
) -> BaseChatModel:
    retries = settings.llm_max_retries if max_retries is None else max_retries
    return _cached_openai_model(
        api_key=_require_openai_key(),
        model=model,
        reasoning_effort=_reasoning_effort_for(model),
        max_tokens=max_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=retries,
        prompt_cache_mode=prompt_cache_mode,
        prompt_cache_ttl=prompt_cache_ttl,
    )


def _build_optional_fallback_model(
    *,
    max_tokens: int,
    max_retries: int | None = None,
) -> BaseChatModel | None:
    fallback = settings.openai_fallback_model
    if not fallback or fallback == settings.openai_model:
        return None
    return _build_openai_model(
        model=fallback,
        max_tokens=max_tokens,
        max_retries=max_retries,
    )


def build_realtime_interpreter_model() -> BaseChatModel:
    explicit_cache = _supports_explicit_prompt_cache(settings.openai_model)
    return _build_openai_model(
        model=settings.openai_model,
        max_tokens=settings.agent_router_max_output_tokens,
        max_retries=settings.llm_realtime_max_retries,
        prompt_cache_mode="explicit" if explicit_cache else None,
        prompt_cache_ttl="30m" if explicit_cache else None,
    )


def build_realtime_interpreter_fallback_model() -> BaseChatModel | None:
    fallback = settings.openai_fallback_model
    if not fallback or fallback == settings.openai_model:
        return None
    explicit_cache = _supports_explicit_prompt_cache(fallback)
    return _build_openai_model(
        model=fallback,
        max_tokens=settings.agent_router_max_output_tokens,
        max_retries=settings.llm_realtime_max_retries,
        prompt_cache_mode="explicit" if explicit_cache else None,
        prompt_cache_ttl="30m" if explicit_cache else None,
    )


def build_realtime_interpreter_emergency_model() -> BaseChatModel | None:
    return None


def build_realtime_composer_model() -> BaseChatModel:
    return _build_openai_model(
        model=settings.openai_model,
        max_tokens=settings.llm_max_output_tokens,
        max_retries=settings.llm_realtime_max_retries,
    )


def build_realtime_composer_fallback_model() -> BaseChatModel | None:
    return _build_optional_fallback_model(
        max_tokens=settings.llm_max_output_tokens,
        max_retries=settings.llm_realtime_max_retries,
    )


def build_chat_model() -> BaseChatModel:
    return _build_openai_model(
        model=settings.openai_model,
        max_tokens=settings.llm_max_output_tokens,
        max_retries=settings.llm_realtime_max_retries,
    )


def build_chat_fallback_model() -> BaseChatModel | None:
    return _build_optional_fallback_model(
        max_tokens=settings.llm_max_output_tokens,
        max_retries=settings.llm_realtime_max_retries,
    )


def build_semantic_router_model() -> BaseChatModel:
    return _build_openai_model(
        model=settings.openai_model,
        max_tokens=settings.agent_router_max_output_tokens,
        max_retries=settings.llm_realtime_max_retries,
    )


def build_semantic_router_fallback_model() -> BaseChatModel | None:
    return _build_optional_fallback_model(
        max_tokens=settings.agent_router_max_output_tokens,
        max_retries=settings.llm_realtime_max_retries,
    )


def build_flow_interpreter_model() -> BaseChatModel:
    return _build_openai_model(
        model=settings.openai_model,
        max_tokens=settings.agent_flow_interpreter_max_output_tokens,
        max_retries=settings.llm_realtime_max_retries,
    )


def build_flow_interpreter_fallback_model() -> BaseChatModel | None:
    return _build_optional_fallback_model(
        max_tokens=settings.agent_flow_interpreter_max_output_tokens,
        max_retries=settings.llm_realtime_max_retries,
    )


def build_onboarding_model(*, max_retries: int | None = None) -> BaseChatModel:
    return _build_openai_model(
        model=settings.openai_model,
        max_tokens=settings.openai_onboarding_max_output_tokens,
        max_retries=max_retries,
    )


def build_onboarding_fallback_model(
    *,
    max_retries: int | None = None,
) -> BaseChatModel | None:
    return _build_optional_fallback_model(
        max_tokens=settings.openai_onboarding_max_output_tokens,
        max_retries=max_retries,
    )


def build_utility_model() -> BaseChatModel:
    return _build_openai_model(
        model=settings.openai_model,
        max_tokens=1024,
    )


def model_label(model_name: str) -> str:
    return f"openai:{model_name}"


def active_model_name() -> str:
    return settings.openai_model


def active_model_label() -> str:
    return model_label(active_model_name())
