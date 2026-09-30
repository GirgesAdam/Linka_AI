from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import staff_appointment_edits as edits
from app.services.booking import SlotCandidate


def _appointment(*, laser_device_key: str | None = None):
    start = datetime(2026, 9, 10, 17, 45, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        status="confirmed",
        branch_id=uuid4(),
        doctor_id=uuid4(),
        service_id=uuid4(),
        start_at=start,
        end_at=start + timedelta(minutes=60),
        busy_start_at=start,
        busy_end_at=start + timedelta(minutes=65),
        duration_minutes=60,
        price_minor=405_000,
        currency="EGP",
        laser_device_key=laser_device_key,
        laser_device_name="Candela Gentle" if laser_device_key else None,
        patient_package_id=None,
        billing_context="standard",
        package_external_id=None,
    )


def test_same_doctor_same_time_correction_does_not_reapply_current_working_hours(monkeypatch) -> None:
    appointment = _appointment(laser_device_key="candela_gentle")
    workspace = SimpleNamespace(id=appointment.workspace_id)
    branch = SimpleNamespace(
        id=appointment.branch_id,
        workspace_id=workspace.id,
        is_active=True,
        timezone="Africa/Cairo",
    )
    sentinel = SlotCandidate(
        branch_id=appointment.branch_id,
        doctor_id=appointment.doctor_id,
        service_id=appointment.service_id,
        start_at=appointment.start_at,
        end_at=appointment.end_at,
        busy_start_at=appointment.busy_start_at,
        busy_end_at=appointment.busy_end_at,
        duration_minutes=appointment.duration_minutes,
        price_minor=350_000,
        currency="EGP",
        laser_device_key="prime_lase",
        laser_device_name="Prime Lase",
    )

    class BranchDb:
        def scalar(self, _stmt):
            return branch

    monkeypatch.setattr(edits, "resolve_timezone", lambda *_args, **_kwargs: UTC)
    monkeypatch.setattr(
        edits,
        "_same_start_slot_for_current_doctor",
        lambda *args, **kwargs: sentinel,
    )

    def fail_availability(*_args, **_kwargs):
        raise AssertionError("same-time staff correction must not re-run weekly availability")

    monkeypatch.setattr(edits, "calculate_availability", fail_availability)

    result = edits._validated_slot_for_existing_appointment(
        BranchDb(),
        workspace=workspace,
        appointment=appointment,
        service_id=appointment.service_id,
        doctor_id=appointment.doctor_id,
        laser_device_key="prime_lase",
    )

    assert result is sentinel


def test_same_time_device_change_reprices_without_changing_duration(monkeypatch) -> None:
    appointment = _appointment(laser_device_key="candela_gentle")
    workspace = SimpleNamespace(id=appointment.workspace_id)
    service = SimpleNamespace(
        id=appointment.service_id,
        workspace_id=workspace.id,
        is_active=True,
        duration_minutes=60,
        buffer_before_minutes=0,
        buffer_after_minutes=5,
        price_minor=405_000,
        currency="EGP",
        requires_laser_device=True,
    )

    class ServiceDb:
        def scalar(self, _stmt):
            return service

    monkeypatch.setattr(
        edits,
        "configured_device_price",
        lambda *args, **kwargs: SimpleNamespace(
            price_minor=350_000,
            currency="EGP",
            device_key="prime_lase",
            device_name="Prime Lase",
        ),
    )
    monkeypatch.setattr(edits, "_overlapping_appointment_id", lambda *args, **kwargs: None)

    slot = edits._same_start_slot_for_current_doctor(
        ServiceDb(),
        workspace=workspace,
        appointment=appointment,
        service_id=appointment.service_id,
        laser_device_key="prime_lase",
    )

    assert slot.start_at == appointment.start_at
    assert slot.end_at == appointment.end_at
    assert slot.duration_minutes == appointment.duration_minutes
    assert slot.laser_device_key == "prime_lase"
    assert slot.price_minor == 350_000


def test_manual_time_edit_is_validated_and_applied(monkeypatch) -> None:
    appointment = _appointment()
    workspace = SimpleNamespace(id=appointment.workspace_id)
    target_start = appointment.start_at + timedelta(days=1, hours=1)
    target_slot = SlotCandidate(
        branch_id=appointment.branch_id,
        doctor_id=appointment.doctor_id,
        service_id=appointment.service_id,
        start_at=target_start,
        end_at=target_start + timedelta(minutes=60),
        busy_start_at=target_start,
        busy_end_at=target_start + timedelta(minutes=65),
        duration_minutes=60,
        price_minor=appointment.price_minor,
        currency=appointment.currency,
    )
    validated: list[datetime | None] = []

    class Db:
        def flush(self):
            return None

    monkeypatch.setattr(edits, "_locked_appointment", lambda *args, **kwargs: appointment)
    monkeypatch.setattr(
        edits,
        "_validated_slot_for_existing_appointment",
        lambda *args, **kwargs: (
            validated.append(kwargs["requested_start_at"]) or target_slot
        ),
    )
    monkeypatch.setattr(edits, "refresh_appointment_payment_snapshots", lambda *args, **kwargs: None)
    monkeypatch.setattr(edits, "record_activity_event", lambda *args, **kwargs: None)

    result = edits.change_appointment_service(
        Db(),
        workspace=workspace,
        appointment_id=appointment.id,
        service_id=appointment.service_id,
        doctor_id=appointment.doctor_id,
        laser_device_key=None,
        start_at=target_start,
        changed_by_user_id=uuid4(),
    )

    assert result is appointment
    assert validated == [target_start]
    assert appointment.start_at == target_start
    assert appointment.end_at == target_slot.end_at


def test_manual_time_edit_rejects_overlap_in_same_operational_category(monkeypatch) -> None:
    appointment = _appointment()
    workspace = SimpleNamespace(id=appointment.workspace_id)
    branch = SimpleNamespace(
        id=appointment.branch_id,
        workspace_id=workspace.id,
        is_active=True,
        timezone="Africa/Cairo",
    )
    service = SimpleNamespace(
        id=appointment.service_id,
        workspace_id=workspace.id,
        is_active=True,
        operational_category="dermatology",
        requires_laser_device=False,
    )
    target_start = appointment.start_at + timedelta(days=1, hours=1)
    target_slot = SlotCandidate(
        branch_id=appointment.branch_id,
        doctor_id=appointment.doctor_id,
        service_id=appointment.service_id,
        start_at=target_start,
        end_at=target_start + timedelta(minutes=60),
        busy_start_at=target_start,
        busy_end_at=target_start + timedelta(minutes=65),
        duration_minutes=60,
        price_minor=appointment.price_minor,
        currency=appointment.currency,
    )

    class Db:
        def __init__(self):
            self.values = iter((branch, service, uuid4()))

        def scalar(self, _stmt):
            return next(self.values)

    monkeypatch.setattr(edits, "resolve_timezone", lambda *_args, **_kwargs: UTC)
    monkeypatch.setattr(
        edits,
        "calculate_availability",
        lambda *args, **kwargs: ("UTC", [target_slot]),
    )

    with pytest.raises(edits.StaffAppointmentEditError, match="same service category"):
        edits._validated_slot_for_existing_appointment(
            Db(),
            workspace=workspace,
            appointment=appointment,
            service_id=appointment.service_id,
            doctor_id=appointment.doctor_id,
            laser_device_key=None,
            requested_start_at=target_start,
        )
