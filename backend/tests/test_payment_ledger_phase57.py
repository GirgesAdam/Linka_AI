from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.services.payments import STAFF_PAYMENT_METHODS, _payment_totals, _refunds_by_payment


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _row(*, kind: str, amount: int, method: str = "cash", reference=None):
    return SimpleNamespace(
        id=uuid4(),
        transaction_type=kind,
        amount_minor=amount,
        payment_method=method,
        reference_transaction_id=reference,
    )


def test_payment_totals_are_deterministic_and_refund_aware() -> None:
    appointment = SimpleNamespace(price_minor=200_000)
    first = _row(kind="payment", amount=50_000, method="cash")
    second = _row(kind="payment", amount=150_000, method="card")
    refund = _row(kind="refund", amount=30_000, method="card", reference=second.id)

    totals = _payment_totals(appointment=appointment, rows=[first, second, refund])
    assert totals.gross_paid_minor == 200_000
    assert totals.refunded_minor == 30_000
    assert totals.net_paid_minor == 170_000
    assert totals.balance_minor == 30_000
    assert totals.payment_status == "partial"
    assert totals.payment_method == "other"
    assert _refunds_by_payment([first, second, refund])[second.id] == 30_000


def test_full_refund_maps_compatibility_snapshot_to_refunded() -> None:
    appointment = SimpleNamespace(price_minor=200_000)
    payment = _row(kind="payment", amount=200_000, method="card")
    refund = _row(kind="refund", amount=200_000, method="card", reference=payment.id)
    totals = _payment_totals(appointment=appointment, rows=[payment, refund])
    assert totals.net_paid_minor == 0
    assert totals.balance_minor == 200_000
    assert totals.payment_status == "refunded"


def test_payment_ledger_model_is_append_only_for_financial_facts_and_workspace_scoped() -> None:
    root = _root()
    model = (root / "backend/app/models/payment_transaction.py").read_text(encoding="utf-8")
    route = (root / "backend/app/api/routes/payments.py").read_text(encoding="utf-8")
    router = (root / "backend/app/api/router.py").read_text(encoding="utf-8")

    assert '__tablename__ = "payment_transactions"' in model
    assert "TimestampMixin" not in model
    assert "origin_appointment_id" in model
    assert "reference_transaction_id" in model
    assert "amount_minor > 0" in model
    assert "uq_payment_transactions_workspace_idempotency_key" in model
    assert '@router.get("/appointments/{appointment_id}"' in route
    assert '"/appointments/{appointment_id}/payments"' in route
    assert '"/appointments/{appointment_id}/refunds"' in route
    assert "get_workspace_reader" in route
    assert "get_workspace_admin" in route
    assert 'prefix="/payments"' in router
    assert "@router.patch" not in route
    assert "@router.delete" not in route


def test_payment_writes_lock_appointment_bound_amounts_and_audit() -> None:
    source = (_root() / "backend/app/services/payments.py").read_text(encoding="utf-8")
    assert ".with_for_update()" in source
    assert "Payment exceeds outstanding balance" in source
    assert "Refund exceeds refundable amount" in source
    assert 'action="payment.recorded"' in source
    assert 'action="payment.refunded"' in source
    assert "sync_appointment_payment_snapshot" in source
    assert "Idempotency key was already used" in source
    assert "generative" not in source.lower()
    assert "llm" not in source.lower()
    assert "re.compile" not in source
    assert "re.search" not in source


def test_staff_payment_methods_are_cash_visa_instapay_only() -> None:
    assert STAFF_PAYMENT_METHODS == frozenset({"cash", "visa", "instapay"})


def test_reschedule_reallocates_ledger_without_copying_financial_facts() -> None:
    service = (_root() / "backend/app/services/appointment_operations.py").read_text(encoding="utf-8")
    payments = (_root() / "backend/app/services/payments.py").read_text(encoding="utf-8")
    assert "reallocate_appointment_payments_on_reschedule" in service
    assert ".values(appointment_id=to_appointment_id)" in payments
    assert "origin_appointment_id" in payments
    assert ".values(origin_appointment_id=to_appointment_id)" not in payments


