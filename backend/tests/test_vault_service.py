"""Focused local service checks; PostgreSQL invariants have a separate PG17 harness."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.expense import Expense
from app.models.vault import VaultLedgerEntry, VaultTransfer
from app.services import vault as vault_service


class FakeSession:
    def __init__(self, scalar_results: list | None = None):
        self.scalar_results = list(scalar_results or [])
        self.added: list[object] = []
        self.flushed = 0

    def scalar(self, _stmt):
        if self.scalar_results:
            return self.scalar_results.pop(0)
        return None

    def add(self, record):
        if getattr(record, "id", None) is None:
            record.id = uuid4()
        self.added.append(record)

    def add_all(self, rows):
        for row in rows:
            self.add(row)

    def flush(self):
        self.flushed += 1


def _accounts(branch_id):
    reception = SimpleNamespace(id=uuid4(), branch_id=branch_id, kind="reception")
    treasury = SimpleNamespace(id=uuid4(), branch_id=None, kind="main_cash")
    return reception, treasury


def test_full_transfer_double_entry_zero_sum_and_no_payment(monkeypatch):
    workspace_id, branch_id, user_id = uuid4(), uuid4(), uuid4()
    reception, main_cash = _accounts(branch_id)
    db = FakeSession()
    locks = []
    audits = []
    monkeypatch.setattr(
        vault_service,
        "_account",
        lambda _db, _ws, kind, _branch=None: reception if kind == "reception" else main_cash,
    )
    monkeypatch.setattr(
        vault_service, "_lock_accounts",
        lambda _db, _ws, ids: locks.append(ids),
    )
    monkeypatch.setattr(vault_service, "_balance", lambda *_args, **_kwargs: 400_000)
    monkeypatch.setattr(
        vault_service, "record_activity_event",
        lambda *_args, **kwargs: audits.append(kwargs),
    )
    transfer = vault_service.transfer_full_balance(
        db, workspace_id=workspace_id, branch_id=branch_id,
        actor_user_id=user_id, idempotency_key="local-full-transfer-1",
    )
    assert isinstance(transfer, VaultTransfer)
    assert transfer.amount_minor == 400_000
    ledger = [obj for obj in db.added if isinstance(obj, VaultLedgerEntry)]
    assert len(ledger) == 2
    assert sum(entry.amount_minor for entry in ledger) == 0
    assert {entry.kind for entry in ledger} == {"transfer_out", "transfer_in"}
    assert {entry.account_id for entry in ledger} == {reception.id, main_cash.id}
    assert all(entry.transfer_id == transfer.id for entry in ledger)
    assert len(locks) == 1
    assert audits[0]["action"] == "vault.cash_transferred"
    assert all(entry.payment_transaction_id is None for entry in ledger)


def test_transfer_rejects_empty_cash_without_posting(monkeypatch):
    workspace_id, branch_id = uuid4(), uuid4()
    reception, main_cash = _accounts(branch_id)
    db = FakeSession()
    monkeypatch.setattr(
        vault_service, "_account",
        lambda _db, _ws, kind, _branch=None: reception if kind == "reception" else main_cash,
    )
    monkeypatch.setattr(vault_service, "_lock_accounts", lambda *_args: None)
    monkeypatch.setattr(vault_service, "_balance", lambda *_args, **_kwargs: 0)
    with pytest.raises(vault_service.VaultError, match="No available cash"):
        vault_service.transfer_full_balance(
            db, workspace_id=workspace_id, branch_id=branch_id,
            actor_user_id=uuid4(), idempotency_key="empty",
        )
    assert db.added == []


def test_duplicate_transfer_key_return_original_only_for_matching_branch():
    workspace_id, branch_id = uuid4(), uuid4()
    previous = VaultTransfer(
        id=uuid4(), workspace_id=workspace_id, branch_id=branch_id,
        amount_minor=700, currency="EGP", idempotency_key="retry",
    )
    db = FakeSession([previous])
    actual = vault_service.transfer_full_balance(
        db, workspace_id=workspace_id, branch_id=branch_id,
        actor_user_id=uuid4(), idempotency_key="retry",
    )
    assert actual is previous
    assert db.added == []
    db2 = FakeSession([previous])
    with pytest.raises(vault_service.VaultError, match="another branch"):
        vault_service.transfer_full_balance(
            db2, workspace_id=workspace_id, branch_id=uuid4(),
            actor_user_id=uuid4(), idempotency_key="retry",
        )


def test_cash_expense_posts_one_existing_finance_expense_and_one_debit(monkeypatch):
    workspace_id, branch_id, user_id = uuid4(), uuid4(), uuid4()
    reception, _main = _accounts(branch_id)
    branch = SimpleNamespace(id=branch_id, timezone="Africa/Cairo")
    created_expenses = []
    linked = Expense(
        id=uuid4(), workspace_id=workspace_id, created_by_user_id=user_id,
        title="Office supplies", category="other", expense_type="variable",
        amount_minor=2500, currency="EGP", incurred_on=datetime.now(UTC).date(),
    )

    def fake_create(_db, *, payload, **_kwargs):
        created_expenses.append(payload)
        return linked

    monkeypatch.setattr(vault_service, "_account", lambda *_args: reception)
    monkeypatch.setattr(vault_service, "_lock_accounts", lambda *_args: None)
    monkeypatch.setattr(vault_service, "_balance", lambda *_args, **_kwargs: 4000)
    monkeypatch.setattr(vault_service, "create_expense", fake_create)
    db = FakeSession()
    expense = vault_service.record_cash_expense(
        db, workspace_id=workspace_id, branch=branch, actor_user_id=user_id,
        amount_minor=2500, description="Office supplies", idempotency_key="expense-1",
        workspace_timezone="Africa/Cairo",
    )
    assert expense is linked
    assert len(created_expenses) == 1
    entries = [obj for obj in db.added if isinstance(obj, VaultLedgerEntry)]
    assert len(entries) == 1
    assert entries[0].account_id == reception.id
    assert entries[0].expense_id == expense.id
    assert entries[0].amount_minor == -2500
    assert entries[0].idempotency_key == "expense-1"


def test_cash_expense_insufficient_balance_never_creates_finance_expense(monkeypatch):
    reception, _ = _accounts(uuid4())
    monkeypatch.setattr(vault_service, "_account", lambda *_args: reception)
    monkeypatch.setattr(vault_service, "_lock_accounts", lambda *_args: None)
    monkeypatch.setattr(vault_service, "_balance", lambda *_args, **_kwargs: 100)
    monkeypatch.setattr(
        vault_service, "create_expense",
        lambda *_args, **_kwargs: pytest.fail("Expense was created despite insufficient cash"),
    )
    db = FakeSession()
    with pytest.raises(vault_service.VaultError, match="Insufficient"):
        vault_service.record_cash_expense(
            db, workspace_id=uuid4(), branch=SimpleNamespace(id=uuid4(), timezone="UTC"),
            actor_user_id=uuid4(), amount_minor=101, description="Test",
            idempotency_key="too-much", workspace_timezone="UTC",
        )
    assert db.added == []


def test_repeated_expense_key_other_branch_rejected(monkeypatch):
    workspace_id, new_branch = uuid4(), uuid4()
    old_account = SimpleNamespace(id=uuid4())
    new_account = SimpleNamespace(id=uuid4())
    row = VaultLedgerEntry(
        id=uuid4(), workspace_id=workspace_id, account_id=old_account.id,
        kind="expense", amount_minor=-1000, expense_id=uuid4(),
        idempotency_key="same-key",
    )
    expense = SimpleNamespace(
        id=row.expense_id, workspace_id=workspace_id, amount_minor=1000, title="Supplies",
    )
    db = FakeSession([row, expense])
    monkeypatch.setattr(vault_service, "_account", lambda *_args: new_account)
    with pytest.raises(vault_service.VaultError, match="branch details"):
        vault_service.record_cash_expense(
            db, workspace_id=workspace_id,
            branch=SimpleNamespace(id=new_branch, timezone="UTC"),
            actor_user_id=uuid4(), amount_minor=1000, description="Supplies",
            idempotency_key="same-key", workspace_timezone="UTC",
        )


def test_bad_branch_timezone_uses_workspace_fallback():
    _today, branch_start, branch_end = vault_service._local_bounds(
        "Invalid/Branch", "Africa/Cairo"
    )
    _today, expected_start, expected_end = vault_service._local_bounds("Africa/Cairo")
    assert (branch_start, branch_end) == (expected_start, expected_end)


def test_activation_opening_balance_is_carried_over_not_today_revenue():
    assert vault_service._carried_reception_balance(
        12500, {"opening": 10000, "payment": 5000, "expense": -2500},
    ) == 10000
    assert vault_service._carried_reception_balance(10000, {"opening": 10000}) == 10000


def test_timezone_boundaries_are_stable_and_not_calendar_reset():
    today, start, end = vault_service._local_bounds("Africa/Cairo")
    assert start < end
    assert start.tzinfo == UTC
    assert end.tzinfo == UTC
    assert (end - start).total_seconds() in (23*3600, 24*3600, 25*3600)
    assert today == datetime.now(UTC).astimezone(vault_service.ZoneInfo("Africa/Cairo")).date()
