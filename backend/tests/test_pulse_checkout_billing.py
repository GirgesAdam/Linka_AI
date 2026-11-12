from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.services.payments as payment_service
import app.services.pulse_billing as pulse_service
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


def _appointment(*, pulses_used: int = 1200, billing_context: str = "standard"):
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        patient_id=uuid4(),
        status="confirmed",
        billing_context=billing_context,
        patient_package_id=None,
        laser_device_key="candela_gentle",
        laser_pulses_used=pulses_used,
        currency="EGP",
        price_minor=100_000,
    )


def _stub_checkout_side_effects(monkeypatch, *, consumed: int) -> None:
    monkeypatch.setattr(
        pulse_service,
        "get_appointment_pulse_settlement",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        pulse_service,
        "_appointment_net_paid_minor",
        lambda *args, **kwargs: 0,
    )
    monkeypatch.setattr(
        pulse_service,
        "_reverse_existing_appointment_usages",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        pulse_service,
        "_consume_from_available_packs",
        lambda *args, **kwargs: consumed,
    )
    monkeypatch.setattr(
        pulse_service,
        "refresh_appointment_payment_snapshots",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        pulse_service,
        "record_activity_event",
        lambda *args, **kwargs: None,
    )


def test_checkout_uses_existing_balance_and_removes_base_service_charge(monkeypatch) -> None:
    appointment = _appointment(pulses_used=1200)
    db = FakeDb(appointment)
    _stub_checkout_side_effects(monkeypatch, consumed=1200)

    settlement = pulse_service.checkout_appointment_pulses(
        db,
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        mode="use_balance",
        offer_id=None,
        changed_by_user_id=None,
        idempotency_key="checkout-balance",
    )

    assert appointment.billing_context == "pulse_prepaid"
    assert settlement.pulses_used == 1200
    assert settlement.pulses_from_balance == 1200
    assert settlement.deficit_pulses == 0
    assert settlement.resolution == "balance"


def test_checkout_consumes_old_balance_then_only_deficit_from_new_pack(monkeypatch) -> None:
    appointment = _appointment(pulses_used=1200)
    db = FakeDb(appointment)
    _stub_checkout_side_effects(monkeypatch, consumed=700)

    offer = SimpleNamespace(
        id=uuid4(),
        device_key="candela_gentle",
        pulses_count=1000,
        price_minor=250_000,
    )
    pack = SimpleNamespace(id=uuid4())
    purchase_args = {}
    monkeypatch.setattr(
        pulse_service,
        "_get_active_offer",
        lambda *args, **kwargs: offer,
    )

    def fake_purchase(*args, **kwargs):
        purchase_args.update(kwargs)
        return pack

    monkeypatch.setattr(pulse_service, "purchase_pulse_pack_offer", fake_purchase)

    settlement = pulse_service.checkout_appointment_pulses(
        db,
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        mode="purchase_pack",
        offer_id=offer.id,
        changed_by_user_id=None,
        idempotency_key="checkout-pack",
    )

    usages = [row for row in db.added if row.__class__.__name__ == "PulseUsage"]
    assert len(usages) == 1
    assert usages[0].patient_pulse_pack_id == pack.id
    assert usages[0].pulses_used == 500
    assert settlement.pulses_from_balance == 1200
    assert settlement.deficit_pulses == 500
    assert settlement.resolution == "new_pack"
    assert settlement.resolution_pulse_pack_id == pack.id
    assert purchase_args["amount_paid_minor"] == 0
    assert purchase_args["origin_appointment_id"] == appointment.id


