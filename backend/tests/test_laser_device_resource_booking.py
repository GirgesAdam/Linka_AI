from __future__ import annotations

from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from uuid import uuid4

import app.services.booking as booking


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, *, assignments, scalar_batches):
        self.assignments = assignments
        self.scalar_batches = list(scalar_batches)

    def execute(self, _stmt):
        return _Rows(self.assignments)

    def scalars(self, _stmt):
        assert self.scalar_batches, "Unexpected scalar query in availability regression test."
        return self.scalar_batches.pop(0)


def _fixture(*, doctor_existing=None, device_existing=None):
    workspace_id = uuid4()
    branch_id = uuid4()
    service_id = uuid4()
    doctor_id = uuid4()
    booking_day = date(2026, 9, 10)

    workspace = SimpleNamespace(id=workspace_id, timezone="UTC")
    branch = SimpleNamespace(
        id=branch_id,
        workspace_id=workspace_id,
        is_active=True,
        timezone="UTC",
    )
    service = SimpleNamespace(
        id=service_id,
        workspace_id=workspace_id,
        is_active=True,
        requires_laser_device=True,
        duration_minutes=60,
        buffer_before_minutes=0,
        buffer_after_minutes=0,
        price_minor=100_000,
        currency="EGP",
    )
    doctor = SimpleNamespace(id=doctor_id, doctor_type="resident")
    doctor_service = SimpleNamespace(custom_price_minor=None)
    doctor_branch = SimpleNamespace(doctor_id=doctor_id, branch_id=branch_id)
    branch_hours = [SimpleNamespace(start_time=time(15, 0), end_time=time(18, 0))]
    doctor_hours = [
        SimpleNamespace(
            doctor_id=doctor_id,
            branch_id=branch_id,
            start_time=time(15, 0),
            end_time=time(18, 0),
        )
    ]

    doctor_existing_rows = list(doctor_existing or [])
    for appointment in doctor_existing_rows:
        if not hasattr(appointment, "doctor_id"):
            appointment.doctor_id = doctor_id

    db = _FakeDb(
        assignments=[(doctor_branch, doctor_service, doctor)],
        scalar_batches=[
            branch_hours,
            doctor_existing_rows,
            [],
            list(device_existing or []),
            doctor_hours,
            [],
        ],
    )
    return db, workspace, branch, service, doctor_id, booking_day


def _busy_appointment(start_hour: int, end_hour: int):
    return SimpleNamespace(
        busy_start_at=datetime(2026, 9, 10, start_hour, 0, tzinfo=UTC),
        busy_end_at=datetime(2026, 9, 10, end_hour, 0, tzinfo=UTC),
    )


def _configure(
    monkeypatch,
    *,
    device_key: str,
    device_name: str,
    price_minor: int,
    duration_minutes: int = 60,
):
    monkeypatch.setattr(
        booking,
        "get_effective_booking_settings",
        lambda _db, _workspace_id: booking.EffectiveBookingSettings(
            slot_interval_minutes=15,
            minimum_notice_minutes=0,
            booking_horizon_days=90,
            cancellation_notice_minutes=720,
            allow_same_day_booking=True,
            require_confirmation=True,
            default_currency="EGP",
        ),
    )
    monkeypatch.setattr(
        booking,
        "configured_device_price",
        lambda _db, *, workspace_id, service_id, device_key: SimpleNamespace(
            device_key=device_key,
            device_name=device_name,
            price_minor=price_minor,
            duration_minutes=duration_minutes,
            currency="EGP",
        ),
    )


