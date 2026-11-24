"""Runtime-safe capability checks for the optional Reception Vault schema.

The backend runtime deliberately does not need SELECT on Alembic's control
table. Detect the supported schema shapes from PostgreSQL catalogs and use
Migration 0099's independent expenses constraint as a marker when Alembic is
not readable. Partial shapes are schema drift and must fail closed.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

VAULT_SCHEMA_TABLES = frozenset(
    {
        "vault_accounts",
        "vault_branch_grants",
        "vault_ledger_entries",
        "vault_pending_payments",
        "vault_transfers",
    }
)

_PRE_VAULT_REVISION = "0098_doctor_laser_weekday_exclusions"
_VAULT_REVISION = "0099_reception_main_vault_ledger"
_VAULT_BANK_REVISION = "0100_vault_bank_reception"

_VAULT_SCHEMA_STATE = text(
    """
    SELECT
      EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'expenses'
          AND c.relkind IN ('r', 'p')
      ) AS expenses_exists,
      EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint AS con
        JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'expenses'
          AND con.conname = 'uq_expenses_workspace_id_id'
          AND con.contype = 'u'
          AND con.convalidated
          AND (
            SELECT array_agg(a.attname ORDER BY key.ordinality)
            FROM unnest(con.conkey) WITH ORDINALITY AS key(attnum, ordinality)
            JOIN pg_catalog.pg_attribute AS a
              ON a.attrelid = con.conrelid AND a.attnum = key.attnum
          ) = ARRAY['workspace_id', 'id']::name[]
      ) AS vault_migration_marker,
      EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'vault_accounts'
          AND c.relkind IN ('r', 'p')
      ) AS vault_accounts,
      EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'vault_branch_grants'
          AND c.relkind IN ('r', 'p')
      ) AS vault_branch_grants,
      EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'vault_ledger_entries'
          AND c.relkind IN ('r', 'p')
      ) AS vault_ledger_entries,
      EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'vault_pending_payments'
          AND c.relkind IN ('r', 'p')
      ) AS vault_pending_payments,
      EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'vault_transfers'
          AND c.relkind IN ('r', 'p')
      ) AS vault_transfers,
      EXISTS (
        SELECT 1
        FROM pg_catalog.pg_trigger AS t
        JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'expenses'
          AND t.tgname = 'vault_protect_expense'
          AND NOT t.tgisinternal AND t.tgenabled IN ('O', 'A')
      ) AS posted_expense_guard,
      EXISTS (
        SELECT 1
        FROM pg_catalog.pg_trigger AS t
        JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'vault_ledger_entries'
          AND t.tgname = 'vault_ledger_immutable'
          AND NOT t.tgisinternal AND t.tgenabled IN ('O', 'A')
      ) AS ledger_immutable_guard,
      EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'alembic_version'
          AND c.relkind IN ('r', 'p')
      ) AS alembic_version_exists,
      has_table_privilege(
        current_user, 'public.alembic_version', 'SELECT'
      ) AS can_read_alembic_version
    """
)


class SchemaCapabilityError(RuntimeError):
    """The database does not match a supported Vault schema shape."""


def vault_ledger_schema_available(db: Session) -> bool:
    """Return False for intact pre-0099, True for intact 0099, else fail closed.

    Catalog reads identify the independent Migration 0099 marker, all five
    Vault tables, and both enabled immutable triggers without requiring
    table-level financial privileges. If the current role already has SELECT
    on Alembic's version table, its single revision is also cross-checked.
    No permission is added to the runtime role.

    SQL/catalog permission or connection errors are deliberately not caught.
    """
    state = db.execute(_VAULT_SCHEMA_STATE).mappings().one()
    if not state["expenses_exists"]:
        raise SchemaCapabilityError(
            "Expenses table is missing; Finance schema capabilities cannot be verified."
        )
    if not state["alembic_version_exists"]:
        raise SchemaCapabilityError(
            "Alembic version metadata is missing; Vault schema cannot be verified."
        )

    table_state = {
        name: bool(state[name])
        for name in sorted(VAULT_SCHEMA_TABLES)
    }
    present = {name for name, exists in table_state.items() if exists}
    expense_guard = bool(state["posted_expense_guard"])
    ledger_guard = bool(state["ledger_immutable_guard"])
    migration_marker = bool(state["vault_migration_marker"])

    if not migration_marker and not present and not expense_guard and not ledger_guard:
        available = False
    elif (
        migration_marker
        and present == VAULT_SCHEMA_TABLES
        and expense_guard
        and ledger_guard
    ):
        available = True
    else:
        missing = sorted(VAULT_SCHEMA_TABLES - present)
        raise SchemaCapabilityError(
            "Vault schema is inconsistent; Finance and Demo Reset operations are blocked "
            f"(migration_marker={migration_marker}, missing_tables={missing}, "
            f"posted_expense_guard={expense_guard}, "
            f"ledger_immutable_guard={ledger_guard})."
        )

    if state["can_read_alembic_version"]:
        revisions = list(db.scalars(text("SELECT version_num FROM public.alembic_version")))
        if len(revisions) != 1:
            raise SchemaCapabilityError(
                "Alembic revision state is ambiguous; Vault schema cannot be verified."
            )
        revision = revisions[0]
        if revision == _PRE_VAULT_REVISION and not migration_marker and not available:
            return False
        if revision in (_VAULT_REVISION, _VAULT_BANK_REVISION) and migration_marker and available:
            return True
        raise SchemaCapabilityError(
            "Alembic revision and Vault schema capabilities do not match; "
            "Finance and Demo Reset operations are blocked."
        )

    return available