def test_checkout_overage_uses_selected_device_price(monkeypatch) -> None:
    appointment = _appointment(pulses_used=1200)
    db = FakeDb(appointment)
    _stub_checkout_side_effects(monkeypatch, consumed=700)
    captured = {}

    def fake_price(*args, **kwargs):
        captured.update(kwargs)
        return 300, "EGP"

    monkeypatch.setattr(pulse_service, "_device_overage_price", fake_price)

    settlement = pulse_service.checkout_appointment_pulses(
        db,
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        mode="overage",
        offer_id=None,
        changed_by_user_id=None,
        idempotency_key="checkout-overage",
    )

    assert captured["device_key"] == "candela_gentle"
    assert settlement.deficit_pulses == 500
    assert settlement.overage_unit_price_minor == 300
    assert settlement.overage_charge_minor == 150_000
    assert settlement.resolution == "overage"


def test_checkout_rejects_switch_after_standard_payment(monkeypatch) -> None:
    appointment = _appointment()
    db = FakeDb(appointment)
    monkeypatch.setattr(
        pulse_service,
        "get_appointment_pulse_settlement",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        pulse_service,
        "_appointment_net_paid_minor",
        lambda *args, **kwargs: 10_000,
    )

    with pytest.raises(PulseBillingError, match="already has recorded payment"):
        pulse_service.checkout_appointment_pulses(
            db,
            workspace_id=appointment.workspace_id,
            appointment_id=appointment.id,
            mode="overage",
            offer_id=None,
            changed_by_user_id=None,
            idempotency_key="checkout-paid",
        )

    assert appointment.billing_context == "standard"


def test_checkout_rejects_pack_for_other_device(monkeypatch) -> None:
    appointment = _appointment(pulses_used=1200)
    db = FakeDb(appointment)
    _stub_checkout_side_effects(monkeypatch, consumed=0)
    offer = SimpleNamespace(
        id=uuid4(),
        device_key="prime_lase",
        pulses_count=2000,
        price_minor=250_000,
    )
    monkeypatch.setattr(
        pulse_service,
        "_get_active_offer",
        lambda *args, **kwargs: offer,
    )

    with pytest.raises(PulseBillingError, match="different laser device"):
        pulse_service.checkout_appointment_pulses(
            db,
            workspace_id=appointment.workspace_id,
            appointment_id=appointment.id,
            mode="purchase_pack",
            offer_id=offer.id,
            changed_by_user_id=None,
            idempotency_key="checkout-wrong-device",
        )


def test_record_payment_allocates_visit_pulse_pack_before_general_balance(monkeypatch) -> None:
    appointment = _appointment(pulses_used=0, billing_context="pulse_prepaid")
    pack = SimpleNamespace(
        id=uuid4(),
        sale_price_minor=250_000,
        purchase_transaction_id=None,
    )
    db = FakeDb()

    monkeypatch.setattr(
        payment_service,
        "require_tia_workspace_domain_write",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        payment_service,
        "_locked_appointment",
        lambda *args, **kwargs: appointment,
    )
    monkeypatch.setattr(
        payment_service,
        "_ledger_rows",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        payment_service,
        "_appointment_charge_breakdown",
        lambda *args, **kwargs: (100_000, 0, 0, 0, 250_000, 0, 250_000),
    )
    monkeypatch.setattr(
        payment_service,
        "_visit_packages",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        payment_service,
        "_visit_pulse_packs",
        lambda *args, **kwargs: [pack],
    )
    monkeypatch.setattr(
        payment_service,
        "_pulse_pack_net_paid_minor",
        lambda *args, **kwargs: 0,
    )
    monkeypatch.setattr(
        payment_service,
        "_add_single_appointment_allocation",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        payment_service,
        "sync_appointment_payment_snapshot",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        payment_service,
        "record_activity_event",
        lambda *args, **kwargs: None,
    )

    transaction = payment_service.record_payment(
        db,
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        amount_minor=250_000,
        payment_method="cash",
        created_by_user_id=None,
        source="staff",
    )

    assert transaction.patient_pulse_pack_id == pack.id
    assert transaction.patient_package_id is None
    assert transaction.amount_minor == 250_000
    assert transaction.reason == "Pulse pack payment from appointment checkout"


