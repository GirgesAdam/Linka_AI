import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Linka"
    environment: str = "staging"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    docs_enabled: bool = True
    log_level: str = "INFO"

    # Recruiter/public demo sandbox. The agent and database writes remain real,
    # while provider delivery is disabled unless explicitly allowed.
    demo_mode: bool = False
    demo_allow_external_dispatch: bool = False
    demo_agent_hourly_turn_limit: int = Field(default=60, ge=1, le=500)

    database_url: str
    migration_database_url: str

    supabase_url: str
    supabase_publishable_key: str
    supabase_secret_key: str

    # Linka uses OpenAI for all LLM generation. Luna is the normal low-cost model;
    # GPT-5 mini is a separate affordable model used only for cross-model failover.
    llm_provider: Literal["openai"] = "openai"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-luna"
    openai_fallback_model: str | None = "gpt-5-mini"
    openai_reasoning_effort: Literal[
        "none", "low", "medium", "high", "xhigh", "max"
    ] = "low"
    openai_fallback_reasoning_effort: Literal[
        "none", "low", "medium", "high"
    ] = "low"
    openai_onboarding_max_output_tokens: int = Field(
        default=8192,
        ge=2048,
        le=65536,
    )
    openai_embedding_model: str = "text-embedding-3-small"

    llm_timeout_seconds: int = Field(default=60, ge=5, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=6)
    # Realtime turns are latency-sensitive. Keep provider retries bounded; the
    # runtime error boundary decides whether the fallback model may run.
    llm_realtime_max_retries: int = Field(default=0, ge=0, le=2)
    llm_realtime_circuit_breaker_cooldown_seconds: int = Field(default=120, ge=0, le=900)
    llm_max_output_tokens: int = Field(default=2048, ge=256, le=8192)

    # Conversation/context budgets are product-quality bounds, not free-tier hacks.
    agent_history_messages: int = Field(default=24, ge=4, le=80)
    agent_operational_context_items: int = Field(default=8, ge=1, le=20)
    agent_operational_context_max_chars: int = Field(
        default=16000,
        ge=2000,
        le=50000,
    )
    agent_recursion_limit: int = Field(default=8, ge=4, le=32)
    agent_max_tool_rounds: int = Field(default=2, ge=1, le=4)
    agent_prefetch_reads_enabled: bool = True

    # Agent Core V2 is opt-in until cutover is explicitly enabled. Both HTTP and
    # channel turns pass through the same live facade, so one flag controls them.
    agent_v2_live_enabled: bool = False

    # PostgreSQL owns outbound retry state. n8n performs one provider attempt per
    # claimed dispatch, then reports the real provider result back here.
    channel_dispatch_max_attempts: int = Field(default=5, ge=1, le=20)

    # Unified customer-turn orchestration is now the default realtime path. The
    # legacy split router/flow interpreter remains behind this flag as rollback
    # insurance while deterministic policy/state/tool/database layers stay authoritative.
    agent_unified_turn_interpreter_enabled: bool = True

    agent_semantic_router_enabled: bool = True
    agent_router_max_output_tokens: int = Field(default=1024, ge=256, le=4096)
    agent_router_history_messages: int = Field(default=8, ge=2, le=24)

    agent_flow_ttl_hours: int = Field(default=24, ge=1, le=168)
    agent_flow_interpreter_max_output_tokens: int = Field(
        default=1024,
        ge=256,
        le=4096,
    )
    agent_flow_interpreter_history_messages: int = Field(
        default=8,
        ge=2,
        le=24,
    )

    # Keep the environment value scalar so pydantic-settings never treats it as
    # a complex value and never attempts JSON decoding before app validation.
    # The public cors_origins property below normalizes plain, CSV, and JSON forms.
    cors_origins_raw: str = Field(default="", validation_alias="CORS_ORIGINS")

    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800

    # Analytics uses its own small connection pool so expensive reporting reads
    # cannot consume every connection needed by booking/customer operations.
    analytics_db_pool_size: int = Field(default=2, ge=1, le=20)
    analytics_db_max_overflow: int = Field(default=0, ge=0, le=20)
    analytics_db_pool_timeout_seconds: int = Field(default=3, ge=1, le=30)
    analytics_statement_timeout_ms: int = Field(default=10_000, ge=1_000, le=60_000)

    analytics_aggregate_cache_ttl_seconds: int = Field(default=5, ge=0, le=60)
    analytics_aggregate_cache_max_entries: int = Field(default=256, ge=0, le=2048)
    analytics_export_max_rows: int = Field(default=5_000, ge=100, le=50_000)
    analytics_export_max_bytes: int = Field(default=5_000_000, ge=100_000, le=50_000_000)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def require_psycopg_scheme(cls, value: str) -> str:
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError(
                "Database URLs must start with postgresql+psycopg:// for SQLAlchemy + Psycopg 3."
            )
        return value

    @field_validator("supabase_url")
    @classmethod
    def validate_supabase_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if not normalized.startswith("https://") or ".supabase.co" not in normalized:
            raise ValueError(
                "SUPABASE_URL must be the hosted https://<project-ref>.supabase.co URL."
            )
        return normalized

    @field_validator("supabase_publishable_key")
    @classmethod
    def validate_publishable_key(cls, value: str) -> str:
        if not value.startswith("sb_publishable_"):
            raise ValueError("Use a Supabase sb_publishable_... key for SUPABASE_PUBLISHABLE_KEY.")
        return value

    @field_validator("supabase_secret_key")
    @classmethod
    def validate_secret_key(cls, value: str) -> str:
        if not value.startswith("sb_secret_"):
            raise ValueError("Use a Supabase sb_secret_... key for SUPABASE_SECRET_KEY.")
        return value

    @field_validator(
        "openai_api_key",
        "openai_fallback_model",
        mode="before",
    )
    @classmethod
    def normalize_optional_value(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("openai_model", "openai_embedding_model")
    @classmethod
    def validate_required_model_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("OpenAI model names cannot be empty.")
        return normalized

    # Compatibility aliases keep the existing orchestration call sites stable
    # while the provider itself is now OpenAI-only. These all resolve to OpenAI
    # model IDs and never select another provider.
    @property
    def gemini_realtime_interpreter_model(self) -> str:
        return self.openai_model

    @property
    def gemini_realtime_interpreter_fallback_model(self) -> str | None:
        return self.openai_fallback_model

    @property
    def gemini_realtime_interpreter_emergency_model(self) -> None:
        return None

    @property
    def gemini_realtime_interpreter_thinking_level(self) -> str:
        return self.openai_reasoning_effort

    @property
    def gemini_realtime_composer_model(self) -> str:
        return self.openai_model

    @property
    def gemini_realtime_composer_fallback_model(self) -> str | None:
        return self.openai_fallback_model

    @property
    def gemini_realtime_composer_thinking_level(self) -> str:
        return self.openai_reasoning_effort

    @property
    def gemini_agent_model(self) -> str:
        return self.openai_model

    @property
    def gemini_agent_fallback_model(self) -> str | None:
        return self.openai_fallback_model

    @property
    def gemini_agent_thinking_level(self) -> str:
        return self.openai_reasoning_effort

    @property
    def gemini_router_model(self) -> str:
        return self.openai_model

    @property
    def gemini_router_fallback_model(self) -> str | None:
        return self.openai_fallback_model

    @property
    def gemini_router_thinking_level(self) -> str:
        return self.openai_reasoning_effort

    @property
    def gemini_flow_model(self) -> str:
        return self.openai_model

    @property
    def gemini_flow_fallback_model(self) -> str | None:
        return self.openai_fallback_model

    @property
    def gemini_flow_thinking_level(self) -> str:
        return self.openai_reasoning_effort

    @property
    def gemini_onboarding_model(self) -> str:
        return self.openai_model

    @property
    def gemini_onboarding_fallback_model(self) -> str | None:
        return self.openai_fallback_model

    @property
    def gemini_onboarding_thinking_level(self) -> str:
        return self.openai_reasoning_effort

    @property
    def gemini_onboarding_max_output_tokens(self) -> int:
        return self.openai_onboarding_max_output_tokens

    @property
    def gemini_utility_model(self) -> str:
        return self.openai_model

    @property
    def gemini_utility_thinking_level(self) -> str:
        return self.openai_reasoning_effort

    @property
    def gemini_embedding_model(self) -> str:
        return self.openai_embedding_model

    @property
    def cors_origins(self) -> list[str]:
        raw = self.cors_origins_raw.strip()
        if not raw:
            return []

        if raw.startswith("["):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "CORS_ORIGINS must be a valid JSON array or comma-separated string."
                ) from exc
            if not isinstance(decoded, list):
                raise ValueError("CORS_ORIGINS JSON value must be an array.")
            return [str(item).strip() for item in decoded if str(item).strip()]

        return [item.strip() for item in raw.split(",") if item.strip()]

    @property
    def supabase_auth_issuer(self) -> str:
        return f"{self.supabase_url}/auth/v1"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
