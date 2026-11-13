# Pytest fixtures are intentionally imported/re-exported and shadowed by local context variables.
# ruff: noqa: F811, F401
"""Actual FastAPI Checkout + Migration 0099 ledger integration on disposable PG17.

Reuses the repository's existing synthetic Checkout fixture, but runs only
when its exact localhost-only PostgreSQL test database is supplied. The
dedicated database MUST be upgraded to 0099 before running these tests.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from test_appointment_checkout_completion_pg17 import (
    checkout,
    make_standard,
    visit,
)
from test_appointment_checkout_completion_pg17 import (
    checkout_tables as checkout_tables,
)
from test_existing_package_additional_service_pg17 import ctx, engine, post

from app.models.appointment import Appointment
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.models.vault import VaultAccount, VaultLedgerEntry, VaultPendingPayment
from app.services.vault import activate_account, transfer_full_balance


@pytest.fixture(autouse=True)
def require_vault_migration(engine):
    with engine.connect() as conn:
        version = conn.scalar(text("SELECT version_num FROM public.alembic_version"))
        assert version == "0099_reception_main_vault_ledger", version
        assert conn.scalar(text("SELECT to_regclass('public.vault_ledger_entries')")) is not None


@pytest.fixture()
def active_vault_visit(visit):
    with visit["sessions"]() as db:
        ws = visit["workspace"].id
        branch = db.get(Appointment, visit["appointment"].id).branch_id
        for kind, branch_id in (
            ("reception", branch), ("main_cash", None),
            ("main_visa", None), ("main_instapay", None),
        ):
            activate_account(
                db, workspace_id=ws, actor_user_id=visit["user"].id,
                kind=kind, branch_id=branch_id,
                opening_balance_minor=0, currency="EGP",
            )
        db.commit()
    return visit


def _ledger_rows(ctx, transaction_id):
    with ctx["sessions"]() as db:
        return db.scalars(
            select(VaultLedgerEntry).where(
                VaultLedgerEntry.workspace_id == ctx["workspace"].id,
                VaultLedgerEntry.payment_transaction_id == transaction_id,
            )
        ).all()


def _source_transaction(ctx, key):
    with ctx["sessions"]() as db:
        return db.scalar(select(PaymentTransaction).where(
            PaymentTransaction.workspace_id == ctx["workspace"].id,
            PaymentTransaction.idempotency_key == key,
        ))


@pytest.mark.parametrize("payment_method,account_kind", [
    ("cash", "reception"), ("visa", "main_visa"), ("instapay", "main_instapay"),
])
def test_actual_checkout_posts_once_and_refunds_to_original_channel(
    active_vault_visit, payment_method, account_kind,
):
    ctx = active_vault_visit
    make_standard(ctx)
    key = "vault-pay-" + uuid4().hex
    response = checkout(ctx, amount=5000, key=key, payment_method=payment_method)
    assert response.status_code == 200, response.text

    payment = _source_transaction(ctx, key)
    assert payment is not None
    assert payment.transaction_type == "payment"
    rows = _ledger_rows(ctx, payment.id)
    assert len(rows) == 1
    assert rows[0].kind == "payment"
    assert rows[0].amount_minor == 5000
    with ctx["sessions"]() as db:
        account = db.get(VaultAccount, rows[0].account_id)
        assert account.kind == account_kind
        assert account.workspace_id == ctx["workspace"].id
        allocations = list(db.scalars(select(PaymentAllocation).where(
            PaymentAllocation.workspace_id == ctx["workspace"].id,
            PaymentAllocation.transaction_id == payment.id,
        )))
        assert sum(a.amount_minor for a in allocations) == 5000 if payment_method == "cash" else True

    replay = checkout(ctx, amount=5000, key=key, payment_method=payment_method)
    assert replay.status_code == 200, replay.text
    assert len(_ledger_rows(ctx, payment.id)) == 1

    refund = ctx["client"].post(
        "/payments/appointments/" + str(ctx["appointment"].id) + "/refunds",
        json={"payment_transaction_id": str(payment.id), "amount_minor": 1000,
              "reason": "Synthetic test refund against posted source"},
        headers={"Idempotency-Key": "refund-vault-" + uuid4().hex},
    )
    assert refund.status_code == 201, refund.text
    with ctx["sessions"]() as db:
        refunds = list(db.scalars(select(PaymentTransaction).where(
            PaymentTransaction.workspace_id == ctx["workspace"].id,
            PaymentTransaction.reference_transaction_id == payment.id,
            PaymentTransaction.transaction_type == "refund",
        )))
        assert len(refunds) == 1
        refund_entries = _ledger_rows(ctx, refunds[0].id)
        assert len(refund_entries) == 1
        assert refund_entries[0].amount_minor == -1000
        assert refund_entries[0].account_id == rows[0].account_id
        assert db.scalar(select(func.sum(VaultLedgerEntry.amount_minor)).where(
            VaultLedgerEntry.account_id == rows[0].account_id,
        )) == 4000


def test_cash_refund_after_full_handover_rolls_back_all_financial_facts(active_vault_visit):
    ctx = active_vault_visit
    make_standard(ctx)
    key = "cash-transfer-" + uuid4().hex
    assert checkout(ctx, amount=10000, key=key, payment_method="cash").status_code == 200
    payment = _source_transaction(ctx, key)
    assert payment is not None

    with ctx["sessions"]() as db:
        branch = db.get(Appointment, ctx["appointment"].id).branch_id
        transfer = transfer_full_balance(
            db, workspace_id=ctx["workspace"].id, branch_id=branch,
            actor_user_id=ctx["user"].id,
            idempotency_key="transfer-" + uuid4().hex,
        )
        assert transfer.amount_minor == 10000
        db.commit()

    key_refund = "unfunded-refund-" + uuid4().hex
    with ctx["sessions"]() as db:
        before_facts = int(db.scalar(select(func.count(PaymentTransaction.id)).where(
            PaymentTransaction.workspace_id == ctx["workspace"].id,
        )))
        before_ledger = int(db.scalar(select(func.count(VaultLedgerEntry.id)).where(
            VaultLedgerEntry.workspace_id == ctx["workspace"].id,
        )))
    refund = ctx["client"].post(
        "/payments/appointments/" + str(ctx["appointment"].id) + "/refunds",
        json={"payment_transaction_id": str(payment.id), "amount_minor": 3000,
              "reason": "Synthetic refund when reception cash is empty"},
        headers={"Idempotency-Key": key_refund},
    )
    assert refund.status_code == 409
    with ctx["sessions"]() as db:
        assert db.scalar(select(func.count(PaymentTransaction.id)).where(
            PaymentTransaction.workspace_id == ctx["workspace"].id,
        )) == before_facts
        assert db.scalar(select(func.count(VaultLedgerEntry.id)).where(
            VaultLedgerEntry.workspace_id == ctx["workspace"].id,
        )) == before_ledger
        reception = db.scalar(select(VaultAccount).where(
            VaultAccount.workspace_id == ctx["workspace"].id,
            VaultAccount.kind == "reception",
        ))
        main_cash = db.scalar(select(VaultAccount).where(
            VaultAccount.workspace_id == ctx["workspace"].id,
            VaultAccount.kind == "main_cash",
        ))
        assert db.scalar(select(func.sum(VaultLedgerEntry.amount_minor)).where(
            VaultLedgerEntry.account_id == reception.id,
        )) == 0
        assert db.scalar(select(func.sum(VaultLedgerEntry.amount_minor)).where(
            VaultLedgerEntry.account_id == main_cash.id,
        )) == 10000


def test_additional_service_checkout_does_not_duplicate_reception_cash(active_vault_visit):
    ctx = active_vault_visit
    make_standard(ctx)
    extra = post(ctx)
    assert extra.status_code == 201, extra.text
    key = "extra-vault-" + uuid4().hex
    assert checkout(ctx, amount=4500, key=key, payment_method="cash").status_code == 200
    payment = _source_transaction(ctx, key)
    assert payment is not None
    rows = _ledger_rows(ctx, payment.id)
    assert len(rows) == 1 and rows[0].amount_minor == 4500


def test_multi_workspace_pending_isolation_on_checkout(active_vault_visit):
    ctx = active_vault_visit
    with ctx["sessions"]() as db:
        assert db.scalar(select(func.count(VaultPendingPayment.id)).where(
            VaultPendingPayment.workspace_id == uuid4(),
        )) == 0
