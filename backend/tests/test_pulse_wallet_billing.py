from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.services.pulse_billing as pulse_service
from app.schemas.booking import AppointmentCreate
from app.schemas.pulse_billing import PulseBillingSettingsUpsert, PulsePackOfferUpsert
from app.services.pulse_billing import PulseBillingError


class FakeDb:
    def __init__(self, scalar_result=None):
        self.scalar_result = scalar_result
        self.added = []
        self.flush_count = 0

    def scalar(self, _statement):
        return self.scalar_result

    def add(self, value):
        self.added.append(value)

    def flush(self):
        self.flush_count += 1


def test_booking_schema_supports_pulse_balance_without_reserving_quantity() -> None:
    payload = AppointmentCreate(
        patient_id=uuid4(),
        branch_id=uuid4(),
        doctor_id=uuid4(),
        service_id=uuid4(),
        use_pulse_balance=True,
        start_at=datetime.now(UTC) + timedelta(days=1),
    )
    assert payload.use_pulse_balance is True
    assert not hasattr(payload, "pulse_quantity")


def test_booking_validation_checks_balance_on_appointment_date(monkeypatch) -> None:
    captured = {}
    appointment_start = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)

    def fake_balance(*args, **kwargs):
        captured.update(kwargs)
        return 25

    monkeypatch.setattr(pulse_service, "available_pulse_balance", fake_balance)
    pulse_service.validate_pulse_booking(
        object(),
        workspace_id=uuid4(),
        patient_id=uuid4(),
        device_key="candela_gentle",
        appointment_date=appointment_start.date(),
    )
    assert captured["on_date"] == date(2026, 10, 5)


def test_booking_validation_rejects_zero_balance(monkeypatch) -> None:
    monkeypatch.setattr(pulse_service, "available_pulse_balance", lambda *args, **kwargs: 0)
    with pytest.raises(PulseBillingError, match="no available pulse balance"):
        pulse_service.validate_pulse_booking(
            object(),
            workspace_id=uuid4(),
            patient_id=uuid4(),
            device_key="candela_gentle",
            appointment_date=(datetime.now(UTC) + timedelta(days=1)).date(),
        )


def test_pack_read_keeps_purchase_lot_and_reports_remaining(monkeypatch) -> None:
    now = datetime(2026, 9, 23, tzinfo=UTC)
    pack = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        patient_id=uuid4(),
        pulse_pack_offer_id=uuid4(),
        origin_appointment_id=None,
        purchase_transaction_id=uuid4(),
        device_key="candela_gentle",
        device_name="Candela Gentle",
        pulses_purchased=2000,
        sale_price_minor=450_000,
        standalone_pulse_price_minor_at_purchase=300,
        currency="EGP",
        purchased_at=now,
        expires_at=None,
        status="active",
        created_at=now,
        updated_at=now,
    )
    monkeypatch.setattr(pulse_service, "_consumed_for_pack", lambda *args, **kwargs: 650)
    monkeypatch.setattr(
        pulse_service,
        "_pack_financial_rows",
        lambda *args, **kwargs: (
            [SimpleNamespace(amount_minor=450_000)],
            [],
        ),
    )
    result = pulse_service.pulse_pack_read(object(), pack, include_financials=True)
    assert result.pulses_consumed == 650
    assert result.pulses_remaining == 1350
    assert result.amount_paid_minor == 450_000
    assert result.balance_due_minor == 0


