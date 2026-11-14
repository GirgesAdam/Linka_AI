from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from alembic import command
from app.database import migration_timeouts
from app.database.migration_lock import (
    ALEMBIC_MIGRATION_LOCK_KEY,
    MigrationAdvisoryLockTimeout,
    acquire_migration_lock,
    migration_session_lock,
)
from app.database.migration_timeouts import (
    apply_transaction_local_timeouts,
    migration_timeout_guard,
)

BACKEND = Path(__file__).resolve().parents[1]
LOCAL_POSTGRES_HOSTS = {"localhost", "127.0.0.1", "::1"}


@pytest.fixture
def migration_lock_database() -> Iterator[str]:
    """Create an isolated database only on the disposable local CI PostgreSQL."""
    base_url = make_url(os.environ["MIGRATION_DATABASE_URL"])
    if base_url.host not in LOCAL_POSTGRES_HOSTS or base_url.database != "ci_db":
        pytest.fail(
            "Alembic lock integration tests only permit loopback PostgreSQL database ci_db; "
            "refusing to connect to any other database."
        )

    database_name = f"tia_alembic_lock_{uuid4().hex[:12]}"
    admin_url = base_url.set(database="postgres")
    admin_engine = create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        with admin_engine.connect() as admin:
            admin.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    except OperationalError as exc:
        admin_engine.dispose()
        ci_enabled = any(
            os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
            for name in ("CI", "GITHUB_ACTIONS")
        )
        if ci_enabled:
            pytest.fail(
                "Required disposable PostgreSQL database creation failed in CI; "
                f"migration-lock integration tests cannot be skipped: {exc}"
            )
        pytest.skip(
            "Disposable local PostgreSQL is unavailable for migration-lock tests: "
            f"{exc}"
        )

    target_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    try:
        yield target_url
    finally:
        try:
            with admin_engine.connect() as admin:
                admin.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity "
                        "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                ).all()
                admin.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
                )
        finally:
            admin_engine.dispose()


def _engine(database_url: str):
    return create_engine(database_url, poolclass=NullPool, pool_pre_ping=True)


def _try_acquire_lock(engine) -> bool:
    with engine.connect() as connection:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": ALEMBIC_MIGRATION_LOCK_KEY},
            ).scalar_one()
        )
        connection.commit()
        if acquired:
            unlocked = bool(
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": ALEMBIC_MIGRATION_LOCK_KEY},
                ).scalar_one()
            )
            connection.commit()
            assert unlocked
        return acquired


