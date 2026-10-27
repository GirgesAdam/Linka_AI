from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.api.routes.booking as booking_routes
import app.api.routes.package_offers as package_offer_routes
import app.api.routes.payments as payment_routes
import app.api.routes.pulse_billing as pulse_routes
import app.services.package_offers as package_offers
import app.services.patient_packages as packages
import app.services.payments as payments
import app.services.pulse_billing as pulses
from app.models.payment_transaction import PaymentTransaction
from app.services.patient_packages import PackageOperationError
from app.services.payments import PaymentOperationError
from app.services.pulse_billing import PulseBillingError


class ScalarRows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class ReplayDb:
    def __init__(self, *scalar_results, scalar_rows=()):
        self._scalar_results = iter(scalar_results)
        self._scalar_rows = iter(scalar_rows)
        self.added = []
        self.flush_count = 0

    def scalar(self, _statement):
        return next(self._scalar_results)

    def scalars(self, _statement):
        return ScalarRows(next(self._scalar_rows))

    def add(self, value):
        self.added.append(value)

    def flush(self):
        self.flush_count += 1


def _payment_transaction(
    *,
    appointment_id: UUID,
    amount_minor: int = 50_000,
    payment_method: str = "cash",
    external_reference: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        transaction_type="payment",
        amount_minor=amount_minor,
        payment_method=payment_method,
        source="staff",
        external_reference=external_reference,
        appointment_id=appointment_id,
    )


def _payment_event(*, amount_minor: int = 50_000) -> SimpleNamespace:
    return SimpleNamespace(
        metadata_json={
            "amount_minor": amount_minor,
            "payment_method": "cash",
            "source": "staff",
            "discount_mode": "unchanged",
        }
    )


def _package(
    *,
    patient_id: UUID,
    service_id: UUID,
    purchase_transaction_id: UUID | None = None,
    package_offer_id: UUID | None = None,
    origin_appointment_id: UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        patient_id=patient_id,
        service_id=service_id,
        purchase_transaction_id=purchase_transaction_id,
        package_offer_id=package_offer_id,
        origin_appointment_id=origin_appointment_id,
        name="6 sessions",
        sessions_purchased=6,
        sale_price_minor=480_000,
        expires_at=None,
        external_id=None,
        laser_device_key=None,
        laser_device_name=None,
        opening_sessions_remaining=None,
        sessions_total_known=True,
        standalone_session_price_minor_at_purchase=None,
        purchased_at=datetime(2026, 10, 27, tzinfo=UTC),
    )


def _package_purchase_transaction(
    package_id: UUID,
    *,
    amount_minor: int,
    payment_method: str = "cash",
    external_reference: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        transaction_type="payment",
        patient_package_id=package_id,
        amount_minor=amount_minor,
        payment_method=payment_method,
        external_reference=external_reference,
        source="staff",
        reason="Prepaid package purchase",
    )


def _pulse_pack(
    *,
    patient_id: UUID,
    offer_id: UUID,
    purchase_transaction_id: UUID | None = None,
    origin_appointment_id: UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        patient_id=patient_id,
        pulse_pack_offer_id=offer_id,
        purchase_transaction_id=purchase_transaction_id,
        origin_appointment_id=origin_appointment_id,
        expires_at=None,
    )


def _pulse_purchase_transaction(
    pack_id: UUID,
    *,
    amount_minor: int,
    payment_method: str = "cash",
    external_reference: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        transaction_type="payment",
        patient_pulse_pack_id=pack_id,
        amount_minor=amount_minor,
        payment_method=payment_method,
        external_reference=external_reference,
        source="staff",
        reason="Prepaid pulse pack purchase",
    )


def test_payment_exact_replay_keeps_same_result_and_zero_mutation(monkeypatch) -> None:
    workspace_id, appointment_id = uuid4(), uuid4()
    existing = _payment_transaction(appointment_id=appointment_id)
    db = ReplayDb(existing, _payment_event())
    monkeypatch.setattr(payments, "require_tia_workspace_domain_write", lambda *a, **k: None)
    monkeypatch.setattr(payments, "_transaction_has_allocation", lambda *a, **k: True)

    result = payments.record_payment(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
        amount_minor=50_000,
        payment_method="cash",
        created_by_user_id=None,
        idempotency_key="payment-replay",
    )

    assert result is existing
    assert db.added == []