def test_overage_resolution_snapshots_admin_price(monkeypatch) -> None:
    appointment = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        patient_id=uuid4(),
        laser_device_key="candela_gentle",
    )
    settlement = SimpleNamespace(
        id=uuid4(),
        deficit_pulses=150,
        resolution="pending",
        overage_unit_price_minor=None,
        overage_charge_minor=0,
        resolved_at=None,
    )
    settings = SimpleNamespace(overage_price_minor=300)
    db = FakeDb(settings)
    monkeypatch.setattr(
        pulse_service,
        "_locked_pending_settlement",
        lambda *args, **kwargs: (appointment, settlement),
    )
    monkeypatch.setattr(
        pulse_service,
        "refresh_appointment_payment_snapshots",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(pulse_service, "record_activity_event", lambda *args, **kwargs: None)

    result = pulse_service.resolve_pulse_deficit_with_overage(
        db,
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        changed_by_user_id=None,
    )

    assert result.resolution == "overage"
    assert result.overage_unit_price_minor == 300
    assert result.overage_charge_minor == 45_000
    assert result.resolved_at is not None


def test_new_pack_resolution_consumes_only_deficit(monkeypatch) -> None:
    workspace_id, patient_id, appointment_id = uuid4(), uuid4(), uuid4()
    appointment = SimpleNamespace(
        id=appointment_id,
        workspace_id=workspace_id,
        patient_id=patient_id,
        laser_device_key="candela_gentle",
    )
    settlement = SimpleNamespace(
        deficit_pulses=150,
        pulses_from_balance=300,
        resolution="pending",
        resolution_pulse_pack_id=None,
        overage_unit_price_minor=None,
        overage_charge_minor=0,
        resolved_at=None,
    )
    offer = SimpleNamespace(
        id=uuid4(),
        device_key="candela_gentle",
        pulses_count=1000,
        price_minor=250_000,
    )
    pack = SimpleNamespace(id=uuid4())
    db = FakeDb()
    monkeypatch.setattr(
        pulse_service,
        "_locked_pending_settlement",
        lambda *args, **kwargs: (appointment, settlement),
    )
    monkeypatch.setattr(pulse_service, "_get_active_offer", lambda *args, **kwargs: offer)
    monkeypatch.setattr(
        pulse_service,
        "purchase_pulse_pack_offer",
        lambda *args, **kwargs: pack,
    )
    monkeypatch.setattr(
        pulse_service,
        "refresh_appointment_payment_snapshots",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(pulse_service, "record_activity_event", lambda *args, **kwargs: None)

    result = pulse_service.resolve_pulse_deficit_with_pack(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
        offer_id=offer.id,
        amount_paid_minor=offer.price_minor,
        payment_method="cash",
        external_reference=None,
        changed_by_user_id=None,
        idempotency_key="pulse-pack-test",
    )

    usages = [item for item in db.added if item.__class__.__name__ == "PulseUsage"]
    assert len(usages) == 1
    assert usages[0].pulses_used == 150
    assert result.pulses_from_balance == 450
    assert result.resolution == "new_pack"
    assert result.resolution_pulse_pack_id == pack.id


def test_completion_requires_resolved_pulse_settlement(monkeypatch) -> None:
    appointment = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        billing_context="pulse_prepaid",
        laser_pulses_used=450,
    )
    monkeypatch.setattr(
        pulse_service,
        "get_appointment_pulse_settlement",
        lambda *args, **kwargs: SimpleNamespace(resolution="pending"),
    )
    with pytest.raises(PulseBillingError, match="Resolve the pulse balance deficit"):
        pulse_service.require_pulse_settlement_before_completion(
            object(),
            appointment=appointment,
        )


def test_migration_and_payment_contract_include_pulse_billing() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    migration = (root / "alembic/versions/0082_pulse_wallet_billing.py").read_text(
        encoding="utf-8"
    )
    payments = (root / "app/services/payments.py").read_text(encoding="utf-8")
    finance = (root / "app/services/finance.py").read_text(encoding="utf-8")
    assert "pulse_pack_offers" in migration
    assert "patient_pulse_packs" in migration
    assert "pulse_usages" in migration
    assert "appointment_pulse_settlements" in migration
    assert "pulse_prepaid" in migration
    assert "pulse_pack_sales_total_minor" in payments
    assert "pulse_overage_total_minor" in payments
    assert "pulse_prepaid" in finance


def test_pulse_wallet_migration_compiles_for_postgresql(monkeypatch) -> None:
    import importlib.util
    import io
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    root = Path(__file__).resolve().parents[1]
    path = root / "alembic/versions/0082_pulse_wallet_billing.py"
    spec = importlib.util.spec_from_file_location("migration_0082_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(module, "op", Operations(context))
    module.upgrade()
    module.downgrade()

    sql = output.getvalue().lower()
    assert "create table pulse_billing_settings" in sql
    assert "create table pulse_pack_offers" in sql
    assert "create table patient_pulse_packs" in sql
    assert "create table pulse_usages" in sql
    assert "create table appointment_pulse_settlements" in sql
    assert "pulse_prepaid" in sql


def test_pulse_prepaid_visit_charges_only_pack_sales_and_overage() -> None:
    import app.services.payments as payment_service

    class ChargeDb:
        def __init__(self):
            self.values = iter([0, 0, 0, 250_000, 45_000, 0])

        def scalar(self, _statement):
            return next(self.values)

    appointment = SimpleNamespace(
        id=uuid4(),
        price_minor=100_000,
        billing_context="pulse_prepaid",
    )
    breakdown = payment_service._appointment_charge_breakdown(
        ChargeDb(),
        workspace_id=uuid4(),
        appointment=appointment,
    )
    assert breakdown == (100_000, 0, 0, 0, 250_000, 45_000, 295_000)


def test_pulse_schemas_accept_arbitrary_clinic_device_keys() -> None:
    settings = PulseBillingSettingsUpsert(
        device_key="device_deka_again",
        overage_price_minor=150,
        currency="EGP",
    )
    offer = PulsePackOfferUpsert(
        device_key="device_deka_again",
        pulses_count=1000,
        price_minor=120_000,
        currency="EGP",
    )

    assert settings.device_key == "device_deka_again"
    assert offer.device_key == "device_deka_again"