def test_busy_candela_does_not_block_prime_for_another_free_doctor(monkeypatch) -> None:
    candela_busy = _busy_appointment(16, 17)
    candela_db, workspace, branch, service, doctor_id, booking_day = _fixture(
        device_existing=[candela_busy]
    )
    _configure(
        monkeypatch,
        device_key="candela_gentle",
        device_name="Candela Gentle",
        price_minor=140_000,
    )

    _, candela_slots = booking.calculate_availability(
        db=candela_db,
        workspace=workspace,
        branch_id=branch.id,
        service_id=service.id,
        booking_date=booking_day,
        doctor_id=doctor_id,
        now=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        preloaded_branch=branch,
        preloaded_service=service,
        laser_device_key="candela_gentle",
    )
    assert all(slot.start_at.hour != 16 for slot in candela_slots)

    prime_db, workspace, branch, service, doctor_id, booking_day = _fixture()
    _configure(
        monkeypatch,
        device_key="prime_lase",
        device_name="Prime Lase",
        price_minor=120_000,
        duration_minutes=45,
    )
    _, prime_slots = booking.calculate_availability(
        db=prime_db,
        workspace=workspace,
        branch_id=branch.id,
        service_id=service.id,
        booking_date=booking_day,
        doctor_id=doctor_id,
        now=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        preloaded_branch=branch,
        preloaded_service=service,
        laser_device_key="prime_lase",
    )
    four_pm = [slot for slot in prime_slots if slot.start_at.hour == 16 and slot.start_at.minute == 0]
    assert len(four_pm) == 1
    assert four_pm[0].laser_device_key == "prime_lase"
    assert four_pm[0].laser_device_name == "Prime Lase"
    assert four_pm[0].price_minor == 120_000
    assert four_pm[0].duration_minutes == 45
    assert int((four_pm[0].end_at - four_pm[0].start_at).total_seconds() // 60) == 45


def test_free_second_device_cannot_double_book_the_same_doctor(monkeypatch) -> None:
    doctor_busy = _busy_appointment(16, 17)
    db, workspace, branch, service, doctor_id, booking_day = _fixture(
        doctor_existing=[doctor_busy],
        device_existing=[],
    )
    _configure(
        monkeypatch,
        device_key="prime_lase",
        device_name="Prime Lase",
        price_minor=120_000,
    )

    _, slots = booking.calculate_availability(
        db=db,
        workspace=workspace,
        branch_id=branch.id,
        service_id=service.id,
        booking_date=booking_day,
        doctor_id=doctor_id,
        now=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        preloaded_branch=branch,
        preloaded_service=service,
        laser_device_key="prime_lase",
    )
    assert all(slot.start_at.hour != 16 for slot in slots)


def test_database_migration_guards_concurrent_same_device_overlap() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    migration = (
        root / "backend/alembic/versions/0062_laser_device_scheduling.py"
    ).read_text(encoding="utf-8")

    assert "CREATE EXTENSION IF NOT EXISTS btree_gist" in migration
    assert "excl_appointments_laser_device_busy_time" in migration
    assert "laser_device_key WITH =" in migration
    assert "tstzrange(busy_start_at, busy_end_at, '[)')" in migration
    assert "('pending', 'confirmed', 'checked_in', 'in_progress')" in migration


def test_arbitrary_clinic_device_key_drives_availability(monkeypatch) -> None:
    db, workspace, branch, service, doctor_id, booking_day = _fixture()
    _configure(
        monkeypatch,
        device_key="device_deka_again",
        device_name="DEKA Again",
        price_minor=155_000,
        duration_minutes=50,
    )

    _, slots = booking.calculate_availability(
        db=db,
        workspace=workspace,
        branch_id=branch.id,
        service_id=service.id,
        booking_date=booking_day,
        doctor_id=doctor_id,
        now=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        preloaded_branch=branch,
        preloaded_service=service,
        laser_device_key="device_deka_again",
    )

    assert slots
    assert all(slot.laser_device_key == "device_deka_again" for slot in slots)
    assert all(slot.laser_device_name == "DEKA Again" for slot in slots)
    assert all(slot.price_minor == 155_000 for slot in slots)
    assert all(slot.duration_minutes == 50 for slot in slots)
