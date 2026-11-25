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

from app.database.schema_capabilities import vault_ledger_schema_available
from app.models.branch import Branch
from app.models.patient import Patient
from app.models.payment_transaction import PaymentTransaction
from app.models.user import User
from app.models.vault import VaultAccount, VaultLedgerEntry, VaultPendingPayment, VaultTransfer
from app.models.workspace import Workspace
from app.services.vault import (
    VaultError,
    activate_account,
    main_treasury_summary,
    record_bank_deposit,
)
from app.services.vault_pending import review_preactivation_electronic


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
            legacy = Branch(workspace_id=wid, name="Existing Activated", code="legacy")
            db.add_all([prior, inactive, demo_branch, legacy])
            db.flush()
            prior_id, inactive_id = prior.id, inactive.id
            legacy_id = activate_account(db, workspace_id=wid, actor_user_id=uid, kind="reception",
                                         branch_id=legacy.id, opening_balance_minor=700, currency="EGP").id
            activate_account(db, workspace_id=wid, actor_user_id=uid, kind="main_cash",
                             branch_id=None, opening_balance_minor=10000, currency="EGP")
            activate_account(db, workspace_id=wid, actor_user_id=uid, kind="main_visa",
                             branch_id=None, opening_balance_minor=2000, currency="EGP")
            assert db.scalar(select(func.count(VaultAccount.id)).where(
                VaultAccount.workspace_id == wid, VaultAccount.kind == "reception",
            )) == 1
        engine.dispose()
        env = {**os.environ, "DATABASE_URL": raw, "MIGRATION_DATABASE_URL": raw}
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0100_vault_bank_reception"],
                       env=env, check=True, timeout=120)
        with sessions.begin() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0100_vault_bank_reception"
            assert vault_ledger_schema_available(db) is True
            reception = db.scalar(select(VaultAccount).where(
                VaultAccount.workspace_id == wid, VaultAccount.branch_id == prior_id,
            ))
            assert reception is not None
            assert reception.opened_by_user_id is None
            assert db.scalar(select(func.sum(VaultLedgerEntry.amount_minor)).where(
                VaultLedgerEntry.account_id == legacy_id,
            )) == 700
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
            )) == 4
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

        # Both electronic channels must share the consolidated opening cap.
        with sessions.begin() as db:
            clinic = Workspace(name="Synthetic Bank Review", slug="vault-review-" + uuid4().hex)
            db.add(clinic)
            db.flush()
            review_wid = clinic.id
            patient = Patient(workspace_id=review_wid, first_name="Synthetic")
            db.add(patient)
            db.flush()
            patient_id = patient.id
            receipts = [
                PaymentTransaction(workspace_id=review_wid, patient_id=patient_id,
                                   transaction_type="payment", amount_minor=2000, currency="EGP",
                                   payment_method=method, source="staff", created_by_user_id=uid)
                for method in ("visa", "instapay")
            ]
            db.add_all(receipts)
            db.flush()
            pending_ids = list(db.scalars(select(VaultPendingPayment.id).where(
                VaultPendingPayment.workspace_id == review_wid,
            )))
            assert len(pending_ids) == 2
        with sessions.begin() as db:
            bank = activate_account(db, workspace_id=review_wid, actor_user_id=uid, kind="main_bank",
                                    branch_id=None, opening_balance_minor=3000, currency="EGP")
            review_bank_id = bank.id
        with sessions.begin() as db:
            reviewed = review_preactivation_electronic(
                db, workspace_id=review_wid, pending_id=pending_ids[0], actor_user_id=uid,
                outcome="included_in_opening", explanation="Synthetic verified bank opening",
            )
            assert reviewed.reviewed_account_id == review_bank_id
        with pytest.raises(VaultError, match="does not cover"):
            with sessions.begin() as db:
                review_preactivation_electronic(
                    db, workspace_id=review_wid, pending_id=pending_ids[1], actor_user_id=uid,
                    outcome="included_in_opening", explanation="Synthetic verified bank opening",
                )
        with sessions.begin() as db:
            before = main_treasury_summary(db, workspace_id=review_wid, workspace_timezone="Africa/Cairo")
            assert before["bank_minor"] == 3000
            receipts = [
                PaymentTransaction(workspace_id=review_wid, patient_id=patient_id,
                                   transaction_type="payment", amount_minor=amount, currency="EGP",
                                   payment_method=method, source="staff", created_by_user_id=uid)
                for method, amount in (("visa", 800), ("instapay", 600))
            ]
            db.add_all(receipts)
            db.flush()
            visa_id = receipts[0].id
            assert db.scalar(select(func.count(VaultLedgerEntry.id)).where(
                VaultLedgerEntry.workspace_id == review_wid,
                VaultLedgerEntry.kind == "payment",
            )) == 2
        with sessions.begin() as db:
            refund = PaymentTransaction(
                workspace_id=review_wid, patient_id=patient_id, transaction_type="refund",
                amount_minor=300, currency="EGP", payment_method="visa", source="staff",
                created_by_user_id=uid, reference_transaction_id=visa_id,
            )
            db.add(refund)
            db.flush()
            after = main_treasury_summary(db, workspace_id=review_wid, workspace_timezone="Africa/Cairo")
            assert (after["visa_minor"], after["instapay_minor"], after["bank_minor"]) == (500, 600, 4100)
            assert db.scalar(select(func.count(VaultLedgerEntry.id)).where(
                VaultLedgerEntry.workspace_id == review_wid,
                VaultLedgerEntry.kind == "refund",
                VaultLedgerEntry.payment_transaction_id == refund.id,
            )) == 1
        print("VAULT_0100_SHARED_OPENING_CAP_PAYMENT_REFUND_PASS")
    finally:
        engine.dispose()
