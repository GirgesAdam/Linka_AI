from app.core.config import Settings

BASE = {
    "DATABASE_URL": "postgresql+psycopg://user:pass@localhost:5432/db",
    "MIGRATION_DATABASE_URL": "postgresql+psycopg://user:pass@localhost:5432/db",
    "SUPABASE_URL": "https://abcdefghijklmnop.supabase.co",
    "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
    "SUPABASE_SECRET_KEY": "sb_secret_test",
}


def _set_base(monkeypatch) -> None:
    for key, value in BASE.items():
        monkeypatch.setenv(key, value)


def test_openai_is_the_only_configured_provider(monkeypatch) -> None:
    _set_base(monkeypatch)

    settings = Settings(_env_file=None)

    assert settings.llm_provider == "openai"
    assert settings.openai_model == "gpt-6-luna"
    assert settings.openai_fallback_model == "gpt-5-mini"
    assert settings.llm_realtime_max_retries == 0


def test_openai_fallback_can_be_disabled(monkeypatch) -> None:
    _set_base(monkeypatch)
    monkeypatch.setenv("OPENAI_FALLBACK_MODEL", "")

    settings = Settings(_env_file=None)

    assert settings.openai_fallback_model is None


def test_realtime_agent_loop_is_bounded(monkeypatch) -> None:
    _set_base(monkeypatch)

    settings = Settings(_env_file=None)

    assert settings.agent_recursion_limit == 8
    assert settings.agent_max_tool_rounds == 2
    assert settings.agent_prefetch_reads_enabled is True
    assert settings.channel_dispatch_max_attempts == 5
