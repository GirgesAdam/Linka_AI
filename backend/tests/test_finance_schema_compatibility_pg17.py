"""Focused PostgreSQL 17.6 coverage for Finance on the pre/post-0099 schemas.

The 0098 fixture builds only the pre-Vault ORM schema; the local 0099 fallback
builds a disposable schema from metadata plus the two migration guard triggers.
CI instead uses its already-migrated disposable 0099 database. No Alembic
migration is run by this test module.
"""
from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.api.routes.finance import expense_list
from app.database.base import Base
from app.database.schema_capabilities import (
    VAULT_SCHEMA_TABLES,
    SchemaCapabilityError,
    vault_ledger_schema_available,
)
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.expense import Expense
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.models.vault import VaultAccount, VaultLedgerEntry
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.schemas.finance import ExpenseUpdate
from app.services.demo_reset import (
    DemoResetError,
    capture_demo_canonical_seed,
    reset_demo_workspace,
)
from app.services.finance import (
    FinanceOperationError,
    _require_unposted_expense,
    delete_expense,
    update_expense,
)

PG17_VERSION = "170006"
PG17_HOST = "127.0.0.1"
PG17_PORT = 55443
PG17_USER = "postgres"
DATABASE_0098 = "vault_finance_0098_test"
DATABASE_0099 = "vault_0099_financial_test"


def _engine_from_env(name: str, database: str):
    raw = os.environ.get(name)
    if not raw:
        if os.environ.get("FINANCE_COMPAT_PG17_REQUIRED") == "1":
            pytest.fail(f"Mandatory disposable PostgreSQL 17.6 URL missing: {name}")
        pytest.skip(f"Set {name} to the disposable local PostgreSQL 17.6 database")

    url = make_url(raw)
    if (url.host, url.port, url.database, url.username) != (
        PG17_HOST,
        PG17_PORT,
        database,
        PG17_USER,
    ):
        pytest.fail(f"Refusing non-disposable/non-local PostgreSQL target for {name}")
    if os.environ.get("ENVIRONMENT") != "test":
        pytest.fail("ENVIRONMENT=test is required for Finance compatibility tests")

    engine = create_engine(raw, connect_args={"connect_timeout": 5}, pool_pre_ping=True)
    with engine.connect() as connection:
        version = connection.scalar(text("SELECT current_setting('server_version_num')"))
        if version != PG17_VERSION:
            engine.dispose()
            pytest.fail(f"PostgreSQL 17.6 is required; found server_version_num={version}")
    return engine


def _database_is_empty(connection) -> bool:
    return int(
        connection.scalar(
            text(
                """
                SELECT count(*)
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                  AND c.relname NOT LIKE 'pg_%'
                """
            )
        )
        or 0
    ) == 0


