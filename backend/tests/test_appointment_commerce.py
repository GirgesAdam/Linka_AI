from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import appointment_commerce as commerce
from app.services import payments as payment_service


class _Db:
    def scalar(self, _stmt):
        return None


def _appointment(*, status: str = "confirmed"):
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        patient_id=uuid4(),
        service_id=uuid4(),
        patient_package_id=None,
        laser_device_key="candela_gentle",
        price_minor=100000,
        status=status,
    )


def test_package_purchase_from_completed_appointment_consumes_first_session(
    monkeypatch,
) -> None:
    appointment = _appointment(status="completed")
    offer = SimpleNamespace(
        id=uuid4(),
        service_id=appointment.service_id,
        device_key=appointment.laser_device_key,
        price_minor=480000,
    )
    package = SimpleNamespace(id=uuid4())
    calls: list[str] = []

    monkeypatch.setattr(
        commerce, "_locked_appointment", lambda *args, **kwargs: appointment
    )
    monkeypatch.setattr(
        commerce, "get_active_package_offer", lambda *args, **kwargs: offer
    )
    monkeypatch.setattr(
        commerce,
        "get_appointment_payment_summary",
        lambda *args, **kwargs: SimpleNamespace(net_paid_minor=0, price_minor=100000),
    )
    purchase_kwargs: dict[str, object] = {}

    def _purchase(*args, **kwargs):
        purchase_kwargs.update(kwargs)
        return package

    monkeypatch.setattr(commerce, "purchase_package_offer", _purchase)
    monkeypatch.setattr(
        commerce,
        "reserve_package_usage",
        lambda *args, **kwargs: calls.append("reserved"),
    )
    monkeypatch.setattr(
        commerce,
        "consume_package_usage",
        lambda *args, **kwargs: calls.append("consumed"),
    )
    monkeypatch.setattr(
        commerce, "refresh_appointment_payment_snapshots", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        commerce, "record_activity_event", lambda *args, **kwargs: None
    )

    result = commerce.purchase_package_for_appointment(
        _Db(),
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        offer_id=offer.id,
        created_by_user_id=uuid4(),
        idempotency_key="appointment-package-test",
    )

    assert result is package
    assert calls == ["reserved", "consumed"]
    assert purchase_kwargs["amount_paid_minor"] == 0
    assert purchase_kwargs["payment_method"] == "unknown"
    assert purchase_kwargs["origin_appointment_id"] == appointment.id


def test_package_conversion_rejects_existing_appointment_payment(
    monkeypatch,
) -> None:
    appointment = _appointment()
    offer = SimpleNamespace(
        id=uuid4(),
        service_id=appointment.service_id,
        device_key=appointment.laser_device_key,
        price_minor=480000,
    )
    monkeypatch.setattr(
        commerce, "_locked_appointment", lambda *args, **kwargs: appointment
    )
    monkeypatch.setattr(
        commerce, "get_active_package_offer", lambda *args, **kwargs: offer
    )
    monkeypatch.setattr(
        commerce,
        "get_appointment_payment_summary",
        lambda *args, **kwargs: SimpleNamespace(net_paid_minor=100000, price_minor=100000),
    )

    with pytest.raises(
        commerce.AppointmentCommerceError, match="payment already applied to this service"
    ):
        commerce.purchase_package_for_appointment(
            _Db(),
            workspace_id=appointment.workspace_id,
            appointment_id=appointment.id,
            offer_id=offer.id,
            created_by_user_id=uuid4(),
            idempotency_key=None,
        )