def _wait_for_file(path: Path, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    pytest.fail(f"Timed out waiting for subprocess marker: {path.name}")


def test_t1_independent_processes_serialize_on_the_same_postgres_lock(
    migration_lock_database: str,
    tmp_path: Path,
) -> None:
    """Two real Python processes use separate PostgreSQL sessions."""
    worker = dedent(
        """
        import sys
        import time
        from pathlib import Path

        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        from app.database.migration_lock import migration_session_lock

        mode, database_url, started_path, acquired_path, release_or_hold = sys.argv[1:]
        engine = create_engine(database_url, poolclass=NullPool)
        try:
            with engine.connect() as connection:
                Path(started_path).write_text("started", encoding="utf-8")
                with migration_session_lock(
                    connection,
                    timeout_seconds=5,
                    poll_interval_seconds=0.02,
                ):
                    Path(acquired_path).write_text("acquired", encoding="utf-8")
                    print(f"{mode}:ACQUIRED", flush=True)
                    if mode == "holder":
                        # Keep the lock until the parent observes the waiter's
                        # attempt, rather than relying on child startup timing.
                        release_file = Path(release_or_hold)
                        deadline = time.monotonic() + 15
                        while not release_file.exists():
                            if time.monotonic() > deadline:
                                raise TimeoutError("Timed out awaiting lock-holder release marker")
                            time.sleep(0.01)
                    else:
                        time.sleep(float(release_or_hold))
            print(f"{mode}:RELEASED", flush=True)
        finally:
            engine.dispose()
        """
    )
    environment = os.environ.copy()
    current_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(BACKEND), current_pythonpath) if part
    )

    holder_started = tmp_path / "holder.started"
    holder_acquired = tmp_path / "holder.acquired"
    holder_release = tmp_path / "holder.release"
    waiter_started = tmp_path / "waiter.started"
    waiter_acquired = tmp_path / "waiter.acquired"

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            worker,
            "holder",
            migration_lock_database,
            str(holder_started),
            str(holder_acquired),
            str(holder_release),
        ],
        cwd=BACKEND,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    waiter = None
    try:
        _wait_for_file(holder_acquired)
        waiter = subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker,
                "waiter",
                migration_lock_database,
                str(waiter_started),
                str(waiter_acquired),
                "0.05",
            ],
            cwd=BACKEND,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_for_file(waiter_started)
        time.sleep(0.15)

        assert not waiter_acquired.exists(), (
            "The second process acquired the migration lock while the first process held it."
        )
        assert holder.poll() is None
        assert waiter.poll() is None

        # Explicit release makes this a genuine overlap test on slow runners,
        # including Windows, without weakening the lock exclusion assertion.
        holder_release.write_text("release", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        waiter_stdout, waiter_stderr = waiter.communicate(timeout=10)
        assert holder.returncode == 0, holder_stderr
        assert waiter.returncode == 0, waiter_stderr
        assert "holder:ACQUIRED" in holder_stdout
        assert "holder:RELEASED" in holder_stdout
        assert "waiter:ACQUIRED" in waiter_stdout
        assert "waiter:RELEASED" in waiter_stdout
        assert waiter_acquired.exists()
    finally:
        holder_release.write_text("release", encoding="utf-8")
        for process in (holder, waiter):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_t2_session_lock_survives_alembic_autocommit_block(
    migration_lock_database: str,
) -> None:
    engine = _engine(migration_lock_database)
    table_name = f"lock_autocommit_{uuid4().hex[:12]}"
    index_name = f"ix_lock_autocommit_{uuid4().hex[:12]}"
    try:
        with engine.connect() as connection:
            with migration_session_lock(connection):
                migration_context = MigrationContext.configure(connection=connection)
                with migration_context.begin_transaction():
                    connection.execute(
                        text(f'CREATE TABLE "{table_name}" (id integer NOT NULL)')
                    )
                    with migration_context.autocommit_block():
                        connection.execute(
                            text(
                                f'CREATE INDEX CONCURRENTLY "{index_name}" '
                                f'ON "{table_name}" (id)'
                            )
                        )

                    # The concurrent-index DDL crossed Alembic's real AUTOCOMMIT
                    # boundary. A different PostgreSQL session must still be blocked.
                    assert not _try_acquire_lock(engine)

                index_exists = connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_indexes "
                        "WHERE schemaname = current_schema() AND indexname = :index_name"
                        ")"
                    ),
                    {"index_name": index_name},
                ).scalar_one()
                assert index_exists is True

        assert _try_acquire_lock(engine) is True
    finally:
        engine.dispose()


def test_t3_migration_exception_releases_lock_without_masking_original_error(
    migration_lock_database: str,
) -> None:
    engine = _engine(migration_lock_database)
    try:
        with engine.connect() as connection:
            with pytest.raises(RuntimeError, match="synthetic migration failure"):
                with migration_session_lock(connection):
                    with connection.begin():
                        connection.execute(text("SELECT 1"))
                        raise RuntimeError("synthetic migration failure")

        assert _try_acquire_lock(engine) is True
    finally:
        engine.dispose()