def test_device_pricing_migration_contract() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    migration = (
        root / "alembic/versions/0083_pulse_device_pricing_checkout.py"
    ).read_text(encoding="utf-8")
    model = (root / "app/models/pulse_billing.py").read_text(encoding="utf-8")

    assert 'revision: str = "0083_pulse_device_pricing"' in migration
    assert 'down_revision: str | Sequence[str] | None = "0082_pulse_wallet_billing"' in migration
    assert "uq_pulse_billing_settings_workspace_device" in migration
    assert "device_key" in migration
    assert "uq_pulse_billing_settings_workspace_device" in model


def _additional_line(*, device_key: str = "candela_gentle"):
    return SimpleNamespace(
        id=uuid4(),
        patient_package_id=None,
        billing_context="standard",
        laser_device_key=device_key,
        laser_pulses_used=None,
        pulse_resolution=None,
        pulse_resolution_pulse_pack_id=None,
        pulse_overage_unit_price_minor=None,
        pulse_overage_charge_minor=0,
    )


def test_additional_service_can_use_existing_pulse_balance(monkeypatch) -> None:
    appointment = _appointment(pulses_used=0)
    line = _additional_line()
    db = FakeDb()
    monkeypatch.setattr(
        pulse_service,
        "_consume_from_available_packs",
        lambda *args, **kwargs: 450,
    )
    monkeypatch.setattr(
        pulse_service,
        "record_activity_event",
        lambda *args, **kwargs: None,
    )

    result = pulse_service.apply_additional_service_pulse_billing(
        db,
        appointment=appointment,
        line=line,
        pulses_used=450,
        mode="use_balance",
        offer_id=None,
        changed_by_user_id=None,
        idempotency_key="extra-balance",
    )

    assert result.billing_context == "pulse_prepaid"
    assert result.laser_pulses_used == 450
    assert result.pulse_resolution == "balance"
    assert result.pulse_overage_charge_minor == 0


def test_additional_service_overage_uses_its_own_device_price(monkeypatch) -> None:
    appointment = _appointment(pulses_used=0)
    appointment.laser_device_key = "prime_lase"
    line = _additional_line(device_key="candela_gentle")
    db = FakeDb()
    monkeypatch.setattr(
        pulse_service,
        "_consume_from_available_packs",
        lambda *args, **kwargs: 200,
    )
    captured = {}

    def fake_price(*args, **kwargs):
        captured.update(kwargs)
        return 300, "EGP"

    monkeypatch.setattr(pulse_service, "_device_overage_price", fake_price)
    monkeypatch.setattr(
        pulse_service,
        "record_activity_event",
        lambda *args, **kwargs: None,
    )

    result = pulse_service.apply_additional_service_pulse_billing(
        db,
        appointment=appointment,
        line=line,
        pulses_used=500,
        mode="overage",
        offer_id=None,
        changed_by_user_id=None,
        idempotency_key="extra-overage",
    )

    assert captured["device_key"] == "candela_gentle"
    assert result.billing_context == "pulse_prepaid"
    assert result.pulse_resolution == "overage"
    assert result.pulse_overage_unit_price_minor == 300
    assert result.pulse_overage_charge_minor == 90_000


def test_release_visit_pulses_reverses_additional_service_usage_when_primary_is_standard(
    monkeypatch,
) -> None:
    appointment = _appointment(pulses_used=0, billing_context="standard")
    line = _additional_line()
    line.billing_context = "pulse_prepaid"
    line.laser_pulses_used = 500
    line.pulse_resolution = "overage"
    line.pulse_overage_unit_price_minor = 300
    line.pulse_overage_charge_minor = 90_000
    usage = SimpleNamespace(status="consumed")

    class Rows:
        def __init__(self, values):
            self.values = values

        def all(self):
            return list(self.values)

    class ReleaseDb:
        def __init__(self):
            self.batches = [[line], [usage]]
            self.flush_count = 0

        def scalars(self, _statement):
            return Rows(self.batches.pop(0))

        def flush(self):
            self.flush_count += 1

    db = ReleaseDb()
    monkeypatch.setattr(
        pulse_service,
        "refresh_appointment_payment_snapshots",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        pulse_service,
        "record_activity_event",
        lambda *args, **kwargs: None,
    )

    pulse_service.release_appointment_pulse_usage(
        db,
        appointment=appointment,
        changed_by_user_id=None,
        reason="appointment_cancelled",
    )

    assert usage.status == "reversed"
    assert appointment.laser_pulses_used == 0
    assert line.billing_context == "pulse_prepaid"
    assert line.laser_pulses_used == 0
    assert line.pulse_resolution == "balance"
    assert line.pulse_overage_unit_price_minor is None
    assert line.pulse_overage_charge_minor == 0