def test_additional_service_package_is_added_unpaid_and_consumed_when_visit_completed(
    monkeypatch,
) -> None:
    appointment = _appointment(status="completed")
    line = SimpleNamespace(
        id=uuid4(),
        service_id=uuid4(),
        laser_device_key="candela_gentle",
        patient_package_id=None,
        billing_context="standard",
        unit_price_minor=100000,
    )
    offer = SimpleNamespace(
        id=uuid4(),
        service_id=line.service_id,
        device_key=line.laser_device_key,
        price_minor=350000,
    )
    package = SimpleNamespace(id=uuid4())
    calls: list[str] = []
    purchase_kwargs: dict[str, object] = {}

    class _LineDb:
        def scalar(self, _stmt):
            return line

    monkeypatch.setattr(
        commerce, "_locked_appointment", lambda *args, **kwargs: appointment
    )
    monkeypatch.setattr(
        commerce,
        "get_appointment_payment_summary",
        lambda *args, **kwargs: SimpleNamespace(net_paid_minor=0, price_minor=100000),
    )
    monkeypatch.setattr(
        commerce, "get_active_package_offer", lambda *args, **kwargs: offer
    )

    def _purchase(*args, **kwargs):
        purchase_kwargs.update(kwargs)
        return package

    monkeypatch.setattr(commerce, "purchase_package_offer", _purchase)
    monkeypatch.setattr(
        commerce,
        "reserve_additional_service_package_usage",
        lambda *args, **kwargs: calls.append("reserved"),
    )
    monkeypatch.setattr(
        commerce,
        "consume_visit_package_usages",
        lambda *args, **kwargs: calls.append("consumed"),
    )
    monkeypatch.setattr(
        commerce, "refresh_appointment_payment_snapshots", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        commerce, "record_activity_event", lambda *args, **kwargs: None
    )

    result = commerce.purchase_package_for_additional_service(
        _LineDb(),
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        line_id=line.id,
        offer_id=offer.id,
        created_by_user_id=uuid4(),
        idempotency_key="extra-package-test",
    )

    assert result is package
    assert calls == ["reserved", "consumed"]
    assert purchase_kwargs["amount_paid_minor"] == 0
    assert purchase_kwargs["payment_method"] == "unknown"
    assert purchase_kwargs["origin_appointment_id"] == appointment.id




def test_primary_package_conversion_allows_payments_that_only_cover_other_visit_items(
    monkeypatch,
) -> None:
    appointment = _appointment()
    appointment.price_minor = 100000
    offer = SimpleNamespace(
        id=uuid4(),
        service_id=appointment.service_id,
        device_key=appointment.laser_device_key,
        price_minor=480000,
    )
    package = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(
        commerce, "_locked_appointment", lambda *args, **kwargs: appointment
    )
    monkeypatch.setattr(
        commerce, "get_active_package_offer", lambda *args, **kwargs: offer
    )
    monkeypatch.setattr(
        commerce,
        "get_appointment_payment_summary",
        lambda *args, **kwargs: SimpleNamespace(
            net_paid_minor=50000,
            price_minor=150000,
        ),
    )
    monkeypatch.setattr(
        commerce, "purchase_package_offer", lambda *args, **kwargs: package
    )
    monkeypatch.setattr(
        commerce, "reserve_package_usage", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        commerce, "refresh_appointment_payment_snapshots", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        commerce, "record_activity_event", lambda *args, **kwargs: None
    )

    result = commerce.purchase_package_for_appointment(
        _Db(),
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        offer_id=offer.id,
        created_by_user_id=uuid4(),
        idempotency_key="primary-package-other-charge-paid",
    )

    assert result is package


