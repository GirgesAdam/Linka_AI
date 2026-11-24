"""Mandatory upgrade from real 0099 and bank-deposit runtime on disposable PG17."""
from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.models.branch import Branch
from app.models.user import User
from app.models.vault import VaultAccount, VaultLedgerEntry, VaultTransfer
from app.models.workspace import Workspace
from app.services.vault import (
    VaultError,
    activate_account,
    main_treasury_summary,
    record_bank_deposit,
)


def test_actual_0099_to_0100_reception_and_bank_deposits():
    raw = os.environ.get("VAULT_0100_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("Dedicated 0100 CI gate provides the disposable migration target.")
    url = make_url(raw)
    assert (url.host, url.port, url.database, url.username) == (
        "127.0.0.1", 55432, "package_service_ci", "package_ci_user",
    )
    engine = create_engine(raw)
    sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        with sessions.begin() as db:
            assert db.scalar(text("SELECT current_setting('server_version_num')")) == "170006"
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0099_reception_main_vault_ledger"
            suffix = uuid4().hex[:12]
            workspace = Workspace(name="Synthetic 0100", slug="vault-0100-" + suffix)
            demo = Workspace(name="Synthetic Demo", slug="vault-demo-" + suffix, is_demo=True)
            actor = User(email="vault-0100-" + suffix + "@example.invalid")
            db.add_all([workspace, demo, actor])
            db.flush()
            wid, did, uid = workspace.id, demo.id, actor.id
            prior = Branch(workspace_id=wid, name="Existing", code="existing")
            inactive = Branch(workspace_id=wid, name="Inactive", code="inactive", is_active=False)
            demo_branch = Branch(workspace_id=did, name="Demo", code="demo")
            db.add_all([prior, inactive, demo_branch])
            db.flush()
            prior_id, inactive_id = prior.id, inactive.id
            activate_account(db, workspace_id=wid, actor_user_id=uid, kind="main_cash",
                             branch_id=None, opening_balance_minor=10000, currency="EGP")
            activate_account(db, workspace_id=wid, actor_user_id=uid, kind="main_visa",
                             branch_id=None, opening_balance_minor=2000, currency="EGP")
            assert db.scalar(select(func.count(VaultAccount.id)).where(
                VaultAccount.workspace_id == wid, VaultAccount.kind == "reception",
            )) == 0
        engine.dispose()
        env = {**os.environ, "DATABASE_URL": raw, "MIGRATION_DATABASE_URL": raw}
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0100_vault_bank_reception"],
                       env=env, check=True, timeout=120)
        with sessions.begin() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0100_vault_bank_reception"
            reception = db.scalar(select(VaultAccount).where(
                VaultAccount.workspace_id == wid, VaultAccount.branch_id == prior_id,
            ))
            assert reception is not None
            assert reception.opened_by_user_id is None
            assert db.scalar(select(func.count(VaultLedgerEntry.id)).where(
                VaultLedgerEntry.account_id == reception.id,
            )) == 0
            assert db.scalar(select(func.count(VaultAccount.id)).where(
                VaultAccount.workspace_id == did,
            )) == 0
            assert db.scalar(select(func.count(VaultAccount.id)).where(
                VaultAccount.branch_id == inactive_id,
            )) == 0
            db.get(Branch, inactive_id).is_active = True
            new_branch = Branch(workspace_id=wid, name="New", code="new")
            db.add(new_branch)
            db.flush()
            assert db.scalar(select(func.count(VaultAccount.id)).where(
                VaultAccount.workspace_id == wid, VaultAccount.kind == "reception",
            )) == 3
            bank = activate_account(db, workspace_id=wid, actor_user_id=uid, kind="main_bank",
                                    branch_id=None, opening_balance_minor=3000, currency="EGP")
            bank_id = bank.id
        with sessions.begin() as db:
            bank = activate_account(db, workspace_id=wid, actor_user_id=uid, kind="main_bank",
                                    branch_id=None, opening_balance_minor=3000, currency="EGP")
            assert bank.id == bank_id
            before = main_treasury_summary(db, workspace_id=wid, workspace_timezone="Africa/Cairo")
            assert (before["cash_minor"], before["bank_minor"], before["total_recorded_minor"]) == (10000, 5000, 15000)
            assert before["bank_activated"]
        def deposit(key, amount):
            try:
                with sessions.begin() as db:
                    return record_bank_deposit(db, workspace_id=wid, actor_user_id=uid,
                                               amount_minor=amount, idempotency_key=key).id
            except VaultError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(deposit, "same-key", 4000)
            b = pool.submit(deposit, "same-key", 4000)
            first, second = a.result(timeout=30), b.result(timeout=30)
        assert first is not None and first == second
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(deposit, "race-a", 4000)
            b = pool.submit(deposit, "race-b", 4000)
            results = [a.result(timeout=30), b.result(timeout=30)]
        assert sum(value is not None for value in results) == 1
        with sessions.begin() as db:
            after = main_treasury_summary(db, workspace_id=wid, workspace_timezone="Africa/Cairo")
            assert (after["cash_minor"], after["bank_minor"], after["total_recorded_minor"]) == (2000, 13000, 15000)
            headers = list(db.scalars(select(VaultTransfer).where(VaultTransfer.workspace_id == wid)))
            assert len(headers) == 2
            for header in headers:
                assert header.branch_id is None
                entries = list(db.scalars(select(VaultLedgerEntry).where(
                    VaultLedgerEntry.workspace_id == wid, VaultLedgerEntry.transfer_id == header.id,
                )))
                assert sorted(row.amount_minor for row in entries) == [-4000, 4000]
                assert {row.kind for row in entries} == {"transfer_out", "transfer_in"}
            print("VAULT_0100_REAL_MIGRATION_AUTO_RECEPTION_BANK_RETRY_CONCURRENCY_PASS")
        assert deposit("same-key", 1000) is None
        assert deposit("overdraw", 2001) is None
        with pytest.raises(DBAPIError, match="VAULT_BANK_TRANSFER_REQUIRES_MATCHING_LEDGER_SIDES"):
            with sessions.begin() as db:
                db.add(VaultTransfer(workspace_id=wid, branch_id=None, amount_minor=100,
                                     currency="EGP", idempotency_key="unpaired", actor_user_id=uid))
    finally:
        engine.dispose()