def test_payment_false_success_changed_amount_conflicts_and_records_nothing(monkeypatch) -> None:
    workspace_id, appointment_id = uuid4(), uuid4()
    existing = _payment_transaction(appointment_id=appointment_id, amount_minor=50_000)
    db = ReplayDb(existing, _payment_event(amount_minor=50_000))
    monkeypatch.setattr(payments, "require_tia_workspace_domain_write", lambda *a, **k: None)
    monkeypatch.setattr(payments, "_transaction_has_allocation", lambda *a, **k: True)

    with pytest.raises(PaymentOperationError, match="different payment operation"):
        payments.record_payment(
            db,
            workspace_id=workspace_id,
            appointment_id=appointment_id,
            amount_minor=100_000,
            payment_method="cash",
            created_by_user_id=None,
            idempotency_key="payment-500-then-1000",
        )

    assert existing.amount_minor == 50_000
    assert db.added == []


def test_payment_changed_method_and_target_conflict_before_mutation(monkeypatch) -> None:
    workspace_id, appointment_id = uuid4(), uuid4()
    existing = _payment_transaction(appointment_id=appointment_id)
    monkeypatch.setattr(payments, "require_tia_workspace_domain_write", lambda *a, **k: None)
    monkeypatch.setattr(payments, "_transaction_has_allocation", lambda *a, **k: True)
    db = ReplayDb(existing, _payment_event())
    with pytest.raises(PaymentOperationError, match="different payment operation"):
        payments.record_payment(
            db,
            workspace_id=workspace_id,
            appointment_id=appointment_id,
            amount_minor=50_000,
            payment_method="visa",
            created_by_user_id=None,
            idempotency_key="payment-method-mismatch",
        )
    assert db.added == []

    monkeypatch.setattr(payments, "_transaction_has_allocation", lambda *a, **k: False)
    db = ReplayDb(existing, _payment_event())
    with pytest.raises(PaymentOperationError, match="different payment operation"):
        payments.record_payment(
            db,
            workspace_id=workspace_id,
            appointment_id=uuid4(),
            amount_minor=50_000,
            payment_method="cash",
            created_by_user_id=None,
            idempotency_key="payment-target-mismatch",
        )
    assert db.added == []


def test_refund_exact_replay_and_changed_target_amount_reason_conflict(monkeypatch) -> None:
    workspace_id, appointment_id, payment_id = uuid4(), uuid4(), uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        transaction_type="refund",
        reference_transaction_id=payment_id,
        amount_minor=25_000,
        reason="duplicate charge",
    )
    monkeypatch.setattr(payments, "require_tia_workspace_domain_write", lambda *a, **k: None)
    monkeypatch.setattr(payments, "_transaction_has_allocation", lambda *a, **k: True)

    exact_db = ReplayDb(existing)
    assert (
        payments.record_refund(
            exact_db,
            workspace_id=workspace_id,
            appointment_id=appointment_id,
            payment_transaction_id=payment_id,
            amount_minor=25_000,
            reason="duplicate charge",
            created_by_user_id=uuid4(),
            idempotency_key="refund-replay",
        )
        is existing
    )
    assert exact_db.added == []

    for changed_payment_id, amount, reason in (
        (uuid4(), 25_000, "duplicate charge"),
        (payment_id, 30_000, "duplicate charge"),
        (payment_id, 25_000, "different reason"),
    ):
        db = ReplayDb(existing)
        with pytest.raises(PaymentOperationError, match="different refund operation"):
            payments.record_refund(
                db,
                workspace_id=workspace_id,
                appointment_id=appointment_id,
                payment_transaction_id=changed_payment_id,
                amount_minor=amount,
                reason=reason,
                created_by_user_id=uuid4(),
                idempotency_key="refund-replay",
            )
        assert db.added == []


def test_package_sale_exact_paid_replay_and_changed_initial_payment_conflict() -> None:
    workspace_id, patient_id, service_id = uuid4(), uuid4(), uuid4()
    package = _package(patient_id=patient_id, service_id=service_id)
    purchase = _package_purchase_transaction(package.id, amount_minor=100_000)
    package.purchase_transaction_id = purchase.id

    exact_db = ReplayDb(package, purchase)
    assert (
        packages.create_patient_package(
            exact_db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 sessions",
            sessions_purchased=6,
            sale_price_minor=480_000,
            amount_paid_minor=100_000,
            payment_method="cash",
            created_by_user_id=None,
            idempotency_key="package-sale-replay",
        )
        is package
    )
    assert exact_db.added == []

    mismatch_db = ReplayDb(package, purchase)
    with pytest.raises(PackageOperationError, match="different package sale"):
        packages.create_patient_package(
            mismatch_db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 sessions",
            sessions_purchased=6,
            sale_price_minor=480_000,
            amount_paid_minor=200_000,
            payment_method="cash",
            created_by_user_id=None,
            idempotency_key="package-sale-replay",
        )
    assert mismatch_db.added == []