def test_additional_service_pulse_migration_contract() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    migration = (
        root / "alembic/versions/0084_additional_service_pulses.py"
    ).read_text(encoding="utf-8")
    model = (
        root / "app/models/appointment_additional_service.py"
    ).read_text(encoding="utf-8")

    assert 'revision: str = "0084_extra_service_pulses"' in migration
    assert 'down_revision: str | Sequence[str] | None = "0083_pulse_device_pricing"' in migration
    assert "appointment_additional_service_id" in migration
    assert "pulse_prepaid" in model


def test_appointment_ui_keeps_standard_payment_optional_and_shows_balances() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    payment_form = (
        root
        / "frontend/src/app/(dashboard)/appointments/[appointmentId]/appointment-payment-form.tsx"
    ).read_text(encoding="utf-8")
    page = (
        root
        / "frontend/src/app/(dashboard)/appointments/[appointmentId]/page.tsx"
    ).read_text(encoding="utf-8")
    additional_form = (
        root
        / "frontend/src/app/(dashboard)/appointments/[appointmentId]/additional-service-form.tsx"
    ).read_text(encoding="utf-8")

    assert 'pulseCheckout?.lockedToPulse ? "pulse_pending" : "standard"' in payment_form
    assert 'checked={billingChoice === "standard"}' in payment_form
    assert 'onChange={() => chooseBilling("standard")}' in payment_form
    assert "visiblePulseBalances" in page
    assert "PatientPackageBalances" in page
    assert "packages={patientPackages}" in page
    assert "loadFailed={packageLoadFailed}" in page
    package_list = (
        root
        / "frontend/src/app/(dashboard)/appointments/[appointmentId]/patient-package-balances.tsx"
    ).read_text(encoding="utf-8")
    assert "pkg.sessions_remaining" in package_list
    assert "pkg.sessions_reserved" in package_list
    assert "pkg.sessions_consumed" in package_list
    assert "pulseBalances={pulseBalances}" in page
    assert "pulsePackOffers={pulsePackOffers}" in page
    assert 'useState<"standard" | "package" | "pulse">("standard")' in additional_form
    assert 'checked={billingMethod === "standard"}' in additional_form
    assert 'onChange={() => setBillingMethod("standard")}' in additional_form
    assert 'checked={billingMethod === "package"}' in additional_form
    assert 'onChange={() => setBillingMethod("package")}' in additional_form
    assert 'checked={billingMethod === "pulse"}' in additional_form
    assert 'onChange={() => setBillingMethod("pulse")}' in additional_form
    assert 'name="patient_package_id"' in additional_form
    assert 'name="pulses_used"' in additional_form
    assert '"use_balance"' in additional_form


def test_manual_booking_ui_requests_immediate_staff_availability() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    actions = (
        root / "frontend/src/app/(dashboard)/appointments/actions.ts"
    ).read_text(encoding="utf-8")
    booking_route = (root / "backend/app/api/routes/booking.py").read_text(encoding="utf-8")

    assert 'allow_immediate: "true"' in actions
    assert 'minimum_notice_minutes_override=0 if allow_immediate else None' in booking_route
    assert 'minimum_notice_minutes_override=0 if payload.source == "staff" else None' in booking_route