def test_t3b_original_migration_error_survives_unlock_failure(
    migration_lock_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.database.migration_lock as migration_lock

    engine = _engine(migration_lock_database)
    original_release = migration_lock.release_migration_lock

    def fail_release(*args, **kwargs) -> None:
        raise RuntimeError("synthetic unlock failure")

    connection = engine.connect()
    try:
        try:
            monkeypatch.setattr(migration_lock, "release_migration_lock", fail_release)
            with pytest.raises(ValueError, match="original migration error"):
                with migration_lock.migration_session_lock(connection):
                    raise ValueError("original migration error")
        finally:
            monkeypatch.setattr(migration_lock, "release_migration_lock", original_release)
            # NullPool closes the physical PostgreSQL session and releases the lock.
            connection.close()

        assert _try_acquire_lock(engine) is True
    finally:
        engine.dispose()


def test_t4_lock_wait_timeout_fails_with_clear_error(
    migration_lock_database: str,
) -> None:
    engine = _engine(migration_lock_database)
    try:
        with engine.connect() as owner:
            with migration_session_lock(owner):
                with engine.connect() as waiter:
                    started = time.monotonic()
                    with pytest.raises(
                        MigrationAdvisoryLockTimeout,
                        match="Timed out after 0.15 seconds waiting for the shared PostgreSQL",
                    ):
                        acquire_migration_lock(
                            waiter,
                            timeout_seconds=0.15,
                            poll_interval_seconds=0.01,
                        )
                    assert time.monotonic() - started < 2.0
                    assert not waiter.in_transaction()
    finally:
        engine.dispose()


def test_t5_sequential_alembic_upgrades_read_current_head_without_reapplying(
    migration_lock_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migration_lock_database)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_lock_database)
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    expected_head = ScriptDirectory.from_config(config).get_current_head()
    engine = _engine(migration_lock_database)
    try:
        # The first upgrade exercises the historical 0043 AUTOCOMMIT block.
        command.upgrade(config, "0096_patient_customer_id")
        with engine.connect() as connection:
            first_revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert first_revision == "0096_patient_customer_id"
        assert _try_acquire_lock(engine) is True

        # Rehearse the exact production target as a separate Alembic run.
        command.upgrade(config, "0097_member_page_permissions")
        with engine.connect() as connection:
            second_revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            columns = {
                row[0]: (row[1], row[2], row[3])
                for row in connection.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'workspace_members' "
                        "AND column_name IN ('page_permissions', 'permissions_version')"
                    )
                ).all()
            }

        assert second_revision == "0097_member_page_permissions"
        assert columns == {
            "page_permissions": ("jsonb", "YES", None),
            "permissions_version": ("integer", "NO", "0"),
        }
        assert _try_acquire_lock(engine) is True

        command.upgrade(config, "head")
        with engine.connect() as connection:
            third_revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert third_revision == expected_head == "0099_reception_main_vault_ledger"
        assert third_revision != second_revision
        # Upgrading an already-current database must not reapply revisions 0098 or 0099.
        command.upgrade(config, "head")
        with engine.connect() as connection:
            final_revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            new_table_count = connection.execute(
                text("SELECT count(*) FROM information_schema.tables "
                     "WHERE table_schema = current_schema() "
                     "AND table_name = 'doctor_laser_device_weekday_exclusions'")
            ).scalar_one()
        assert final_revision == third_revision
        assert new_table_count == 1

        with engine.connect() as connection:
            concurrent_index_exists = connection.execute(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_indexes "
                    "WHERE schemaname = current_schema() "
                    "AND indexname = 'ix_payment_transactions_workspace_currency_created'"
                    ")"
                )
            ).scalar_one()
        assert concurrent_index_exists is True
    finally:
        engine.dispose()


def test_t6_closing_lock_owner_connection_releases_postgres_session_lock(
    migration_lock_database: str,
) -> None:
    engine = _engine(migration_lock_database)
    try:
        owner = engine.connect()
        acquire_migration_lock(owner)
        assert not owner.in_transaction()
        assert not _try_acquire_lock(engine)

        # NullPool makes Connection.close() close the physical DBAPI session.
        owner.close()

        with engine.connect() as next_owner:
            with migration_session_lock(next_owner, timeout_seconds=0.5):
                assert not _try_acquire_lock(engine)
        assert _try_acquire_lock(engine) is True
    finally:
        engine.dispose()


def _timeout_settings(connection) -> tuple[str, str]:
    values = connection.exec_driver_sql(
        "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
    ).one()
    return str(values[0]), str(values[1])


def test_t7_transaction_local_timeouts_are_scoped_and_reset_after_commit_and_rollback(
    migration_lock_database: str,
) -> None:
    assert migration_timeouts.MIGRATION_LOCK_TIMEOUT == "5s"
    assert migration_timeouts.MIGRATION_STATEMENT_TIMEOUT == "60s"

    engine = _engine(migration_lock_database)
    try:
        with engine.connect() as connection:
            baseline = _timeout_settings(connection)
            connection.rollback()

            with migration_timeout_guard(connection):
                with connection.begin():
                    apply_transaction_local_timeouts(connection)
                    assert _timeout_settings(connection) == ("5s", "1min")

                    # A different PostgreSQL session cannot see transaction-local settings.
                    with engine.connect() as observer:
                        assert _timeout_settings(observer) == baseline

                # Commit resets both settings on the same physical connection.
                assert _timeout_settings(connection) == baseline
                connection.rollback()

                with pytest.raises(RuntimeError, match="rollback sentinel"):
                    with connection.begin():
                        apply_transaction_local_timeouts(connection)
                        assert _timeout_settings(connection) == ("5s", "1min")
                        raise RuntimeError("rollback sentinel")

                # Rollback also resets both settings on the same connection.
                assert _timeout_settings(connection) == baseline
                connection.rollback()
    finally:
        engine.dispose()


