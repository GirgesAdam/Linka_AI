from types import SimpleNamespace
from uuid import uuid4

import app.services.patient_packages as packages
import app.services.payments as payments
import app.services.pulse_billing as pulses


class ReplayDb:
    def __init__(self, *scalar_results):
        self._scalar_results = iter(scalar_results)
        self.added = []

    def scalar(self, _statement):
        return next(self._scalar_results)

    def add(self, value):
        self.added.append(value)

    def flush(self):
        pass


def test_same_payment_operation_key_replays_without_second_mutation(monkeypatch) -> None:
    workspace_id = uuid4()
    appointment_id = uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        transaction_type="payment",
        amount_minor=100_000,
        payment_method="cash",
        source="staff",
        external_reference=None,
    )
    event = SimpleNamespace(
        metadata_json={
            "amount_minor": 100_000,
            "payment_method": "cash",
            "source": "staff",
            "discount_mode": "unchanged",
        }
    )
    db = ReplayDb(existing, event)
    monkeypatch.setattr(payments, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)
    monkeypatch.setattr(payments, "_transaction_has_allocation", lambda *args, **kwargs: True)

    result = payments.record_payment(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
        amount_minor=100_000,
        payment_method="cash",
        created_by_user_id=None,
        idempotency_key="dashboard-checkout:11111111-1111-4111-8111-111111111111",
    )

    assert result is existing
    assert db.added == []


def test_same_refund_operation_key_replays_without_second_mutation(monkeypatch) -> None:
    workspace_id = uuid4()
    appointment_id = uuid4()
    payment_transaction_id = uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        transaction_type="refund",
        reference_transaction_id=payment_transaction_id,
        amount_minor=50_000,
        reason="duplicate submit replay",
    )
    db = ReplayDb(existing)
    monkeypatch.setattr(payments, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)
    monkeypatch.setattr(payments, "_transaction_has_allocation", lambda *args, **kwargs: True)

    result = payments.record_refund(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
        payment_transaction_id=payment_transaction_id,
        amount_minor=50_000,
        reason="duplicate submit replay",
        created_by_user_id=uuid4(),
        idempotency_key="dashboard-refund:11111111-1111-4111-8111-111111111111",
    )

    assert result is existing
    assert db.added == []


def test_same_package_purchase_operation_key_replays_without_second_package(monkeypatch) -> None:
    workspace_id, patient_id, service_id = uuid4(), uuid4(), uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        patient_id=patient_id,
        service_id=service_id,
        purchase_transaction_id=None,
        name="6 sessions",
        sessions_purchased=6,
        sale_price_minor=480_000,
        expires_at=None,
        external_id=None,
        package_offer_id=None,
        origin_appointment_id=None,
        laser_device_key=None,
        laser_device_name=None,
        opening_sessions_remaining=None,
        sessions_total_known=True,
        standalone_session_price_minor_at_purchase=None,
    )
    db = ReplayDb(existing)

    result = packages.create_patient_package(
        db,
        workspace_id=workspace_id,
        patient_id=patient_id,
        service_id=service_id,
        name="6 sessions",
        sessions_purchased=6,
        sale_price_minor=480_000,
        amount_paid_minor=0,
        payment_method="unknown",
        created_by_user_id=None,
        idempotency_key="dashboard-package-purchase:11111111-1111-4111-8111-111111111111",
    )

    assert result is existing
    assert db.added == []


def test_same_package_payment_operation_key_replays_without_second_payment(monkeypatch) -> None:
    workspace_id, package_id = uuid4(), uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        transaction_type="payment",
        patient_package_id=package_id,
        amount_minor=100_000,
        payment_method="cash",
        external_reference=None,
    )
    db = ReplayDb(existing)
    monkeypatch.setattr(packages, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)

    result = packages.record_package_payment(
        db,
        workspace_id=workspace_id,
        package_id=package_id,
        amount_minor=100_000,
        payment_method="cash",
        created_by_user_id=None,
        idempotency_key="dashboard-package-payment:11111111-1111-4111-8111-111111111111",
    )

    assert result is existing
    assert db.added == []


def test_same_pulse_purchase_operation_key_replays_without_second_pack(monkeypatch) -> None:
    workspace_id, patient_id, offer_id = uuid4(), uuid4(), uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        patient_id=patient_id,
        pulse_pack_offer_id=offer_id,
        origin_appointment_id=None,
        purchase_transaction_id=None,
        expires_at=None,
    )
    db = ReplayDb(existing)

    result = pulses.purchase_pulse_pack_offer(
        db,
        workspace_id=workspace_id,
        patient_id=patient_id,
        offer_id=offer_id,
        amount_paid_minor=0,
        payment_method="unknown",
        created_by_user_id=None,
        idempotency_key="patient-pulse-pack:11111111-1111-4111-8111-111111111111",
    )

    assert result is existing
    assert db.added == []


def test_same_pulse_payment_operation_key_replays_without_second_payment(monkeypatch) -> None:
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
    monkeypatch.setattr(pulses, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)

    result = pulses.record_pulse_pack_payment(
        db,
        workspace_id=workspace_id,
        pack_id=pack_id,
        amount_minor=100_000,
        payment_method="cash",
        created_by_user_id=None,
        idempotency_key="patient-pulse-payment:11111111-1111-4111-8111-111111111111",
    )

    assert result is existing
    assert db.added == []
