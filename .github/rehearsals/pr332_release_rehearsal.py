"""Isolated production-shape fixture; no actual clinic data or credentials."""
import os
import subprocess
import sys
import types
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from test_appointment_checkout_completion_pg17 import checkout, make_standard, visit, checkout_tables
from test_existing_package_additional_service_pg17 import ctx, engine

from app.database.schema_capabilities import vault_ledger_schema_available
from app.models import Appointment, Branch, Patient, PaymentTransaction, Workspace
from app.models.vault import VaultAccount, VaultLedgerEntry, VaultPendingPayment
from app.services.vault import activate_account, transfer_full_balance


def test_production_shape_cash_cutover(visit):
    sessions = visit["sessions"]
    workspace = visit["workspace"].id
    user = visit["user"].id
    with sessions.begin() as db:
        assert db.scalar(text("SELECT current_setting('server_version_num')")) == "170006"
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0099_reception_main_vault_ledger"
        assert vault_ledger_schema_available(db)
        branch = db.get(Appointment, visit["appointment"].id).branch_id
        second = Workspace(name="Synthetic Second", slug="second-" + uuid4().hex)
        third = Workspace(name="Synthetic Third", slug="third-" + uuid4().hex)
        demo = Workspace(name="Synthetic Demo", slug="demo-" + uuid4().hex, is_demo=True)
        db.add_all([second, third, demo])
        db.flush()
        db.add_all([
            Branch(workspace_id=second.id, name="Active 2", code="B2"),
            Branch(workspace_id=third.id, name="Active 3", code="B3"),
            Branch(workspace_id=demo.id, name="Demo Active", code="DEMO"),
        ])
        for index in range(7):
            db.add(Branch(workspace_id=second.id, name="Inactive " + str(index), code="I" + str(index), is_active=False))
        patient = Patient(workspace_id=second.id, first_name="Synthetic Historical")
        db.add(patient)
        db.flush()
        # Representative historical rows: explicitly imported facts and a
        # genuine post-0099 pre-activation receipt, none may be swept by 0100.
        db.add_all([
            PaymentTransaction(workspace_id=second.id, patient_id=patient.id,
                transaction_type="payment", amount_minor=1000, currency="EGP",
                payment_method=method, source="legacy_backfill", created_by_user_id=user)
            for method in ("cash", "visa", "instapay", "unknown")
        ])
        db.add(PaymentTransaction(workspace_id=second.id, patient_id=patient.id,
            transaction_type="payment", amount_minor=2000, currency="EGP",
            payment_method="cash", source="staff", created_by_user_id=user))
        db.flush()
        assert db.scalar(select(func.count(Branch.id))) == 11
        assert db.scalar(select(func.count(VaultAccount.id))) == 0
        before = tuple(db.scalar(select(func.count(model.id))) for model in (
            PaymentTransaction, VaultPendingPayment, VaultLedgerEntry,
        ))
        assert before == (5, 1, 0)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0100_vault_bank_reception"],
                   env=os.environ.copy(), check=True, timeout=120)
    old_code = subprocess.check_output([
        "git", "show", "b64324a7cd001b8e8176a142f3f79b533256f056:backend/app/database/schema_capabilities.py",
    ], text=True)
    old_cap = types.ModuleType("old_cap")
    exec(compile(old_code, "old_cap.py", "exec"), old_cap.__dict__)
    with sessions.begin() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0100_vault_bank_reception"
        assert vault_ledger_schema_available(db)
        with pytest.raises(old_cap.SchemaCapabilityError):
            old_cap.vault_ledger_schema_available(db)
        accounts = list(db.scalars(select(VaultAccount)))
        assert len(accounts) == 3
        assert all(a.kind == "reception" and a.opened_by_user_id is None for a in accounts)
        assert db.scalar(select(func.count(VaultAccount.id)).join(Branch, VaultAccount.branch_id == Branch.id)
                         .join(Workspace, Branch.workspace_id == Workspace.id)
                         .where(Workspace.is_demo | ~Branch.is_active)) == 0
        assert tuple(db.scalar(select(func.count(model.id))) for model in (
            PaymentTransaction, VaultPendingPayment, VaultLedgerEntry,
        )) == before
        reception = next(a for a in accounts if a.workspace_id == workspace)
        assert reception.branch_id == branch
        reception_id = reception.id
        activate_account(db, workspace_id=workspace, actor_user_id=user, kind="main_cash",
                         branch_id=None, opening_balance_minor=0, currency="EGP")
    # Actual candidate checkout handler, not direct ledger insertion.
    make_standard(visit)
    key = "release-auto-cash-" + uuid4().hex
    response = checkout(visit, amount=5000, key=key, payment_method="cash")
    assert response.status_code == 200, response.text
    response = checkout(visit, amount=5000, key=key, payment_method="cash")
    assert response.status_code == 200, response.text
    with sessions.begin() as db:
        payment = db.scalar(select(PaymentTransaction).where(
            PaymentTransaction.workspace_id == workspace, PaymentTransaction.idempotency_key == key,
        ))
        entries = list(db.scalars(select(VaultLedgerEntry).where(
            VaultLedgerEntry.payment_transaction_id == payment.id,
        )))
        assert len(entries) == 1 and entries[0].account_id == reception_id
        assert entries[0].amount_minor == 5000
        handover = transfer_full_balance(db, workspace_id=workspace, branch_id=branch,
            actor_user_id=user, idempotency_key="release-handover-" + uuid4().hex)
        assert handover.amount_minor == 5000
    print("AUTO_RECEPTION_CASH_CHECKOUT_AND_HANDOVER_PASS")
    print("HISTORICAL_ROWS_UNCHANGED_3_REAL_1_DEMO_7_INACTIVE_PASS")
    print("OLD_SCHEMA_CAPABILITY_REJECTS_0100_CANDIDATE_ACCEPTS_BOTH_PASS")