def test_t8_timeout_guard_covers_alembic_autocommit_block_and_restores_prior_settings(
    migration_lock_database: str,
) -> None:
    engine = _engine(migration_lock_database)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET lock_timeout = '2s'")
            connection.exec_driver_sql("SET statement_timeout = '3s'")
            connection.commit()
            prior_settings = ("2s", "3s")

            with migration_timeout_guard(connection):
                migration_context = MigrationContext.configure(connection=connection)
                with migration_context.begin_transaction():
                    apply_transaction_local_timeouts(connection)
                    assert _timeout_settings(connection) == ("5s", "1min")

                    with migration_context.autocommit_block():
                        assert (
                            connection.get_execution_options().get("isolation_level")
                            == "AUTOCOMMIT"
                        )
                        # AUTOCOMMIT has no transaction for SET LOCAL, so the
                        # guard temporarily protects this same session and restores it.
                        assert _timeout_settings(connection) == ("5s", "1min")
                        with engine.connect() as observer:
                            assert _timeout_settings(observer) == ("0", "0")

                    # Alembic's post-AUTOCOMMIT transaction receives SET LOCAL again.
                    assert _timeout_settings(connection) == ("5s", "1min")

            # The outer transaction commit restores the original session values.
            assert _timeout_settings(connection) == prior_settings
            connection.rollback()
    finally:
        engine.dispose()


def _alembic_config_for_database(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Config:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", database_url)
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    return config


def _assert_0096_without_permission_columns(engine) -> None:
    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        permission_column_count = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'workspace_members' "
                "AND column_name IN ('page_permissions', 'permissions_version')"
            )
        ).scalar_one()

    assert revision == "0096_patient_customer_id"
    assert permission_column_count == 0


def test_t9_alembic_lock_timeout_aborts_0097_and_releases_advisory_lock(
    migration_lock_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _alembic_config_for_database(migration_lock_database, monkeypatch)
    command.upgrade(config, "0096_patient_customer_id")

    monkeypatch.setattr(migration_timeouts, "MIGRATION_LOCK_TIMEOUT", "150ms")
    engine = _engine(migration_lock_database)
    blocker = engine.connect()
    blocker_transaction = blocker.begin()
    try:
        blocker.exec_driver_sql(
            "LOCK TABLE workspace_members IN ACCESS EXCLUSIVE MODE"
        )

        started = time.monotonic()
        with pytest.raises(OperationalError) as failure:
            command.upgrade(config, "0097_member_page_permissions")

        assert getattr(failure.value.orig, "sqlstate", None) == "55P03"
        assert time.monotonic() - started < 2.0
        _assert_0096_without_permission_columns(engine)
        assert _try_acquire_lock(engine) is True

        # Once the independent blocker is gone, the same migration can run cleanly.
        blocker_transaction.rollback()
        blocker.close()
        monkeypatch.setattr(migration_timeouts, "MIGRATION_LOCK_TIMEOUT", "5s")
        command.upgrade(config, "0097_member_page_permissions")
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == "0097_member_page_permissions"
        assert _try_acquire_lock(engine) is True
    finally:
        if blocker.in_transaction():
            blocker_transaction.rollback()
        blocker.close()
        engine.dispose()


def test_t10_alembic_statement_timeout_aborts_0097_and_releases_advisory_lock(
    migration_lock_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _alembic_config_for_database(migration_lock_database, monkeypatch)
    command.upgrade(config, "0096_patient_customer_id")
    engine = _engine(migration_lock_database)
    trigger_created = False

    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE FUNCTION public.test_migration_statement_timeout_sleep()
                RETURNS event_trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    PERFORM pg_sleep(1);
                END;
                $$
                """
            )
            connection.exec_driver_sql(
                """
                CREATE EVENT TRIGGER test_migration_statement_timeout_sleep
                ON ddl_command_start
                WHEN TAG IN ('ALTER TABLE')
                EXECUTE FUNCTION public.test_migration_statement_timeout_sleep()
                """
            )
        trigger_created = True

        monkeypatch.setattr(
            migration_timeouts,
            "MIGRATION_STATEMENT_TIMEOUT",
            "150ms",
        )
        with pytest.raises(OperationalError) as failure:
            command.upgrade(config, "0097_member_page_permissions")

        assert getattr(failure.value.orig, "sqlstate", None) == "57014"
        _assert_0096_without_permission_columns(engine)
        assert _try_acquire_lock(engine) is True
    finally:
        if trigger_created:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "DROP EVENT TRIGGER IF EXISTS "
                    "test_migration_statement_timeout_sleep"
                )
                connection.exec_driver_sql(
                    "DROP FUNCTION IF EXISTS "
                    "public.test_migration_statement_timeout_sleep()"
                )

    # Remove the injected DDL delay and prove a subsequent migration succeeds.
    monkeypatch.setattr(
        migration_timeouts,
        "MIGRATION_STATEMENT_TIMEOUT",
        "60s",
    )
    command.upgrade(config, "0097_member_page_permissions")
    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert revision == "0097_member_page_permissions"
    assert _try_acquire_lock(engine) is True
    engine.dispose()