def test_additional_service_package_allows_original_appointment_to_be_paid(
    monkeypatch,
) -> None:
    appointment = _appointment()
    line = SimpleNamespace(
        id=uuid4(),
        service_id=uuid4(),
        laser_device_key="candela_gentle",
        patient_package_id=None,
        billing_context="standard",
        unit_price_minor=100000,
    )
    offer = SimpleNamespace(
        id=uuid4(),
        service_id=line.service_id,
        device_key=line.laser_device_key,
        price_minor=350000,
    )
    package = SimpleNamespace(id=uuid4())

    class _LineDb:
        def scalar(self, _stmt):
            return line

    monkeypatch.setattr(
        commerce, "_locked_appointment", lambda *args, **kwargs: appointment
    )
    monkeypatch.setattr(
        commerce,
        "get_appointment_payment_summary",
        lambda *args, **kwargs: SimpleNamespace(
            net_paid_minor=100000,
            price_minor=200000,
        ),
    )
    monkeypatch.setattr(
        commerce, "get_active_package_offer", lambda *args, **kwargs: offer
    )
    monkeypatch.setattr(
        commerce, "purchase_package_offer", lambda *args, **kwargs: package
    )
    monkeypatch.setattr(
        commerce,
        "reserve_additional_service_package_usage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        commerce, "refresh_appointment_payment_snapshots", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        commerce, "record_activity_event", lambda *args, **kwargs: None
    )

    result = commerce.purchase_package_for_additional_service(
        _LineDb(),
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        line_id=line.id,
        offer_id=offer.id,
        created_by_user_id=uuid4(),
        idempotency_key="extra-package-primary-paid",
    )

    assert result is package


def test_additional_service_package_rejects_when_that_line_was_already_paid(
    monkeypatch,
) -> None:
    appointment = _appointment()
    line = SimpleNamespace(
        id=uuid4(),
        service_id=uuid4(),
        laser_device_key="candela_gentle",
        patient_package_id=None,
        billing_context="standard",
        unit_price_minor=100000,
    )
    offer = SimpleNamespace(
        id=uuid4(),
        service_id=line.service_id,
        device_key=line.laser_device_key,
        price_minor=350000,
    )

    class _LineDb:
        def scalar(self, _stmt):
            return line

    monkeypatch.setattr(
        commerce, "_locked_appointment", lambda *args, **kwargs: appointment
    )
    monkeypatch.setattr(
        commerce,
        "get_appointment_payment_summary",
        lambda *args, **kwargs: SimpleNamespace(
            net_paid_minor=150000,
            price_minor=200000,
        ),
    )
    monkeypatch.setattr(
        commerce, "get_active_package_offer", lambda *args, **kwargs: offer
    )

    with pytest.raises(
        commerce.AppointmentCommerceError,
        match="payment already applied to this additional service",
    ):
        commerce.purchase_package_for_additional_service(
            _LineDb(),
            workspace_id=appointment.workspace_id,
            appointment_id=appointment.id,
            line_id=line.id,
            offer_id=offer.id,
            created_by_user_id=uuid4(),
            idempotency_key=None,
        )


class _ChargeDb:
    def __init__(self, values):
        self.values = iter(values)

    def scalar(self, _stmt):
        return next(self.values)


def test_package_bought_from_visit_replaces_session_price_and_becomes_due() -> None:
    appointment = SimpleNamespace(
        id=uuid4(),
        price_minor=100000,
        billing_context="package_prepaid",
    )
    breakdown = payment_service._appointment_charge_breakdown(
        _ChargeDb([0, 0, 480000, 0, 0, 0]),
        workspace_id=uuid4(),
        appointment=appointment,
    )

    assert breakdown == (100000, 0, 0, 480000, 0, 0, 480000)


def test_additional_service_package_replaces_extra_service_price_but_keeps_primary_due() -> None:
    appointment = SimpleNamespace(
        id=uuid4(),
        price_minor=100000,
        billing_context="standard",
    )
    breakdown = payment_service._appointment_charge_breakdown(
        _ChargeDb([0, 0, 350000, 0, 0, 0]),
        workspace_id=uuid4(),
        appointment=appointment,
    )

    assert breakdown == (100000, 0, 0, 350000, 0, 0, 450000)


def test_pulse_billed_additional_service_excludes_line_price_and_keeps_overage() -> None:
    appointment = SimpleNamespace(
        id=uuid4(),
        price_minor=100000,
        billing_context="standard",
    )
    breakdown = payment_service._appointment_charge_breakdown(
        _ChargeDb([0, 0, 0, 0, 0, 90000]),
        workspace_id=uuid4(),
        appointment=appointment,
    )

    assert breakdown == (100000, 0, 0, 0, 0, 90000, 190000)