def test_package_offer_purchase_replay_does_not_revalidate_mutable_offer(monkeypatch) -> None:
    workspace_id, patient_id, service_id, offer_id = uuid4(), uuid4(), uuid4(), uuid4()
    package = _package(
        patient_id=patient_id,
        service_id=service_id,
        package_offer_id=offer_id,
    )
    db = ReplayDb(package)
    monkeypatch.setattr(
        package_offers,
        "get_active_package_offer",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("offer must not be reread on replay")),
    )

    result = package_offers.purchase_package_offer(
        db,
        workspace_id=workspace_id,
        patient_id=patient_id,
        offer_id=offer_id,
        amount_paid_minor=0,
        payment_method="unknown",
        created_by_user_id=None,
        idempotency_key="package-offer-replay",
    )

    assert result is package
    assert db.added == []


def test_package_payment_changed_method_conflicts_and_new_key_operation_is_allowed(monkeypatch) -> None:
    workspace_id, package_id = uuid4(), uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        transaction_type="payment",
        patient_package_id=package_id,
        amount_minor=100_000,
        payment_method="cash",
        external_reference=None,
    )
    monkeypatch.setattr(packages, "require_tia_workspace_domain_write", lambda *a, **k: None)
    mismatch_db = ReplayDb(existing)
    with pytest.raises(PackageOperationError, match="different package payment"):
        packages.record_package_payment(
            mismatch_db,
            workspace_id=workspace_id,
            package_id=package_id,
            amount_minor=100_000,
            payment_method="visa",
            created_by_user_id=None,
            idempotency_key="package-payment-replay",
        )
    assert mismatch_db.added == []

    package = SimpleNamespace(
        id=package_id,
        origin_appointment_id=None,
        patient_id=uuid4(),
        currency="EGP",
        sale_price_minor=500_000,
        purchase_transaction_id=None,
        status="active",
        expires_at=None,
    )
    new_db = ReplayDb(None)
    monkeypatch.setattr(packages, "_locked_package", lambda *a, **k: package)
    monkeypatch.setattr(packages, "_package_financial_rows", lambda *a, **k: ([], []))
    monkeypatch.setattr(packages, "record_activity_event", lambda *a, **k: None)
    transaction = packages.record_package_payment(
        new_db,
        workspace_id=workspace_id,
        package_id=package_id,
        amount_minor=200_000,
        payment_method="visa",
        created_by_user_id=None,
        idempotency_key="new-package-payment-key",
    )
    assert transaction.amount_minor == 200_000
    assert transaction.idempotency_key == "new-package-payment-key"
    assert len(new_db.added) == 1


def _cancellation_event(package_id: UUID, reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        entity_id=package_id,
        metadata_json={
            "idempotency_key": "package-cancel-replay",
            "reason_sha256": packages._package_cancellation_reason_digest(reason),
            "settlement_target_mode": "default",
            "standalone_price_mode": "unchanged",
            "require_reversible_unpaid": False,
            "expected_patient_id": None,
            "collected_minor": 300_000,
            "consumed_value_minor": 100_000,
            "settlement_target_minor": 100_000,
            "previously_refunded_minor": 0,
            "collected_now_minor": 0,
            "refunded_now_minor": 200_000,
            "refund_transaction_ids": [],
        },
    )


def test_package_cancellation_exact_replay_and_changed_meaning_conflict(monkeypatch) -> None:
    workspace_id, package_id = uuid4(), uuid4()
    package = SimpleNamespace(id=package_id, workspace_id=workspace_id)
    reason = "customer requested cancellation"
    event = _cancellation_event(package_id, reason)
    monkeypatch.setattr(packages, "_locked_package", lambda *a, **k: package)

    exact_db = ReplayDb(event)
    monkeypatch.setattr(packages, "_package_cancellation_event_by_key", lambda *a, **k: event)
    result = packages.cancel_patient_package_with_refund(
        exact_db,
        workspace_id=workspace_id,
        package_id=package_id,
        reason=reason,
        created_by_user_id=None,
        idempotency_key="package-cancel-replay",
    )
    assert result[0] is package
    assert result[1:7] == (300_000, 100_000, 100_000, 0, 0, 200_000)
    assert exact_db.added == []

    mismatch_db = ReplayDb(event)
    with pytest.raises(PackageOperationError, match="different package cancellation"):
        packages.cancel_patient_package_with_refund(
            mismatch_db,
            workspace_id=workspace_id,
            package_id=package_id,
            reason="different reason",
            created_by_user_id=None,
            idempotency_key="package-cancel-replay",
        )
    assert mismatch_db.added == []


