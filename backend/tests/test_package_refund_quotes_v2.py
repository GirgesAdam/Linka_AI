from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.services import package_refund_quotes

WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
PATIENT_ID = UUID("22222222-2222-4222-8222-222222222222")
PACKAGE_ID = UUID("33333333-3333-4333-8333-333333333333")
SERVICE_ID = UUID("44444444-4444-4444-8444-444444444444")


def _package(**updates):
    values = {
        "id": PACKAGE_ID,
        "workspace_id": WORKSPACE_ID,
        "patient_id": PATIENT_ID,
        "service_id": SERVICE_ID,
        "name": "Laser 6",
        "laser_device_key": "candela_gentle",
        "currency": "EGP",
        "sessions_purchased": 6,
        "opening_sessions_remaining": None,
        "sessions_total_known": True,
        "standalone_session_price_minor_at_purchase": 50000,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def test_quote_uses_same_collected_consumed_and_prior_refund_formula(monkeypatch) -> None:
    package = _package()
    monkeypatch.setattr(
        package_refund_quotes,
        "_usage_totals",
        lambda *args, **kwargs: (1, 2),
    )
    monkeypatch.setattr(
        package_refund_quotes,
        "_package_financial_rows",
        lambda *args, **kwargs: (
            [SimpleNamespace(amount_minor=300000)],
            [SimpleNamespace(amount_minor=20000)],
        ),
    )

    quote = package_refund_quotes._quote_one(SimpleNamespace(), package=package)

    assert quote.reserved_sessions == 1
    assert quote.consumed_sessions == 2
    assert quote.sessions_remaining == 3
    assert quote.consumed_value_minor == 100000
    assert quote.refundable_minor == 180000


def test_migrated_opening_balance_adds_historical_consumption_for_refund(monkeypatch) -> None:
    package = _package(opening_sessions_remaining=3)
    monkeypatch.setattr(
        package_refund_quotes,
        "_usage_totals",
        lambda *args, **kwargs: (0, 1),
    )
    monkeypatch.setattr(
        package_refund_quotes,
        "_package_financial_rows",
        lambda *args, **kwargs: ([SimpleNamespace(amount_minor=400000)], []),
    )

    quote = package_refund_quotes._quote_one(SimpleNamespace(), package=package)

    assert quote.consumed_sessions == 4
    assert quote.sessions_remaining == 2
    assert quote.consumed_value_minor == 200000
    assert quote.refundable_minor == 200000


def test_migrated_quote_without_snapshot_uses_service_price(monkeypatch) -> None:
    package = _package(
        opening_sessions_remaining=3,
        standalone_session_price_minor_at_purchase=None,
    )
    monkeypatch.setattr(
        package_refund_quotes,
        "_usage_totals",
        lambda *args, **kwargs: (0, 1),
    )
    monkeypatch.setattr(
        package_refund_quotes,
        "_package_financial_rows",
        lambda *args, **kwargs: ([SimpleNamespace(amount_minor=400000)], []),
    )
    db = SimpleNamespace(scalar=lambda *args, **kwargs: 70000)

    quote = package_refund_quotes._quote_one(db, package=package)

    assert quote.consumed_sessions == 4
    assert quote.standalone_session_price_minor_at_purchase == 70000
    assert quote.consumed_value_minor == 280000
    assert quote.refundable_minor == 120000


def test_unknown_migrated_total_fails_closed(monkeypatch) -> None:
    package = _package(opening_sessions_remaining=3, sessions_total_known=False)
    monkeypatch.setattr(
        package_refund_quotes,
        "_usage_totals",
        lambda *args, **kwargs: (0, 0),
    )

    with pytest.raises(package_refund_quotes.PackageRefundQuoteError):
        package_refund_quotes._quote_one(SimpleNamespace(), package=package)


def test_consumed_package_without_purchase_price_snapshot_fails_closed(monkeypatch) -> None:
    package = _package(standalone_session_price_minor_at_purchase=None)
    monkeypatch.setattr(
        package_refund_quotes,
        "_usage_totals",
        lambda *args, **kwargs: (0, 1),
    )

    with pytest.raises(package_refund_quotes.PackageRefundQuoteError):
        package_refund_quotes._quote_one(SimpleNamespace(), package=package)