def _editable_appointment_for_service_edit(*, duration_minutes: int = 20):
    from datetime import UTC, datetime, timedelta

    appointment = _appointment(status="confirmed")
    appointment.branch_id = uuid4()
    appointment.doctor_id = uuid4()
    appointment.start_at = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    appointment.duration_minutes = duration_minutes
    appointment.end_at = appointment.start_at + timedelta(minutes=duration_minutes)
    appointment.busy_start_at = appointment.start_at
    appointment.busy_end_at = appointment.end_at + timedelta(minutes=10)
    appointment.currency = "EGP"
    appointment.laser_device_key = None
    return appointment


def test_cross_category_additional_service_is_rejected_by_commerce_guard(monkeypatch) -> None:
    appointment = _editable_appointment_for_service_edit()
    candidate_id = uuid4()
    primary = SimpleNamespace(
        id=appointment.service_id,
        operational_category="laser",
        requires_laser_device=False,
    )
    candidate = SimpleNamespace(
        id=candidate_id,
        operational_category="dermatology",
        requires_laser_device=False,
        price_minor=50000,
        currency="EGP",
        name="Consultation",
    )
    monkeypatch.setattr(commerce, "_locked_appointment", lambda *args, **kwargs: appointment)
    monkeypatch.setattr(
        commerce,
        "_active_service",
        lambda _db, *, workspace_id, service_id: candidate if service_id == candidate_id else primary,
    )

    with pytest.raises(
        commerce.AppointmentCommerceError,
        match="Cross-category services require separate appointments",
    ):
        commerce.add_additional_service(
            _Db(),
            workspace_id=appointment.workspace_id,
            appointment_id=appointment.id,
            service_id=candidate_id,
            laser_device_key=None,
            created_by_user_id=None,
        )


def test_add_longer_same_category_service_expands_only_after_availability_revalidation(monkeypatch) -> None:
    appointment = _editable_appointment_for_service_edit(duration_minutes=20)
    candidate = SimpleNamespace(
        id=uuid4(),
        operational_category="dermatology",
        requires_laser_device=False,
        price_minor=50000,
        currency="EGP",
        name="Long Consultation",
    )
    doctor_service = SimpleNamespace(custom_price_minor=None)

    class _EditDb:
        def __init__(self):
            self.scalar_calls = 0
            self.added = []
        def scalar(self, _stmt):
            self.scalar_calls += 1
            return doctor_service if self.scalar_calls == 1 else None
        def add(self, value):
            self.added.append(value)
        def flush(self):
            pass

    db = _EditDb()
    expansion = []
    monkeypatch.setattr(commerce, "_locked_appointment", lambda *args, **kwargs: appointment)
    monkeypatch.setattr(commerce, "_active_service", lambda *args, **kwargs: candidate)
    monkeypatch.setattr(commerce, "_require_same_operational_category", lambda *args, **kwargs: "dermatology")
    monkeypatch.setattr(
        commerce,
        "_visit_duration_authority",
        lambda *args, **kwargs: (45, candidate, None),
    )
    monkeypatch.setattr(
        commerce,
        "_ensure_duration_expansion_available",
        lambda *args, **kwargs: expansion.append(kwargs["duration_service"].id),
    )
    monkeypatch.setattr(commerce, "refresh_appointment_payment_snapshots", lambda *args, **kwargs: None)
    monkeypatch.setattr(commerce, "record_activity_event", lambda *args, **kwargs: None)

    commerce.add_additional_service(
        db,
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        service_id=candidate.id,
        laser_device_key=None,
        created_by_user_id=None,
    )

    assert expansion == [candidate.id]
    assert appointment.duration_minutes == 45
    assert len(db.added) == 1