def test_pulse_sale_exact_paid_replay_and_changed_payment_conflict(monkeypatch) -> None:
    workspace_id, patient_id, offer_id = uuid4(), uuid4(), uuid4()
    pack = _pulse_pack(patient_id=patient_id, offer_id=offer_id)
    purchase = _pulse_purchase_transaction(pack.id, amount_minor=100_000)
    pack.purchase_transaction_id = purchase.id
    monkeypatch.setattr(
        pulses,
        "_get_active_offer",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("offer must not be reread on replay")),
    )

    exact_db = ReplayDb(pack, purchase)
    assert (
        pulses.purchase_pulse_pack_offer(
            exact_db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            offer_id=offer_id,
            amount_paid_minor=100_000,
            payment_method="cash",
            created_by_user_id=None,
            idempotency_key="pulse-sale-replay",
        )
        is pack
    )
    assert exact_db.added == []

    mismatch_db = ReplayDb(pack, purchase)
    with pytest.raises(PulseBillingError, match="different pulse pack sale"):
        pulses.purchase_pulse_pack_offer(
            mismatch_db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            offer_id=offer_id,
            amount_paid_minor=125_000,
            payment_method="cash",
            created_by_user_id=None,
            idempotency_key="pulse-sale-replay",
        )
    assert mismatch_db.added == []


def test_pulse_payment_changed_method_conflicts_before_mutation(monkeypatch) -> None:
    workspace_id, pack_id = uuid4(), uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        transaction_type="payment",
        patient_pulse_pack_id=pack_id,
        amount_minor=100_000,
        payment_method="cash",
        external_reference=None,
    )
    db = ReplayDb(existing)
    monkeypatch.setattr(pulses, "require_tia_workspace_domain_write", lambda *a, **k: None)

    with pytest.raises(PulseBillingError, match="different pulse pack payment"):
        pulses.record_pulse_pack_payment(
            db,
            workspace_id=workspace_id,
            pack_id=pack_id,
            amount_minor=100_000,
            payment_method="visa",
            created_by_user_id=None,
            idempotency_key="pulse-payment-replay",
        )
    assert db.added == []


def test_idempotency_key_uniqueness_remains_workspace_scoped() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    PaymentTransaction.__table__.create(engine)
    workspace_a, workspace_b = uuid4(), uuid4()
    patient_a, patient_b = uuid4(), uuid4()
    key = "same-key-different-workspaces"
    with Session(engine) as db:
        db.add_all(
            [
                PaymentTransaction(
                    workspace_id=workspace_a,
                    patient_id=patient_a,
                    transaction_type="payment",
                    amount_minor=10_000,
                    currency="EGP",
                    payment_method="cash",
                    source="staff",
                    idempotency_key=key,
                    created_at=datetime.now(UTC),
                ),
                PaymentTransaction(
                    workspace_id=workspace_b,
                    patient_id=patient_b,
                    transaction_type="payment",
                    amount_minor=20_000,
                    currency="EGP",
                    payment_method="cash",
                    source="staff",
                    idempotency_key=key,
                    created_at=datetime.now(UTC),
                ),
            ]
        )
        db.flush()
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 2
        assert (
            db.scalar(
                select(PaymentTransaction).where(
                    PaymentTransaction.workspace_id == workspace_a,
                    PaymentTransaction.idempotency_key == key,
                )
            ).amount_minor
            == 10_000
        )
        assert (
            db.scalar(
                select(PaymentTransaction).where(
                    PaymentTransaction.workspace_id == workspace_b,
                    PaymentTransaction.idempotency_key == key,
                )
            ).amount_minor
            == 20_000
        )


def test_domain_idempotency_mismatches_map_to_http_409() -> None:
    assert payment_routes._conflict("mismatch").status_code == 409
    assert booking_routes.booking_conflict("mismatch").status_code == 409

    with pytest.raises(Exception) as package_exc:
        package_offer_routes._raise(PackageOperationError("mismatch"))
    assert package_exc.value.status_code == 409

    with pytest.raises(Exception) as pulse_exc:
        pulse_routes._raise(PulseBillingError("mismatch"))
    assert pulse_exc.value.status_code == 409



