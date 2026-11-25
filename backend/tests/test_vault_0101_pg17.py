# ruff: noqa: F401, F811
"""Mandatory 0100 -> 0101 upgrade and real API/ledger verification on disposable PG17."""
from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from test_appointment_checkout_completion_pg17 import (
    checkout,
    checkout_tables,
    make_standard,
    visit,
)
from test_existing_package_additional_service_pg17 import ctx, engine

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.api.routes import vault as vault_api
from app.database.schema_capabilities import vault_ledger_schema_available
from app.models import (
    Appointment,
    Branch,
    PackageUsage,
    Patient,
    PatientPackage,
    PaymentAllocation,
    PaymentTransaction,
    User,
    Workspace,
    WorkspaceMember,
)
from app.models.expense import Expense
from app.models.vault import VaultAccount, VaultBranchGrant, VaultLedgerEntry, VaultPendingPayment
from app.services.vault import (
    VaultError,
    activate_account,
    main_treasury_summary,
    record_manual_funding,
)
from app.services.vault_pending import review_preactivation_electronic

pytestmark = pytest.mark.skipif(
    os.environ.get("VAULT_0101_REQUIRED") != "1",
    reason="Mandatory 0101 PG17 CI gate uses the isolated database left at 0100.",
)


def test_0101_upgrade_and_manual_funding_actual_api(visit):
    raw = os.environ["EXISTING_PACKAGE_TEST_DATABASE_URL"]
    url = make_url(raw)
    assert (url.host, url.port, url.database, url.username) == (
        "127.0.0.1", 55432, "package_service_ci", "package_ci_user",
    )
    sessions = visit["sessions"]
    workspace, admin = visit["workspace"], visit["user"]
    wid, uid = workspace.id, admin.id

    def count(db, model):
        return db.scalar(select(func.count(model.id)).where(model.workspace_id == wid))

    def account(db, kind):
        return db.scalar(select(VaultAccount).where(
            VaultAccount.workspace_id == wid, VaultAccount.kind == kind,
        ))

    def balance(db, kind):
        return db.scalar(select(func.coalesce(func.sum(VaultLedgerEntry.amount_minor), 0)).where(
            VaultLedgerEntry.workspace_id == wid,
            VaultLedgerEntry.account_id == account(db, kind).id,
        ))

    def facts(db):
        return tuple(count(db, model) for model in (
            Patient, Appointment, PatientPackage, PackageUsage, PaymentTransaction, PaymentAllocation, Expense,
        ))

    with sessions.begin() as db:
        assert db.scalar(text("SELECT current_setting('server_version_num')")) == "170006"
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0100_vault_bank_reception"
        assert vault_ledger_schema_available(db)
        branch_id = db.get(Appointment, visit["appointment"].id).branch_id
        reception_id = account(db, "reception").id
        cash = activate_account(db, workspace_id=wid, actor_user_id=uid, kind="main_cash",
                                branch_id=None, opening_balance_minor=7000, currency="EGP")
        visa = activate_account(db, workspace_id=wid, actor_user_id=uid, kind="main_visa",
                                branch_id=None, opening_balance_minor=2000, currency="EGP")
        cash_id, visa_id = cash.id, visa.id
        old_entries = set(db.scalars(select(VaultLedgerEntry.id).where(
            VaultLedgerEntry.workspace_id == wid,
        )))
        assert len(old_entries) == 2
        suffix = uuid4().hex
        member_user = User(email="funding-member-" + suffix + "@example.invalid")
        denied_user = User(email="funding-denied-" + suffix + "@example.invalid")
        other = Workspace(name="Other", slug="funding-other-" + suffix)
        demo = Workspace(name="Demo", slug="funding-demo-" + suffix, is_demo=True)
        inactive = Workspace(name="Inactive", slug="funding-inactive-" + suffix, is_active=False)
        db.add_all([member_user, denied_user, other, demo, inactive])
        db.flush()
        other_id, demo_id, inactive_id = other.id, demo.id, inactive.id
        member = WorkspaceMember(workspace_id=wid, user_id=member_user.id, role="member",
                                 section_access={"clinic_operations": True, "administration": False})
        denied = WorkspaceMember(workspace_id=wid, user_id=denied_user.id, role="member",
                                 section_access={"clinic_operations": False, "administration": True})
        foreign_branch = Branch(workspace_id=other_id, name="Other branch", code="other")
        inactive_branch = Branch(workspace_id=wid, name="Inactive branch", code="inactive", is_active=False)
        db.add_all([member, denied, foreign_branch, inactive_branch])
        db.flush()
        foreign_id, inactive_branch_id = foreign_branch.id, inactive_branch.id
        # Historical grant belongs to another user. The allowed member has no grant.
        grant = VaultBranchGrant(workspace_id=wid, branch_id=branch_id, user_id=uid, granted_by_user_id=uid)
        db.add(grant)
        db.flush()
        grant_id = grant.id
        before = facts(db)
        assert account(db, "main_bank") is None
        # A historical InstaPay receipt predates automatic channel creation.
        historical = PaymentTransaction(
            workspace_id=wid, patient_id=visit["patient"].id, transaction_type="payment",
            amount_minor=1000, currency="EGP", payment_method="instapay", source="staff",
            created_by_user_id=uid,
        )
        db.add(historical)
        db.flush()
        historical_id = historical.id
        pending_id = db.scalar(select(VaultPendingPayment.id).where(
            VaultPendingPayment.payment_transaction_id == historical_id,
        ))
        assert pending_id is not None
        before = facts(db)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0101_vault_manual_funding"],
                   env={**os.environ, "DATABASE_URL": raw, "MIGRATION_DATABASE_URL": raw},
                   check=True, timeout=120)

    with sessions.begin() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0101_vault_manual_funding"
        assert vault_ledger_schema_available(db)
        assert count(db, VaultAccount) == 5
        assert account(db, "reception").id == reception_id
        assert account(db, "main_cash").id == cash_id and balance(db, "main_cash") == 7000
        assert account(db, "main_visa").id == visa_id and balance(db, "main_visa") == 2000
        for kind in ("main_bank", "main_instapay"):
            assert account(db, kind).opened_by_user_id is None
            assert balance(db, kind) == 0
        assert set(db.scalars(select(VaultLedgerEntry.id).where(
            VaultLedgerEntry.workspace_id == wid,
        ))) == old_entries
        assert db.get(VaultBranchGrant, grant_id) is not None
        assert facts(db) == before
        for excluded in (demo_id, inactive_id):
            assert db.scalar(select(func.count(VaultAccount.id)).where(
                VaultAccount.workspace_id == excluded,
            )) == 0
        new = Workspace(name="New", slug="funding-new-" + uuid4().hex)
        db.add(new)
        db.flush()
        new_id = new.id
        new_branch = Branch(workspace_id=new_id, name="New Branch", code="new")
        db.add(new_branch)
        db.flush()
        assert db.scalar(select(func.count(VaultAccount.id)).where(
            VaultAccount.workspace_id == new_id,
        )) == 5
        assert db.scalar(select(func.count(VaultLedgerEntry.id)).where(
            VaultLedgerEntry.workspace_id == new_id,
        )) == 0
        db.get(Workspace, inactive_id).is_active = True
        db.flush()
        assert db.scalar(select(func.count(VaultAccount.id)).where(
            VaultAccount.workspace_id == inactive_id,
        )) == 4
        db.get(Workspace, inactive_id).is_active = False
        db.flush()
        db.get(Workspace, inactive_id).is_active = True
        db.flush()
        assert db.scalar(select(func.count(VaultAccount.id)).where(
            VaultAccount.workspace_id == inactive_id,
        )) == 4

    app = visit["app"]
    app.include_router(vault_api.router, prefix="/vault")
    client = visit["client"]
    admin_access = WorkspaceAccess(user=admin, workspace=workspace, membership=visit["member"])
    member_access = WorkspaceAccess(user=member_user, workspace=workspace, membership=member)
    denied_access = WorkspaceAccess(user=denied_user, workspace=workspace, membership=denied)
    app.dependency_overrides[get_workspace_access] = lambda: member_access
    base = "/vault/reception/" + str(branch_id)
    cash_path = "/vault/treasury/main_cash/funding"
    bank_path = "/vault/treasury/main_bank/funding"

    def fund(path, amount, key, note=None):
        return client.post(path, json={"amount_minor": amount, "idempotency_key": key, "note": note})

    rows = client.get("/vault/reception")
    assert rows.status_code == 200, rows.text
    assert {row["branch_id"] for row in rows.json()} == {str(branch_id)}
    assert rows.json()[0]["activated"]
    assert client.get("/vault/treasury").status_code == 403
    assert fund(cash_path, 100, "member-cash").status_code == 403
    assert fund(bank_path, 100, "member-bank").status_code == 403
    assert fund("/vault/reception/" + str(foreign_id) + "/funding", 100, "foreign").status_code == 403
    assert fund("/vault/reception/" + str(inactive_branch_id) + "/funding", 100, "inactive").status_code == 403
    for invalid in (0, -1, 1.5, True, "100"):
        assert fund(base + "/funding", invalid, "invalid").status_code == 422
    first = fund(base + "/funding", 1000, "reception-one", "verified cash")
    assert first.status_code == 201, first.text
    replay = fund(base + "/funding", 1000, "reception-one", "verified cash")
    assert replay.status_code == 201 and replay.json() == first.json()
    assert fund(base + "/funding", 1001, "reception-one", "verified cash").status_code == 409
    assert fund(base + "/funding", 500, "reception-two").status_code == 201
    app.dependency_overrides[get_workspace_access] = lambda: denied_access
    assert client.get("/vault/reception").status_code == 403
    assert fund(base + "/funding", 100, "denied").status_code == 403
    app.dependency_overrides[get_workspace_access] = lambda: admin_access
    assert fund(cash_path, 3000, "main-cash-one").status_code == 201
    assert fund(bank_path, 4000, "main-bank-one").status_code == 201
    # Manual funding is not evidence of an old opening balance.
    with pytest.raises(VaultError, match="does not cover"), sessions.begin() as db:
        review_preactivation_electronic(
            db, workspace_id=wid, pending_id=pending_id, actor_user_id=uid,
            outcome="included_in_opening", explanation="Synthetic receipt review",
        )
    with sessions.begin() as db:
        pending = db.get(VaultPendingPayment, pending_id)
        assert pending.review_outcome is None
        assert db.scalar(select(VaultLedgerEntry.id).where(
            VaultLedgerEntry.payment_transaction_id == historical_id,
        )) is None
        reviewed = review_preactivation_electronic(
            db, workspace_id=wid, pending_id=pending_id, actor_user_id=uid,
            outcome="requires_accounting_decision", explanation="No historical opening balance",
        )
        assert reviewed.review_outcome == "requires_accounting_decision"
    assert fund(bank_path, 3000, "main-cash-one").status_code == 409
    assert fund(base + "/funding", 1000, "reception-one", "verified cash").status_code == 409
    assert client.post("/vault/accounts/activate", json={
        "kind":"main_cash","opening_balance_minor":100,"currency":"EGP",
    }).status_code == 410
    assert client.get("/vault/branch-grants").status_code == 404

    def concurrent_funding(key, amount):
        with sessions.begin() as db:
            return record_manual_funding(
                db, workspace_id=wid, kind="reception", branch_id=branch_id,
                actor_user_id=uid, amount_minor=amount, note=None, idempotency_key=key,
            ).id
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(concurrent_funding, "race-same", 600)
        b = pool.submit(concurrent_funding, "race-same", 600)
        assert a.result(timeout=30) == b.result(timeout=30)
        a = pool.submit(concurrent_funding, "race-one", 700)
        b = pool.submit(concurrent_funding, "race-two", 700)
        assert a.result(timeout=30) != b.result(timeout=30)

    with sessions.begin() as db:
        assert facts(db) == before
        assert balance(db, "reception") == 3500
        assert balance(db, "main_cash") == 10000
        assert balance(db, "main_bank") == 4000
        entries = list(db.scalars(select(VaultLedgerEntry).where(
            VaultLedgerEntry.workspace_id == wid, VaultLedgerEntry.kind == "manual_funding",
        )))
        assert len(entries) == 7
        assert all(e.payment_transaction_id is None and e.expense_id is None and e.transfer_id is None
                   and e.actor_user_id and e.idempotency_key and e.created_at for e in entries)
        eid = entries[0].id
        assert main_treasury_summary(db, workspace_id=wid, workspace_timezone="Africa/Cairo")["bank_minor"] == 6000
    for sql in ("UPDATE public.vault_ledger_entries SET amount_minor=1 WHERE id=:id",
                "DELETE FROM public.vault_ledger_entries WHERE id=:id"):
        with pytest.raises(DBAPIError), sessions.begin() as db:
            db.execute(text(sql), {"id": eid})
    with pytest.raises(DBAPIError), sessions.begin() as db:
        db.add(VaultLedgerEntry(workspace_id=wid, account_id=account(db, "main_bank").id,
                                kind="opening", amount_minor=100, actor_user_id=uid))
        db.flush()

    receipt = client.get("/vault/reception").json()[0]
    assert receipt["manual_funding_today_minor"] == 3500
    assert receipt["cash_today_minor"] == 0
    assert any(row["kind"] == "manual_funding" and row["note"] == "verified cash" for row in receipt["recent_movements"])
    treasury = client.get("/vault/treasury").json()
    assert treasury["cash_activated"] and treasury["bank_activated"]
    assert treasury["total_recorded_minor"] == 16000
    assert any(row["kind"] == "manual_funding" for row in treasury["recent_movements"])

    make_standard(visit)
    key = "checkout-funding-" + uuid4().hex
    response = checkout(visit, amount=5000, key=key, payment_method="cash")
    assert response.status_code == 200, response.text
    with sessions.begin() as db:
        payment_id = db.scalar(select(PaymentTransaction.id).where(
            PaymentTransaction.workspace_id == wid, PaymentTransaction.idempotency_key == key,
        ))
        assert balance(db, "reception") == 8500
    response = client.post("/payments/appointments/" + str(visit["appointment"].id) + "/refunds",
                           json={"payment_transaction_id":str(payment_id),"amount_minor":1000,
                                 "reason":"Synthetic refund compatibility"},
                           headers={"Idempotency-Key":"refund-funding-" + uuid4().hex})
    assert response.status_code == 201, response.text
    response = client.post(base + "/expenses", json={"amount_minor":500,"description":"Synthetic supplies",
                                                     "idempotency_key":"funding-expense"})
    assert response.status_code == 201, response.text
    response = client.post(base + "/transfer", json={"idempotency_key":"funding-transfer"})
    assert response.status_code == 201, response.text
    assert response.json()["amount_minor"] == 7000
    response = client.post("/vault/treasury/bank-deposits", json={"amount_minor":2000,
                                                                "idempotency_key":"funding-deposit"})
    assert response.status_code == 201, response.text
    with sessions.begin() as db:
        assert balance(db, "reception") == 0
        summary = main_treasury_summary(db, workspace_id=wid, workspace_timezone="Africa/Cairo")
        assert (summary["cash_minor"], summary["bank_minor"], summary["total_recorded_minor"]) == (15000, 8000, 23000)
        assert count(db, PaymentTransaction) == before[4] + 2
        assert count(db, Expense) == before[6] + 1
        assert count(db, VaultBranchGrant) == 1
        assert len(list(db.scalars(select(VaultLedgerEntry.id).where(
            VaultLedgerEntry.workspace_id == wid, VaultLedgerEntry.kind == "opening",
        )))) == 2
    print("VAULT_0101_AUTO_ACTIVATION_AUTH_FUNDING_LEDGER_COMPATIBILITY_PASS")