def test_migration_backfills_legacy_snapshots_and_advances_readiness_head() -> None:
    root = _root()
    migration = (root / "backend/alembic/versions/0029_payment_ledger.py").read_text(encoding="utf-8")
    readiness = (root / "backend/app/services/operational_readiness.py").read_text(encoding="utf-8")
    assert 'revision: str = "0029_payment_ledger"' in migration
    assert 'down_revision: str | Sequence[str] | None = "0028_activity_audit_trail"' in migration
    assert 'op.create_table(\n        "payment_transactions"' in migration
    assert "legacy-payment" in migration
    assert "legacy-refund" in migration
    assert "a.payment_status IN ('paid', 'partial', 'refunded')" in migration
    assert 'EXPECTED_MIGRATION_HEAD = "0072_workspace_demo_policy"' in readiness


def test_historical_import_writes_canonical_payment_ledger_directly() -> None:
    source = (_root() / "backend/app/services/historical_import.py").read_text(encoding="utf-8")
    payments = (_root() / "backend/app/services/payments.py").read_text(encoding="utf-8")
    assert "PaymentTransaction(" in source
    assert 'source="integration"' in source
    assert 'transaction_type="refund" if signed_amount < 0 else "payment"' in source
    assert "if rows:" in payments
    assert "sync_appointment_payment_snapshot(appointment, list(rows))" in payments


def test_analytics_patient_timeline_and_appointment_ui_use_canonical_ledger() -> None:
    root = _root()
    analytics = (root / "backend/app/services/analytics.py").read_text(encoding="utf-8")
    timeline = (root / "backend/app/services/patient_timeline.py").read_text(encoding="utf-8")
    detail = (root / "frontend/src/app/(dashboard)/appointments/[appointmentId]/page.tsx").read_text(encoding="utf-8")
    payment_form = (root / "frontend/src/app/(dashboard)/appointments/[appointmentId]/appointment-payment-form.tsx").read_text(encoding="utf-8")
    actions = (root / "frontend/src/app/(dashboard)/appointments/[appointmentId]/actions.ts").read_text(encoding="utf-8")
    patient = (root / "frontend/src/app/(dashboard)/patients/[patientId]/page.tsx").read_text(encoding="utf-8")
    activity = (root / "frontend/src/app/(dashboard)/activity/page.tsx").read_text(encoding="utf-8")

    assert "PaymentTransaction" in analytics
    assert "gross_paid_minor" in analytics and "refunded_minor" in analytics
    assert "_build_payment_events" in timeline
    assert 'kind="payment"' in timeline
    assert "/payments/appointments/${appointmentId}" in detail
    assert "AppointmentPaymentForm" in detail
    assert "recordAppointmentPayment" in payment_form
    assert "refundAppointmentPayment" in detail
    assert 'Idempotency-Key' in actions
    assert 'event.kind === "payment"' in patient
    assert '"payment.recorded": "تم تسجيل دفعة"' in activity
    assert '["payment_transaction", "المدفوعات والاستردادات"]' in activity


def test_package_prepaid_session_is_settled_without_appointment_revenue() -> None:
    appointment = SimpleNamespace(
        price_minor=180_000,
        billing_context="package_prepaid",
    )
    totals = _payment_totals(appointment=appointment, rows=[], due_minor=0)
    assert totals.gross_paid_minor == 0
    assert totals.refunded_minor == 0
    assert totals.net_paid_minor == 0
    assert totals.balance_minor == 0
    assert totals.payment_status == "paid"
    assert totals.payment_method == "unknown"


def test_package_prepaid_products_remain_collectible() -> None:
    appointment = SimpleNamespace(
        price_minor=180_000,
        billing_context="package_prepaid",
    )
    unpaid = _payment_totals(appointment=appointment, rows=[], due_minor=35_000)
    assert unpaid.balance_minor == 35_000
    assert unpaid.payment_status == "unpaid"

    product_payment = _row(kind="payment", amount=35_000, method="cash")
    paid = _payment_totals(
        appointment=appointment,
        rows=[product_payment],
        due_minor=35_000,
    )
    assert paid.balance_minor == 0
    assert paid.payment_status == "paid"
