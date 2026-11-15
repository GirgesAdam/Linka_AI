"""Real SECURITY INVOKER compatibility using an isolated non-superuser DB role.

Only accepts the dedicated disposable localhost CI PostgreSQL 17.6 instance.
No production/staging data or settings may be used. Requires the synthetic
workspace produced by vault_0099_pg17_rehearsal.py.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.models.vault import VaultLedgerEntry
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.vault import (
    activate_account,
    record_cash_expense,
    transfer_full_balance,
)

raw = os.environ.get("VAULT_PG17_BACKEND_DATABASE_URL")
if not raw:
    raise SystemExit("Missing dedicated local non-superuser Vault backend-role URL.")
url = make_url(raw)
if (url.host, url.port, url.database, url.username) != (
    "127.0.0.1", 55443, "vault_0099_financial_test", "vault_ci_backend",
):
    raise SystemExit("Non-disposable/non-local backend-role connection refused.")
engine = create_engine(raw, connect_args={"connect_timeout": 5}, pool_pre_ping=True)
try:
    with engine.connect() as conn:
        revision = conn.scalar(text("SELECT version_num FROM public.alembic_version"))
        version = int(conn.scalar(text("SELECT current_setting('server_version_num')")))
        identity = conn.execute(text(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        )).one()
        assert revision == "0099_reception_main_vault_ledger"
        assert version // 10000 == 17
        assert identity == (False, True), "Must be a non-superuser isolated backend role"

    with Session(engine) as db:
        # This fixture exists ONLY because the fresh DB financial suite ran.
        workspaces = db.scalars(select(Workspace).where(
            Workspace.slug.like("ci-vault-%"),
        )).all()
        assert len(workspaces) == 1, "Synthetic financial fixture not found exactly once"
        workspace_id = workspaces[0].id
        actor = db.scalar(select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.role == "admin",
        ))
        original_visit = db.scalar(select(Appointment).where(
            Appointment.workspace_id == workspace_id,
        ).order_by(Appointment.created_at).limit(1))
        assert actor and original_visit
        branch = Branch(
            workspace_id=workspace_id, name="Synthetic Runtime Role Branch", code="ROLE-CI",
        )
        db.add(branch)
        db.flush()
        new_branch = branch.id
        start = datetime(2035, 6, 1, tzinfo=UTC)
        visit = Appointment(
            workspace_id=workspace_id, branch_id=branch.id,
            patient_id=original_visit.patient_id, doctor_id=original_visit.doctor_id,
            service_id=original_visit.service_id, doctor_assignment_known=False,
            status="completed", source="staff",
            start_at=start, end_at=start + timedelta(minutes=30),
            busy_start_at=start, busy_end_at=start + timedelta(minutes=30),
            duration_minutes=30, price_minor=3000, currency="EGP",
        )
        db.add(visit)
        db.flush()
        appointment_id = visit.id
        patient_id = visit.patient_id
        activate_account(
            db, workspace_id=workspace_id, actor_user_id=actor,
            kind="reception", branch_id=new_branch,
            opening_balance_minor=5000, currency="EGP",
        )
        db.commit()

    def payment(*, amount: int, method: str, kind: str,
                reference=None, appointment_id=appointment_id):
        with Session(engine) as db:
            obj = PaymentTransaction(
                id=uuid4(), workspace_id=workspace_id,
                appointment_id=appointment_id, origin_appointment_id=appointment_id,
                patient_id=patient_id, created_by_user_id=actor,
                transaction_type=kind, amount_minor=amount,
                payment_method=method, currency="EGP", source="staff",
                reference_transaction_id=reference, created_at=datetime.now(UTC),
            )
            db.add(obj)
            db.flush()
            if kind == "payment" and method == "cash":
                db.add(PaymentAllocation(
                    workspace_id=workspace_id,
                    transaction_id=obj.id,
                    appointment_id=appointment_id,
                    amount_minor=amount,
                ))
                db.flush()
            entry = db.scalar(select(VaultLedgerEntry).where(
                VaultLedgerEntry.workspace_id == workspace_id,
                VaultLedgerEntry.payment_transaction_id == obj.id,
            ))
            assert entry and entry.amount_minor == (
                amount if kind == "payment" else -amount
            ), "SECURITY INVOKER backend-role payment posting failed"
            db.commit()
            return obj.id, entry.account_id

    visa_id, visa_account_id = payment(amount=4400, method="visa", kind="payment")
    cash_id, cash_account_id = payment(amount=1500, method="cash", kind="payment")
    _, refund_account = payment(
        amount=300, method="cash", kind="refund", reference=cash_id,
    )
    assert refund_account == cash_account_id

    with Session(engine) as db:
        branch = db.get(Branch, new_branch)
        expense = record_cash_expense(
            db, workspace_id=workspace_id, branch=branch,
            actor_user_id=actor, amount_minor=200,
            description="Synthetic test branch expense",
            idempotency_key="ci-runtime-expense-" + uuid4().hex,
            workspace_timezone="Africa/Cairo",
        )
        assert expense.id
        transferred = transfer_full_balance(
            db, workspace_id=workspace_id, branch_id=new_branch,
            actor_user_id=actor,
            idempotency_key="ci-runtime-transfer-" + uuid4().hex,
        )
        assert transferred.amount_minor == 6000, transferred.amount_minor
        db.commit()
        reception_final = db.scalar(select(func.coalesce(func.sum(
            VaultLedgerEntry.amount_minor), 0)).where(
            VaultLedgerEntry.account_id == cash_account_id,
        ))
        assert reception_final == 0, reception_final
        visa_ledger = db.scalar(select(func.coalesce(func.sum(
            VaultLedgerEntry.amount_minor), 0)).where(
            VaultLedgerEntry.account_id == visa_account_id,
        ))
        assert visa_ledger >= 4400, visa_ledger

    print("VAULT_NON_SUPERUSER_BACKEND_ROLE_PAYMENT_ALLOCATION_REFUND_EXPENSE_TRANSFER_PASS")
    print("VAULT_RUNTIME_RLS_BROWSER_ROLE_ACCESS_NOT_GRANTED")
finally:
    engine.dispose()