def test_payment_changed_discount_conflicts_before_mutation(monkeypatch) -> None:
    workspace_id, appointment_id = uuid4(), uuid4()
    existing = _payment_transaction(appointment_id=appointment_id)
    event = SimpleNamespace(
        metadata_json={
            "amount_minor": 50_000,
            "payment_method": "cash",
            "source": "staff",
            "discount_mode": "set",
            "discount_minor": 10_000,
        }
    )
    db = ReplayDb(existing, event)
    monkeypatch.setattr(payments, "require_tia_workspace_domain_write", lambda *a, **k: None)
    monkeypatch.setattr(payments, "_transaction_has_allocation", lambda *a, **k: True)

    with pytest.raises(PaymentOperationError, match="different payment operation"):
        payments.record_payment(
            db,
            workspace_id=workspace_id,
            appointment_id=appointment_id,
            amount_minor=50_000,
            payment_method="cash",
            discount_minor=20_000,
            created_by_user_id=None,
            idempotency_key="payment-discount-mismatch",
        )
    assert db.added == []


def test_package_purchase_changed_method_and_offer_target_conflict() -> None:
    workspace_id, patient_id, service_id, offer_id = uuid4(), uuid4(), uuid4(), uuid4()
    package = _package(
        patient_id=patient_id,
        service_id=service_id,
        package_offer_id=offer_id,
    )
    purchase = _package_purchase_transaction(package.id, amount_minor=100_000)
    package.purchase_transaction_id = purchase.id

    method_db = ReplayDb(package, purchase)
    with pytest.raises(PackageOperationError, match="different package offer purchase"):
        package_offers.purchase_package_offer(
            method_db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            offer_id=offer_id,
            amount_paid_minor=100_000,
            payment_method="visa",
            created_by_user_id=None,
            idempotency_key="package-offer-paid-replay",
        )
    assert method_db.added == []

    target_db = ReplayDb(package, purchase)
    with pytest.raises(PackageOperationError, match="different package offer purchase"):
        package_offers.purchase_package_offer(
            target_db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            offer_id=uuid4(),
            amount_paid_minor=100_000,
            payment_method="cash",
            created_by_user_id=None,
            idempotency_key="package-offer-paid-replay",
        )
    assert target_db.added == []


def test_pulse_purchase_changed_method_and_offer_target_conflict(monkeypatch) -> None:
    workspace_id, patient_id, offer_id = uuid4(), uuid4(), uuid4()
    pack = _pulse_pack(patient_id=patient_id, offer_id=offer_id)
    purchase = _pulse_purchase_transaction(pack.id, amount_minor=100_000)
    pack.purchase_transaction_id = purchase.id
    monkeypatch.setattr(
        pulses,
        "_get_active_offer",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("offer must not be reread on replay")),
    )

    method_db = ReplayDb(pack, purchase)
    with pytest.raises(PulseBillingError, match="different pulse pack sale"):
        pulses.purchase_pulse_pack_offer(
            method_db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            offer_id=offer_id,
            amount_paid_minor=100_000,
            payment_method="visa",
            created_by_user_id=None,
            idempotency_key="pulse-sale-paid-replay",
        )
    assert method_db.added == []

    target_db = ReplayDb(pack, purchase)
    with pytest.raises(PulseBillingError, match="different pulse pack sale"):
        pulses.purchase_pulse_pack_offer(
            target_db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            offer_id=uuid4(),
            amount_paid_minor=100_000,
            payment_method="cash",
            created_by_user_id=None,
            idempotency_key="pulse-sale-paid-replay",
        )
    assert target_db.added == []


def test_package_cancellation_changed_settlement_payment_method_conflicts(monkeypatch) -> None:
    workspace_id, package_id = uuid4(), uuid4()
    package = SimpleNamespace(id=package_id, workspace_id=workspace_id)
    reason = "settle consumed value"
    event = _cancellation_event(package_id, reason)
    event.metadata_json["collected_now_minor"] = 50_000
    event.metadata_json["payment_method"] = "cash"
    monkeypatch.setattr(packages, "_locked_package", lambda *a, **k: package)
    monkeypatch.setattr(packages, "_package_cancellation_event_by_key", lambda *a, **k: event)
    db = ReplayDb()

    with pytest.raises(PackageOperationError, match="different package cancellation"):
        packages.cancel_patient_package_with_refund(
            db,
            workspace_id=workspace_id,
            package_id=package_id,
            reason=reason,
            payment_method="visa",
            created_by_user_id=None,
            idempotency_key="package-cancel-replay",
        )
    assert db.added == []
