from logging.config import fileConfig
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import engine_from_config, pool

from alembic import context
from app import models  # noqa: F401
from app.database.base import Base
from app.database.migration_lock import migration_session_lock
from app.database.migration_timeouts import (
    apply_transaction_local_timeouts,
    migration_timeout_guard,
)


class MigrationSettings(BaseSettings):
    migration_database_url: str

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

migration_database_url = MigrationSettings().migration_database_url
config.set_main_option(
    "sqlalchemy.url",
    migration_database_url.replace("%", "%%"),
)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=migration_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # The session-level lock is held across Alembic transactions and
        # PostgreSQL AUTOCOMMIT blocks (e.g. CREATE INDEX CONCURRENTLY).
        # It serializes independent Railway services sharing this database.
        with migration_session_lock(connection):
            with migration_timeout_guard(connection):
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    compare_type=True,
                    compare_server_default=True,
                )

                with context.begin_transaction():
                    apply_transaction_local_timeouts(connection)
                    context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
