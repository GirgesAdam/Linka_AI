"""Verify the actual disposable PostgreSQL runtime/migrator role split."""
from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

PG_HOST = "127.0.0.1"
PG_PORT = 55443
PG_VERSION = "170006"

PAIRS = (
    (
        "0098",
        "vault_finance_0098_test",
        "0098_doctor_laser_weekday_exclusions",
        "FINANCE_COMPAT_0098_RUNTIME_DATABASE_URL",
        "FINANCE_COMPAT_0098_MIGRATION_DATABASE_URL",
        "postgres",
    ),
    (
        "0099",
        "vault_0099_financial_test",
        "0099_reception_main_vault_ledger",
        "FINANCE_COMPAT_0099_RUNTIME_DATABASE_URL",
        "FINANCE_COMPAT_0099_MIGRATION_DATABASE_URL",
        "tia_migrator",
    ),
)


def _required_url(name: str):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Required disposable connection URL is missing: {name}")
    return make_url(value)


def _identity(engine, expected_user: str) -> dict:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT
                  current_user AS current_user,
                  session_user AS session_user,
                  current_setting('server_version_num') AS server_version_num,
                  r.rolcanlogin,
                  r.rolsuper,
                  r.rolcreatedb,
                  r.rolcreaterole,
                  r.rolreplication,
                  r.rolbypassrls,
                  r.rolinherit,
                  has_table_privilege(current_user, 'public.alembic_version', 'SELECT')
                    AS can_read_alembic,
                  has_table_privilege(current_user, 'public.alembic_version', 'UPDATE')
                    AS can_update_alembic,
                  has_schema_privilege(current_user, 'public', 'CREATE')
                    AS can_create_in_public,
                  has_database_privilege(current_user, current_database(), 'CREATE')
                    AS can_create_database,
                  (
                    SELECT count(*)
                    FROM pg_catalog.pg_auth_members AS m
                    JOIN pg_catalog.pg_roles AS member ON member.oid = m.member
                    WHERE member.rolname = current_user
                  ) AS membership_count
                FROM pg_catalog.pg_roles AS r
                WHERE r.rolname = current_user
                """
            )
        ).mappings().one()
        assert row["current_user"] == expected_user, row
        assert row["session_user"] == expected_user, row
        assert row["server_version_num"] == PG_VERSION, row
        assert row["rolcanlogin"] is True, row
        assert row["rolsuper"] is False, row
        assert row["rolcreatedb"] is False, row
        assert row["rolcreaterole"] is False, row
        assert row["rolreplication"] is False, row
        assert row["rolinherit"] is False, row
        assert row["membership_count"] == 0, row
        return dict(row)


def verify_pair(label: str, database: str, revision: str, runtime_key: str, migrator_key: str, expenses_owner: str) -> None:
    runtime_url = _required_url(runtime_key)
    migrator_url = _required_url(migrator_key)

    expected_location = (PG_HOST, PG_PORT, database)
    assert (
        runtime_url.host,
        runtime_url.port,
        runtime_url.database,
    ) == expected_location, f"{runtime_key} does not target the approved disposable database"
    assert (
        migrator_url.host,
        migrator_url.port,
        migrator_url.database,
    ) == expected_location, f"{migrator_key} does not target the same disposable database"
    assert runtime_url.username == "tia_backend_runtime", runtime_key
    assert migrator_url.username == "tia_migrator", migrator_key
    assert runtime_url.username != migrator_url.username

    runtime_engine = create_engine(runtime_url, connect_args={"connect_timeout": 5})
    migrator_engine = create_engine(migrator_url, connect_args={"connect_timeout": 5})
    try:
        runtime_identity = _identity(runtime_engine, "tia_backend_runtime")
        assert runtime_identity["rolbypassrls"] is True, runtime_identity
        assert runtime_identity["can_read_alembic"] is False, runtime_identity
        assert runtime_identity["can_update_alembic"] is False, runtime_identity
        assert runtime_identity["can_create_in_public"] is False, runtime_identity
        assert runtime_identity["can_create_database"] is False, runtime_identity

        with runtime_engine.connect() as connection:
            assert connection.scalar(
                text("SELECT has_table_privilege(current_user, 'public.expenses', 'SELECT')")
            ) is True
            assert connection.scalar(
                text("SELECT has_table_privilege(current_user, 'public.expenses', 'UPDATE')")
            ) is True
            assert connection.scalar(
                text("SELECT has_table_privilege(current_user, 'public.expenses', 'DELETE')")
            ) is True
            assert connection.scalar(
                text("SELECT has_table_privilege(current_user, 'public.activity_events', 'INSERT')")
            ) is True
            assert connection.scalar(
                text("SELECT has_table_privilege(current_user, 'public.workspaces', 'SELECT')")
            ) is True

        migrator_identity = _identity(migrator_engine, "tia_migrator")
        assert migrator_identity["rolbypassrls"] is False, migrator_identity
        assert migrator_identity["can_read_alembic"] is True, migrator_identity
        assert migrator_identity["can_update_alembic"] is True, migrator_identity
        assert migrator_identity["can_create_in_public"] is True, migrator_identity
        assert migrator_identity["can_create_database"] is False, migrator_identity

        with migrator_engine.connect() as connection:
            actual_revision = connection.scalar(
                text("SELECT version_num FROM public.alembic_version")
            )
            assert actual_revision == revision, (label, actual_revision)
            version_owner = connection.scalar(
                text(
                    """
                    SELECT pg_catalog.pg_get_userbyid(c.relowner)
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = 'alembic_version'
                    """
                )
            )
            actual_expenses_owner = connection.scalar(
                text(
                    """
                    SELECT pg_catalog.pg_get_userbyid(c.relowner)
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = 'expenses'
                    """
                )
            )
            assert version_owner == "tia_migrator", (label, version_owner)
            assert actual_expenses_owner == expenses_owner, (label, actual_expenses_owner)

            expected_owners = {
                "payment_transactions": "postgres",
                "payment_allocations": "postgres",
                "appointments": "postgres",
                "workspaces": "postgres",
                "branches": "postgres",
                "users": "postgres",
                "workspace_members": "postgres",
                "doctor_laser_device_weekday_exclusions": "tia_migrator",
            }
            if label == "0099":
                expected_owners.update(
                    {
                        "vault_accounts": "tia_migrator",
                        "vault_branch_grants": "tia_migrator",
                        "vault_ledger_entries": "tia_migrator",
                        "vault_pending_payments": "tia_migrator",
                        "vault_transfers": "tia_migrator",
                    }
                )
            for table_name, expected_owner in expected_owners.items():
                actual_owner = connection.scalar(
                    text(
                        """
                        SELECT pg_catalog.pg_get_userbyid(c.relowner)
                        FROM pg_catalog.pg_class AS c
                        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public' AND c.relname = :table_name
                        """
                    ),
                    {"table_name": table_name},
                )
                assert actual_owner == expected_owner, (
                    label,
                    table_name,
                    actual_owner,
                    expected_owner,
                )

            if label == "0098":
                assert connection.scalar(
                    text(
                        "SELECT has_column_privilege(current_user, "
                        "'public.doctor_branches', 'workspace_id', 'REFERENCES')"
                    )
                ) is True
                assert connection.scalar(
                    text(
                        "SELECT has_column_privilege(current_user, "
                        "'public.clinic_laser_devices', 'device_key', 'REFERENCES')"
                    )
                ) is True
            else:
                for table_name in (
                    "payment_transactions",
                    "payment_allocations",
                    "appointments",
                ):
                    assert connection.scalar(
                        text(
                            "SELECT has_table_privilege(current_user, "
                            ":table_name, 'TRIGGER')"
                        ),
                        {"table_name": f"public.{table_name}"},
                    ) is True
                for table_name, column_name in (
                    ("workspaces", "id"),
                    ("branches", "id"),
                    ("users", "id"),
                    ("payment_transactions", "id"),
                    ("workspace_members", "user_id"),
                ):
                    assert connection.scalar(
                        text(
                            "SELECT has_column_privilege(current_user, "
                            ":table_name, :column_name, 'REFERENCES')"
                        ),
                        {
                            "table_name": f"public.{table_name}",
                            "column_name": column_name,
                        },
                    ) is True

        if label == "0099":
            with runtime_engine.connect() as connection:
                assert connection.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, "
                        "'public.vault_ledger_entries', 'SELECT')"
                    )
                ) is True
        else:
            with runtime_engine.connect() as connection:
                assert connection.scalar(
                    text("SELECT to_regclass('public.vault_ledger_entries')")
                ) is None
        print(
            f"FINANCE_ROLE_PAIR_{label}_PASS: same database={database}; "
            "runtime=tia_backend_runtime; migrator=tia_migrator; "
            "runtime cannot read alembic_version"
        )
    finally:
        runtime_engine.dispose()
        migrator_engine.dispose()


def main() -> None:
    # The job-level application/migration pair is deliberately the 0099 database.
    active_runtime = make_url(os.environ["DATABASE_URL"])
    active_migrator = make_url(os.environ["MIGRATION_DATABASE_URL"])
    assert (
        active_runtime.host,
        active_runtime.port,
        active_runtime.database,
    ) == (
        active_migrator.host,
        active_migrator.port,
        active_migrator.database,
    )
    assert active_runtime.database == "vault_0099_financial_test"
    assert active_runtime.username == "tia_backend_runtime"
    assert active_migrator.username == "tia_migrator"

    for pair in PAIRS:
        verify_pair(*pair)


if __name__ == "__main__":
    main()
