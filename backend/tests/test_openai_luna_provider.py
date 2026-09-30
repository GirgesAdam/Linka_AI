from pathlib import Path

from langchain_openai import ChatOpenAI

from app.agents.model_provider import build_chat_fallback_model, build_chat_model
from app.core.config import Settings, settings


def _settings_kwargs() -> dict[str, str]:
    return {
        "database_url": "postgresql+psycopg://user:pass@localhost:5432/test",
        "migration_database_url": "postgresql+psycopg://user:pass@localhost:5432/test",
        "supabase_url": "https://example.supabase.co",
        "supabase_publishable_key": "sb_publishable_test",
        "supabase_secret_key": "sb_secret_test",
    }


def test_openai_only_settings_use_luna_with_gpt5_mini_fallback() -> None:
    configured = Settings(
        _env_file=None,
        **_settings_kwargs(),
        openai_api_key="sk-test-not-a-real-key",
    )

    assert configured.llm_provider == "openai"
    assert configured.openai_model == "gpt-6-luna"
    assert configured.openai_fallback_model == "gpt-5-mini"
    assert configured.openai_reasoning_effort == "low"
    assert configured.openai_fallback_reasoning_effort == "low"
    assert configured.agent_max_tool_rounds == 2
    assert configured.agent_recursion_limit == 8


def test_openai_provider_builds_responses_api_primary_and_fallback(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-not-a-real-key")
    monkeypatch.setattr(settings, "openai_model", "gpt-6-luna")
    monkeypatch.setattr(settings, "openai_fallback_model", "gpt-5-mini")
    monkeypatch.setattr(settings, "openai_reasoning_effort", "low")
    monkeypatch.setattr(settings, "openai_fallback_reasoning_effort", "low")

    primary = build_chat_model()
    fallback = build_chat_fallback_model()

    assert isinstance(primary, ChatOpenAI)
    assert primary.model_name == "gpt-6-luna"
    assert primary.use_responses_api is True
    assert primary.store is False
    assert primary.reasoning == {"effort": "low"}

    assert isinstance(fallback, ChatOpenAI)
    assert fallback.model_name == "gpt-5-mini"
    assert fallback.use_responses_api is True
    assert fallback.store is False
    assert fallback.reasoning == {"effort": "low"}


def test_openai_base_models_are_process_cached() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "app/agents/model_provider.py").read_text(encoding="utf-8")
    assert "@lru_cache(maxsize=32)" in source
    assert "def _cached_openai_model" in source


def test_runtime_has_no_groq_dependency() -> None:
    backend = Path(__file__).resolve().parent.parent
    files = (
        "app/core/config.py",
        "app/agents/model_provider.py",
        "app/agents/structured_output.py",
        "app/agents/llm_runtime.py",
    )
    source = "\n".join(
        (backend / relative).read_text(encoding="utf-8") for relative in files
    ).lower()

    assert "chatgroq" not in source
    assert "langchain_groq" not in source
    assert "groq_api_key" not in source