def _create_alembic_marker(connection, revision: str) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE public.alembic_version (
                version_num varchar(64) NOT NULL PRIMARY KEY
            )
            """
        )
    )
    connection.execute(
        text("INSERT INTO public.alembic_version (version_num) VALUES (:revision)"),
        {"revision": revision},
    )


def _create_test_schema(connection, *, include_vault: bool) -> None:
    selected = [
        table for table in Base.metadata.tables.values()
        if include_vault or table.name not in VAULT_SCHEMA_TABLES
    ]
    metadata = MetaData()
    for table in selected:
        table.to_metadata(metadata)

    # Two existing model declarations publish the same index twice. Deduplicate
    # only identical definitions in this disposable metadata copy; production
    # models and repository schema definitions remain untouched.
    if not include_vault:
        for constraint in list(metadata.tables["expenses"].constraints):
            if constraint.name == "uq_expenses_workspace_id_id":
                metadata.tables["expenses"].constraints.remove(constraint)

    for table in metadata.tables.values():
        seen_indexes = {}
        for index in list(table.indexes):
            signature = (
                tuple(column.name for column in index.columns),
                index.unique,
            )
            previous = seen_indexes.get(index.name)
            if previous is None:
                seen_indexes[index.name] = signature
                continue
            if previous != signature:
                pytest.fail(f"Conflicting duplicate test index definition: {table.name}.{index.name}")
            table.indexes.remove(index)
    metadata.create_all(connection)


def _bootstrap_0098(connection) -> None:
    if not _database_is_empty(connection):
        pytest.fail("The 0098 compatibility database is not empty; refusing to reshape it")
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    _create_test_schema(connection, include_vault=False)
    _create_alembic_marker(connection, "0098_doctor_laser_weekday_exclusions")


def _bootstrap_local_0099(connection) -> None:
    if not _database_is_empty(connection):
        pytest.fail("The local 0099 compatibility database is not empty; refusing to reshape it")
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    _create_test_schema(connection, include_vault=True)
    _create_alembic_marker(connection, "0099_reception_main_vault_ledger")
    # Test-only equivalents of the two immutable migration guards. CI uses the
    # real Migration 0099 triggers already installed in its disposable database.
    connection.execute(
        text(
            """
            CREATE FUNCTION public.vault_reject_posted_expense_change()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM public.vault_ledger_entries
                WHERE workspace_id = OLD.workspace_id AND expense_id = OLD.id
              ) THEN
                RAISE EXCEPTION 'VAULT_POSTED_EXPENSE_IMMUTABLE';
              END IF;
              IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
              RETURN NEW;
            END;
            $$
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TRIGGER vault_protect_expense
            BEFORE UPDATE OR DELETE ON public.expenses
            FOR EACH ROW EXECUTE FUNCTION public.vault_reject_posted_expense_change()
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE FUNCTION public.vault_reject_entry_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'VAULT_LEDGER_IMMUTABLE';
            END;
            $$
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TRIGGER vault_ledger_immutable
            BEFORE UPDATE OR DELETE ON public.vault_ledger_entries
            FOR EACH ROW EXECUTE FUNCTION public.vault_reject_entry_mutation()
            """
        )
    )


@pytest.fixture(scope="module")
def pg17_0098_engine():
    engine = _engine_from_env("FINANCE_COMPAT_0098_DATABASE_URL", DATABASE_0098)
    try:
        with engine.begin() as connection:
            revision_table = connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
            if revision_table is None:
                _bootstrap_0098(connection)
            revision = connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            assert revision == "0098_doctor_laser_weekday_exclusions"
            assert connection.scalar(
                text(
                    """
                    SELECT EXISTS (
                      SELECT 1 FROM pg_catalog.pg_constraint
                      WHERE conrelid = 'public.expenses'::regclass
                        AND conname = 'uq_expenses_workspace_id_id'
                    )
                    """
                )
            ) is False
            for table_name in VAULT_SCHEMA_TABLES:
                assert connection.scalar(
                    text("SELECT to_regclass(:name)"),
                    {"name": f"public.{table_name}"},
                ) is None
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def pg17_0099_engine():
    engine = _engine_from_env("FINANCE_COMPAT_0099_DATABASE_URL", DATABASE_0099)
    try:
        with engine.begin() as connection:
            revision_table = connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
            if revision_table is None:
                _bootstrap_local_0099(connection)
            revision = connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            assert revision == "0099_reception_main_vault_ledger"
            assert connection.scalar(
                text(
                    """
                    SELECT EXISTS (
                      SELECT 1 FROM pg_catalog.pg_constraint
                      WHERE conrelid = 'public.expenses'::regclass
                        AND conname = 'uq_expenses_workspace_id_id'
                    )
                    """
                )
            ) is True
            for table_name in VAULT_SCHEMA_TABLES:
                assert connection.scalar(
                    text("SELECT to_regclass(:name)"),
                    {"name": f"public.{table_name}"},
                ) is not None
            with Session(bind=connection) as db:
                assert vault_ledger_schema_available(db) is True
        yield engine
    finally:
        engine.dispose()


def _create_finance_fixture(engine, *, demo: bool = False):
    suffix = uuid4().hex[:12]
    with Session(engine) as db:
        workspace = Workspace(
            name="F03 Finance Compatibility",
            slug=f"f03-finance-{suffix}",
            timezone="Africa/Cairo",
            is_demo=demo,
            is_active=True,
        )
        user = User(
            email=f"f03-{suffix}@example.invalid",
            auth_user_id=uuid4(),
        )
        db.add_all([workspace, user])
        db.flush()

        member = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role="admin",
            is_active=True,
        )
        branch = Branch(
            workspace_id=workspace.id,
            name="F03 Synthetic Branch",
            code=f"F03-{suffix}",
        )
        staff = Staff(
            workspace_id=workspace.id,
            first_name="F03",
            last_name="Synthetic",
        )
        service = Service(
            workspace_id=workspace.id,
            name="F03 Synthetic Service",
            slug=f"f03-service-{suffix}",
            category="facial",
            duration_minutes=30,
            price_minor=10000,
            currency="EGP",
        )
        booking = BookingSettings(
            workspace_id=workspace.id,
            slot_interval_minutes=30,
            minimum_notice_minutes=0,
            booking_horizon_days=90,
            cancellation_notice_minutes=60,
            allow_same_day_booking=True,
            require_confirmation=False,
        )
        db.add_all([member, branch, staff, service, booking])
        db.flush()
        doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id)
        db.add(doctor)
        db.flush()
        workspace.primary_branch_id = branch.id

        expense = Expense(
            workspace_id=workspace.id,
            created_by_user_id=user.id,
            title="F03 synthetic expense",
            category="supplies",
            expense_type="variable",
            amount_minor=1200,
            currency="EGP",
            incurred_on=date(2026, 11, 15),
            note="F03 regression fixture",
        )
        db.add(expense)
        db.commit()
        identity = SimpleNamespace(
            workspace_id=workspace.id,
            user_id=user.id,
            branch_id=branch.id,
            expense_id=expense.id,
        )

    if demo:
        with Session(engine) as db:
            capture_demo_canonical_seed(db, workspace_id=identity.workspace_id)
    return identity


def _post_expense_to_vault(engine, identity):
    with Session(engine) as db:
        account = VaultAccount(
            workspace_id=identity.workspace_id,
            kind="main_cash",
            branch_id=None,
            currency="EGP",
            opened_by_user_id=identity.user_id,
        )
        db.add(account)
        db.flush()
        entry = VaultLedgerEntry(
            workspace_id=identity.workspace_id,
            account_id=account.id,
            kind="expense",
            amount_minor=-1200,
            expense_id=identity.expense_id,
            actor_user_id=identity.user_id,
            note="F03 posted expense fixture",
        )
        db.add(entry)
        db.commit()
        return entry.id


def _read_expenses(engine, identity):
    with Session(engine) as db:
        workspace = db.get(Workspace, identity.workspace_id)
        return expense_list(
            access=SimpleNamespace(workspace=workspace),
            db=db,
            limit=200,
        )


def test_revision_0098_finance_list_update_and_delete(pg17_0098_engine):
    identity = _create_finance_fixture(pg17_0098_engine)
    with Session(pg17_0098_engine) as db:
        assert vault_ledger_schema_available(db) is False

    rows = _read_expenses(pg17_0098_engine, identity)
    assert len(rows) == 1
    assert rows[0].id == identity.expense_id
    assert rows[0].is_vault_linked is False

    with Session(pg17_0098_engine) as db:
        updated = update_expense(
            db,
            workspace_id=identity.workspace_id,
            expense_id=identity.expense_id,
            actor_user_id=identity.user_id,
            payload=ExpenseUpdate(amount_minor=1500),
        )
        db.commit()
        assert updated.amount_minor == 1500

    with Session(pg17_0098_engine) as db:
        delete_expense(
            db,
            workspace_id=identity.workspace_id,
            expense_id=identity.expense_id,
            actor_user_id=identity.user_id,
        )
        db.commit()
        assert db.get(Expense, identity.expense_id) is None


def test_revision_0098_demo_reset_skips_absent_vault_tables(pg17_0098_engine):
    identity = _create_finance_fixture(pg17_0098_engine, demo=True)
    with Session(pg17_0098_engine) as db:
        result = reset_demo_workspace(db, workspace_id=identity.workspace_id)
        assert result.reseeded_rows > 0
        assert db.scalar(
            select(Expense.id).where(
                Expense.workspace_id == identity.workspace_id,
                Expense.title == "F03 synthetic expense",
            )
        ) is not None
        assert db.scalar(
            text("SELECT to_regclass('public.vault_ledger_entries')")
        ) is None


def test_revision_0099_finance_list_detects_posted_expense(pg17_0099_engine):
    identity = _create_finance_fixture(pg17_0099_engine)
    _post_expense_to_vault(pg17_0099_engine, identity)

    rows = _read_expenses(pg17_0099_engine, identity)
    assert len(rows) == 1
    assert rows[0].id == identity.expense_id
    assert rows[0].is_vault_linked is True


def test_revision_0099_posted_expense_is_immutable_unposted_remains_manageable(pg17_0099_engine):
    posted = _create_finance_fixture(pg17_0099_engine)
    _post_expense_to_vault(pg17_0099_engine, posted)

    with Session(pg17_0099_engine) as db:
        with pytest.raises(FinanceOperationError, match="is posted"):
            update_expense(
                db,
                workspace_id=posted.workspace_id,
                expense_id=posted.expense_id,
                actor_user_id=posted.user_id,
                payload=ExpenseUpdate(amount_minor=1800),
            )
        with pytest.raises(FinanceOperationError, match="is posted"):
            delete_expense(
                db,
                workspace_id=posted.workspace_id,
                expense_id=posted.expense_id,
                actor_user_id=posted.user_id,
            )
        db.rollback()

    unposted = _create_finance_fixture(pg17_0099_engine)
    with Session(pg17_0099_engine) as db:
        updated = update_expense(
            db,
            workspace_id=unposted.workspace_id,
            expense_id=unposted.expense_id,
            actor_user_id=unposted.user_id,
            payload=ExpenseUpdate(amount_minor=1800),
        )
        db.commit()
        assert updated.amount_minor == 1800

    with Session(pg17_0099_engine) as db:
        delete_expense(
            db,
            workspace_id=unposted.workspace_id,
            expense_id=unposted.expense_id,
            actor_user_id=unposted.user_id,
        )
        db.commit()
        assert db.get(Expense, unposted.expense_id) is None


def test_revision_0099_demo_reset_keeps_posted_history_protected(pg17_0099_engine):
    identity = _create_finance_fixture(pg17_0099_engine, demo=True)
    _post_expense_to_vault(pg17_0099_engine, identity)

    with Session(pg17_0099_engine) as db:
        with pytest.raises(DemoResetError, match="immutable Vault financial history"):
            reset_demo_workspace(db, workspace_id=identity.workspace_id)
        db.rollback()
        assert db.get(Expense, identity.expense_id) is not None


def test_revision_0099_demo_reset_remains_compatible_without_postings(pg17_0099_engine):
    identity = _create_finance_fixture(pg17_0099_engine, demo=True)
    with Session(pg17_0099_engine) as db:
        account = VaultAccount(
            workspace_id=identity.workspace_id,
            kind="main_cash",
            branch_id=None,
            currency="EGP",
            opened_by_user_id=identity.user_id,
        )
        db.add(account)
        db.commit()

    with Session(pg17_0099_engine) as db:
        result = reset_demo_workspace(db, workspace_id=identity.workspace_id)
        assert result.reseeded_rows > 0
        assert db.scalar(
            select(VaultAccount.id).where(
                VaultAccount.workspace_id == identity.workspace_id
            )
        ) is None


def test_revision_0099_schema_drift_missing_ledger_fails_closed(pg17_0099_engine):
    identity = _create_finance_fixture(pg17_0099_engine)
    with pg17_0099_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("DROP TABLE public.vault_ledger_entries"))
            with Session(bind=connection) as db:
                with pytest.raises(SchemaCapabilityError, match="Vault schema is inconsistent"):
                    vault_ledger_schema_available(db)
                workspace = db.get(Workspace, identity.workspace_id)
                with pytest.raises(HTTPException) as response:
                    expense_list(access=SimpleNamespace(workspace=workspace), db=db, limit=200)
                assert response.value.status_code == 503
                with pytest.raises(FinanceOperationError, match="schema is inconsistent"):
                    update_expense(
                        db,
                        workspace_id=identity.workspace_id,
                        expense_id=identity.expense_id,
                        actor_user_id=identity.user_id,
                        payload=ExpenseUpdate(amount_minor=1900),
                    )
                with pytest.raises(FinanceOperationError, match="schema is inconsistent"):
                    delete_expense(
                        db,
                        workspace_id=identity.workspace_id,
                        expense_id=identity.expense_id,
                        actor_user_id=identity.user_id,
                    )
        finally:
            transaction.rollback()


def test_revision_0099_alembic_revision_rejects_missing_vault_artifacts(pg17_0099_engine):
    with pg17_0099_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("DROP TRIGGER vault_protect_expense ON public.expenses"))
            for table_name in (
                "vault_ledger_entries",
                "vault_pending_payments",
                "vault_branch_grants",
                "vault_transfers",
                "vault_accounts",
            ):
                connection.execute(text(f"DROP TABLE public.{table_name}"))
            with Session(bind=connection) as db:
                with pytest.raises(
                    SchemaCapabilityError,
                    match="Alembic revision and Vault schema capabilities do not match",
                ):
                    vault_ledger_schema_available(db)
        finally:
            if transaction.is_active:
                transaction.rollback()


def test_revision_0099_demo_reset_schema_drift_fails_before_mutation(pg17_0099_engine):
    identity = _create_finance_fixture(pg17_0099_engine, demo=True)
    with pg17_0099_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("DROP TABLE public.vault_ledger_entries"))
            with Session(bind=connection) as db:
                with pytest.raises(DemoResetError, match="Vault schema is inconsistent"):
                    reset_demo_workspace(db, workspace_id=identity.workspace_id)
                assert db.scalar(
                    select(Expense.id).where(
                        Expense.workspace_id == identity.workspace_id,
                        Expense.title == "F03 synthetic expense",
                    )
                ) is not None
        finally:
            if transaction.is_active:
                transaction.rollback()


def test_revision_0099_ledger_read_permission_failure_is_not_treated_as_missing(pg17_0099_engine):
    identity = _create_finance_fixture(pg17_0099_engine)
    with Session(pg17_0099_engine) as db:
        expense = db.get(Expense, identity.expense_id)

    role_name = f"f03_no_ledger_read_{uuid4().hex[:10]}"
    with pg17_0099_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE ROLE "{role_name}" NOLOGIN')
        connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO "{role_name}"')
        connection.exec_driver_sql(f'GRANT SELECT ON public.expenses TO "{role_name}"')

    try:
        with pg17_0099_engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text(f'SET LOCAL ROLE "{role_name}"'))
                with Session(bind=connection) as db:
                    with pytest.raises(DBAPIError, match="permission denied"):
                        _require_unposted_expense(db, expense)
            finally:
                transaction.rollback()
    finally:
        with pg17_0099_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP OWNED BY "{role_name}"')
            connection.exec_driver_sql(f'DROP ROLE "{role_name}"')


def test_revision_0099_database_triggers_keep_posted_financial_history_immutable(pg17_0099_engine):
    identity = _create_finance_fixture(pg17_0099_engine)
    entry_id = _post_expense_to_vault(pg17_0099_engine, identity)
    with pg17_0099_engine.begin() as connection:
        for statement in (
            "UPDATE public.vault_ledger_entries "
            "SET amount_minor = amount_minor - 1 WHERE id = :entry_id",
            "DELETE FROM public.vault_ledger_entries WHERE id = :entry_id",
        ):
            with pytest.raises(DBAPIError, match="VAULT_LEDGER_IMMUTABLE"):
                with connection.begin_nested():
                    connection.execute(text(statement), {"entry_id": entry_id})

        for statement in (
            "UPDATE public.expenses SET amount_minor = amount_minor + 1 WHERE id = :expense_id",
            "DELETE FROM public.expenses WHERE id = :expense_id",
        ):
            with pytest.raises(DBAPIError, match="VAULT_POSTED_EXPENSE_IMMUTABLE"):
                with connection.begin_nested():
                    connection.execute(text(statement), {"expense_id": identity.expense_id})
