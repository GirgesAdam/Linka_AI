"""Focused source-evidence and admin resolution tests."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from app.api.routes.payments import _vault_refund_conflict
from app.models.vault import VaultLedgerEntry
from app.services import vault_pending
from app.services.vault import VaultError

TEST_PATIENT_ID = uuid4()


def payment(**overrides):
    data = dict(
        id=uuid4(), patient_id=TEST_PATIENT_ID, amount_minor=10000, currency="EGP", source="staff",
        payment_method="cash", transaction_type="payment",
        created_at=datetime.now(UTC), origin_appointment_id=uuid4(),
        appointment_id=uuid4(), external_reference=None,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def allocation(amount, branch, **kwargs):
    return SimpleNamespace(
        id=uuid4(), appointment_id=uuid4(), amount_minor=amount,
        branch_id=branch, name="Branch", patient_id=TEST_PATIENT_ID, **kwargs,
    )


class Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeDb:
    def __init__(self, *, scalar_results=(), allocation_rows=()):
        self.scalars_results = list(scalar_results)
        self.allocation_rows = list(allocation_rows)
        self.added = []
        self.deleted = []
        self.flushed = 0

    def scalar(self, _query):
        return self.scalars_results.pop(0) if self.scalars_results else None

    def execute(self, _query):
        return Rows(self.allocation_rows)

    def add(self, row):
        if row.id is None:
            row.id = uuid4()
        self.added.append(row)

    def delete(self, row):
        self.deleted.append(row)

    def flush(self):
        self.flushed += 1


@pytest.mark.parametrize(
    ("amounts", "expected"),
    [
        ([], False),
        ([5000], False),
        ([3000, 7000], True),
        ([10000], True),
        ([12000], False),
    ],
)
def test_exact_total_evidence_required(amounts, expected):
    ws, branch = uuid4(), uuid4()
    p = payment()
    db = FakeDb(allocation_rows=[allocation(amount, branch) for amount in amounts])
    evidence, candidate = vault_pending._allocation_evidence(
        db, workspace_id=ws, transaction=p,
    )
    assert len(evidence) == len(amounts)
    assert (candidate == branch) == expected


def test_cross_patient_allocation_never_justifies_branch_posting():
    ws, branch = uuid4(), uuid4()
    row = allocation(10000, branch)
    row.patient_id = uuid4()
    evidence, candidate = vault_pending._allocation_evidence(
        FakeDb(allocation_rows=[row]), workspace_id=ws, transaction=payment(),
    )
    assert candidate is None
    assert evidence[0]["patient_matches_source"] is False


def test_multi_branch_is_never_proof_of_custody():
    ws = uuid4()
    db = FakeDb(allocation_rows=[allocation(5000, uuid4()), allocation(5000, uuid4())])
    _evidence, candidate = vault_pending._allocation_evidence(
        db, workspace_id=ws, transaction=payment(),
    )
    assert candidate is None


@pytest.mark.parametrize("kwargs", [
    {"source":"legacy_backfill"},
    {"transaction_type":"refund"},
    {"payment_method":"visa"},
    {"currency":"USD"},
])
def test_manual_branch_posting_is_cash_and_nonlegacy_only(kwargs):
    assert not vault_pending._posting_allowed(payment(**kwargs))


def test_resolve_posts_once_with_source_unique_and_audit(monkeypatch):
    ws, branch, actor = uuid4(), uuid4(), uuid4()
    p = payment()
    pending = SimpleNamespace(id=uuid4(), payment_transaction_id=p.id)
    account = SimpleNamespace(id=uuid4())
    evidence = [allocation(10000, branch)]
    db = FakeDb(
        scalar_results=[pending, p, pending, None],
        allocation_rows=evidence,
    )
    audit = []
    monkeypatch.setattr(
        vault_pending, "_eligible_account",
        lambda *_args, **_kwargs: account,
    )
    monkeypatch.setattr(
        vault_pending, "record_activity_event",
        lambda *_args, **kwargs: audit.append(kwargs),
    )
    result = vault_pending.resolve_pending_payment(
        db, workspace_id=ws, pending_id=pending.id,
        branch_id=branch, actor_user_id=actor,
        explanation="Verified complete payment allocations",
    )
    assert isinstance(result, VaultLedgerEntry)
    assert result.account_id == account.id
    assert result.payment_transaction_id == p.id
    assert result.amount_minor == 10000
    assert result.idempotency_key == "pending:" + str(p.id)
    assert db.deleted == [pending]
    assert len(db.added) == 1
    assert audit[0]["action"] == "vault.pending_payment_resolved"
    assert audit[0]["actor_user_id"] == actor
    assert audit[0]["metadata"]["branch_id"] == str(branch)


def test_resolve_wrong_branch_does_not_create_entry():
    ws, branch = uuid4(), uuid4()
    p = payment()
    pending = SimpleNamespace(id=uuid4(), payment_transaction_id=p.id)
    db = FakeDb(scalar_results=[pending, p], allocation_rows=[allocation(10000, branch)])
    with pytest.raises(VaultError, match="disagrees"):
        vault_pending.resolve_pending_payment(
            db, workspace_id=ws, pending_id=pending.id,
            branch_id=uuid4(), actor_user_id=uuid4(),
            explanation="Unsupported external branch claim",
        )
    assert db.added == []


def test_resolve_posted_payment_refuses_duplicate(monkeypatch):
    ws, branch = uuid4(), uuid4()
    p = payment()
    pending = SimpleNamespace(id=uuid4(), payment_transaction_id=p.id)
    db = FakeDb(scalar_results=[pending, p, pending, uuid4()],
                allocation_rows=[allocation(10000, branch)])
    monkeypatch.setattr(vault_pending, "_eligible_account",
                        lambda *_args, **_kwargs: SimpleNamespace(id=uuid4()))
    with pytest.raises(VaultError, match="already been posted"):
        vault_pending.resolve_pending_payment(
            db, workspace_id=ws, pending_id=pending.id,
            branch_id=branch, actor_user_id=uuid4(),
            explanation="Previously posted source receipt",
        )
    assert db.added == []


def test_vault_refund_sql_error_is_translated_not_exposed():
    exc = DBAPIError("INSERT", {}, Exception("VAULT_REFUND_REQUIRES_APPROVED_FUNDING"))
    conflict = _vault_refund_conflict(exc)
    assert conflict is not None
    assert conflict.status_code == 409
    assert "VAULT_REFUND_REQUIRES_APPROVED_FUNDING" not in conflict.detail
    assert "خزنة الريسبشن" in conflict.detail


def test_other_database_errors_are_not_hidden():
    assert _vault_refund_conflict(DBAPIError("SELECT", {}, Exception("deadlock"))) is None
