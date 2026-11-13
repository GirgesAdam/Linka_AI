from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

import app.services.finance as finance_service
from app.api.routes.finance import _workspace_today
from app.schemas.finance import ExpenseCreate, ExpenseUpdate
from app.services.finance import FinanceOperationError, _utc_bounds, profitability_summary


def test_expense_payload_normalizes_currency_and_text() -> None:
    payload = ExpenseCreate(
        title="  Clinic rent  ",
        category="rent",
        amount_minor=250000,
        currency=" egp ",
        incurred_on=date(2026, 9, 1),
        note="  September rent  ",
    )

    assert payload.title == "Clinic rent"
    assert payload.currency == "EGP"
    assert payload.note == "September rent"


def test_expense_update_rejects_null_for_non_nullable_columns() -> None:
    with pytest.raises(ValidationError):
        ExpenseUpdate(amount_minor=None)

    assert ExpenseUpdate(note=None).note is None


def test_expense_mutations_emit_workspace_scoped_staff_activity(monkeypatch) -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    expense_id = uuid4()
    events: list[dict] = []

    def fake_record_activity(_db, **kwargs):
        events.append(kwargs)
        return None

    monkeypatch.setattr(finance_service, "record_activity_event", fake_record_activity)

    class _Db:
        def scalar(self, _query):
            return None

        def add(self, value):
            value.id = expense_id

        def delete(self, _value):
            return None

        def flush(self):
            return None

    db = _Db()
    payload = ExpenseCreate(
        title="Clinic rent",
        category="rent",
        amount_minor=250000,
        currency="EGP",
        incurred_on=date(2026, 9, 1),
        note="private finance note",
    )
    created = finance_service.create_expense(
        db,
        workspace_id=workspace_id,
        created_by_user_id=user_id,
        payload=payload,
    )

    assert created.id == expense_id
    assert events[-1]["action"] == "expense.created"
    assert events[-1]["workspace_id"] == workspace_id
    assert events[-1]["actor_user_id"] == user_id
    assert "note" not in events[-1]["metadata"]
    assert "title" not in events[-1]["metadata"]

    existing = SimpleNamespace(
        id=expense_id,
        workspace_id=workspace_id,
        category="rent",
        amount_minor=250000,
        currency="EGP",
        incurred_on=date(2026, 9, 1),
        note="private finance note",
    )
    monkeypatch.setattr(finance_service, "_expense_or_raise", lambda *_args, **_kwargs: existing)

    finance_service.update_expense(
        db,
        workspace_id=workspace_id,
        expense_id=expense_id,
        actor_user_id=user_id,
        payload=ExpenseUpdate(amount_minor=300000, note="updated private note"),
    )
    assert events[-1]["action"] == "expense.updated"
    assert events[-1]["metadata"] == {"changed_fields": ["amount_minor", "note"]}

    finance_service.delete_expense(
        db,
        workspace_id=workspace_id,
        expense_id=expense_id,
        actor_user_id=user_id,
    )
    assert events[-1]["action"] == "expense.deleted"
    assert events[-1]["entity_id"] == expense_id
    assert "note" not in events[-1]["metadata"]


def test_profitability_default_date_uses_workspace_timezone() -> None:
    reference = datetime(2026, 9, 3, 22, 30, tzinfo=UTC)

    assert _workspace_today("UTC", now=reference) == date(2026, 9, 3)
    assert _workspace_today("Africa/Cairo", now=reference) == date(2026, 9, 4)
    assert _workspace_today("Invalid/Timezone", now=reference) == date(2026, 9, 3)


def test_profitability_bounds_use_workspace_timezone() -> None:
    start_at, end_at = _utc_bounds(
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 1),
        timezone_name="Africa/Cairo",
    )

    assert start_at.isoformat() == "2026-08-31T21:00:00+00:00"
    assert end_at.isoformat() == "2026-09-01T21:00:00+00:00"


def test_profitability_rejects_inverted_date_range() -> None:
    with pytest.raises(FinanceOperationError):
        _utc_bounds(
            start_date=date(2026, 9, 2),
            end_date=date(2026, 9, 1),
            timezone_name="Africa/Cairo",
        )


def test_profitability_uses_real_payments_minus_refunds_and_expenses() -> None:
    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Db:
        def __init__(self):
            self.calls = 0

        def execute(self, _stmt):
            self.calls += 1
            if self.calls == 1:
                return _Result([
                    ("EGP", 10000, 2000),
                    ("USD", 500, 0),
                ])
            return _Result([
                ("EGP", 3000),
                ("USD", 100),
                ("EUR", 250),
            ])

    result = profitability_summary(
        _Db(),
        workspace_id=uuid4(),
        timezone_name="Africa/Cairo",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
    )

    by_currency = {row.currency: row for row in result.currencies}
    assert by_currency["EGP"].net_revenue_minor == 8000
    assert by_currency["EGP"].expenses_minor == 3000
    assert by_currency["EGP"].profit_minor == 5000
    assert by_currency["USD"].net_revenue_minor == 500
    assert by_currency["USD"].expenses_minor == 100
    assert by_currency["USD"].profit_minor == 400
    assert by_currency["EUR"].net_revenue_minor == 0
    assert by_currency["EUR"].expenses_minor == 250
    assert by_currency["EUR"].profit_minor == -250