def test_add_longer_service_conflict_rejects_without_line_or_duration_mutation(monkeypatch) -> None:
    appointment = _editable_appointment_for_service_edit(duration_minutes=20)
    candidate = SimpleNamespace(
        id=uuid4(),
        operational_category="dermatology",
        requires_laser_device=False,
        price_minor=50000,
        currency="EGP",
        name="Long Consultation",
    )
    doctor_service = SimpleNamespace(custom_price_minor=None)

    class _EditDb:
        def __init__(self):
            self.scalar_calls = 0
            self.added = []
        def scalar(self, _stmt):
            self.scalar_calls += 1
            return doctor_service if self.scalar_calls == 1 else None
        def add(self, value):
            self.added.append(value)
        def flush(self):
            pass

    db = _EditDb()
    monkeypatch.setattr(commerce, "_locked_appointment", lambda *args, **kwargs: appointment)
    monkeypatch.setattr(commerce, "_active_service", lambda *args, **kwargs: candidate)
    monkeypatch.setattr(commerce, "_require_same_operational_category", lambda *args, **kwargs: "dermatology")
    monkeypatch.setattr(
        commerce,
        "_visit_duration_authority",
        lambda *args, **kwargs: (45, candidate, None),
    )
    monkeypatch.setattr(
        commerce,
        "_ensure_duration_expansion_available",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            commerce.AppointmentCommerceError("unavailable time window")
        ),
    )

    with pytest.raises(commerce.AppointmentCommerceError, match="unavailable time window"):
        commerce.add_additional_service(
            db,
            workspace_id=appointment.workspace_id,
            appointment_id=appointment.id,
            service_id=candidate.id,
            laser_device_key=None,
            created_by_user_id=None,
        )
    assert appointment.duration_minutes == 20
    assert db.added == []


def test_remove_longest_additional_service_shrinks_visit_duration(monkeypatch) -> None:
    appointment = _editable_appointment_for_service_edit(duration_minutes=45)
    line = SimpleNamespace(
        id=uuid4(),
        service_id=uuid4(),
        patient_package_id=None,
        billing_context="standard",
    )

    class _RemoveDb:
        def __init__(self):
            self.deleted = []
        def scalar(self, _stmt):
            return line
        def delete(self, value):
            self.deleted.append(value)
        def flush(self):
            pass

    db = _RemoveDb()
    monkeypatch.setattr(commerce, "_locked_appointment", lambda *args, **kwargs: appointment)
    monkeypatch.setattr(
        commerce,
        "_visit_duration_authority",
        lambda *args, **kwargs: (20, SimpleNamespace(id=appointment.service_id), None),
    )
    monkeypatch.setattr(commerce, "refresh_appointment_payment_snapshots", lambda *args, **kwargs: None)
    monkeypatch.setattr(commerce, "record_activity_event", lambda *args, **kwargs: None)

    commerce.remove_additional_service(
        db,
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        line_id=line.id,
        changed_by_user_id=None,
    )

    assert db.deleted == [line]
    assert appointment.duration_minutes == 20


def test_appointment_end_and_busy_end_follow_recalculated_duration() -> None:
    from datetime import UTC, datetime, timedelta
    from app.models.appointment import Appointment

    start = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    appointment = Appointment(
        workspace_id=uuid4(),
        patient_id=uuid4(),
        branch_id=uuid4(),
        doctor_id=uuid4(),
        service_id=uuid4(),
        start_at=start,
        end_at=start + timedelta(minutes=45),
        busy_start_at=start,
        busy_end_at=start + timedelta(minutes=55),
        duration_minutes=45,
        price_minor=0,
        currency="EGP",
        status="confirmed",
        source="staff",
    )
    commerce._apply_visit_duration(appointment, 20)
    assert appointment.duration_minutes == 20
    assert appointment.end_at.isoformat() == "2026-10-20T15:20:00+00:00"
    assert appointment.busy_end_at.isoformat() == "2026-10-20T15:30:00+00:00"
