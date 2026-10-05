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
        assert self.scalar_batches, "Unexpected scalar query in availability test."
        return self.scalar_batches.pop(0)


def test_clinic_and_doctor_end_time_is_latest_allowed_booking_start(monkeypatch) -> None:
    workspace_id = uuid4()
    branch_id = uuid4()
    service_id = uuid4()
    doctor_id = uuid4()
    booking_day = date(2026, 9, 11)

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
        requires_laser_device=False,
        duration_minutes=60,
        buffer_before_minutes=15,
        buffer_after_minutes=15,
        price_minor=100_000,
        currency="EGP",
    )
    doctor = SimpleNamespace(id=doctor_id, doctor_type="regular")
    doctor_service = SimpleNamespace(custom_price_minor=None)
    doctor_branch = SimpleNamespace(doctor_id=doctor_id, branch_id=branch_id)
    branch_hours = [SimpleNamespace(start_time=time(9, 0), end_time=time(22, 0))]
    doctor_hours = [
        SimpleNamespace(
            doctor_id=doctor_id,
            branch_id=branch_id,
            start_time=time(10, 0),
            end_time=time(22, 0),
        )
    ]
    db = _FakeDb(
        assignments=[(doctor_branch, doctor_service, doctor)],
        scalar_batches=[branch_hours, [], [], doctor_hours, []],
    )

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

    _, slots = booking.calculate_availability(
        db=db,
        workspace=workspace,
        branch_id=branch.id,
        service_id=service.id,
        booking_date=booking_day,
        doctor_id=doctor_id,
        now=datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
        preloaded_branch=branch,
        preloaded_service=service,
    )

    closing_slot = next(
        slot
        for slot in slots
        if slot.start_at == datetime(2026, 9, 11, 22, 0, tzinfo=UTC)
    )
    assert closing_slot.end_at == datetime(2026, 9, 11, 23, 0, tzinfo=UTC)
    assert closing_slot.busy_start_at == datetime(2026, 9, 11, 22, 0, tzinfo=UTC)
    assert closing_slot.busy_end_at == datetime(2026, 9, 11, 23, 0, tzinfo=UTC)


def test_booking_start_after_shared_end_time_is_not_offered(monkeypatch) -> None:
    workspace_id = uuid4()
    branch_id = uuid4()
    service_id = uuid4()
    doctor_id = uuid4()
    booking_day = date(2026, 9, 11)

    workspace = SimpleNamespace(id=workspace_id, timezone="UTC")
    branch = SimpleNamespace(id=branch_id, workspace_id=workspace_id, is_active=True, timezone="UTC")
    service = SimpleNamespace(
        id=service_id,
        workspace_id=workspace_id,
        is_active=True,
        requires_laser_device=False,
        duration_minutes=30,
        buffer_before_minutes=0,
        buffer_after_minutes=0,
        price_minor=100_000,
        currency="EGP",
    )
    doctor = SimpleNamespace(id=doctor_id, doctor_type="regular")
    doctor_service = SimpleNamespace(custom_price_minor=None)
    doctor_branch = SimpleNamespace(doctor_id=doctor_id, branch_id=branch_id)
    db = _FakeDb(
        assignments=[(doctor_branch, doctor_service, doctor)],
        scalar_batches=[
            [SimpleNamespace(start_time=time(9, 0), end_time=time(22, 0))],
            [],
            [],
            [
                SimpleNamespace(
                    doctor_id=doctor_id,
                    branch_id=branch_id,
                    start_time=time(9, 0),
                    end_time=time(21, 30),
                )
            ],
            [],
        ],
    )
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

    _, slots = booking.calculate_availability(
        db=db,
        workspace=workspace,
        branch_id=branch.id,
        service_id=service.id,
        booking_date=booking_day,
        doctor_id=doctor_id,
        now=datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
        preloaded_branch=branch,
        preloaded_service=service,
    )

    starts = {slot.start_at for slot in slots}
    assert datetime(2026, 9, 11, 21, 30, tzinfo=UTC) in starts
    assert datetime(2026, 9, 11, 21, 45, tzinfo=UTC) not in starts


def test_staff_immediate_override_skips_one_hour_notice(monkeypatch) -> None:
    workspace_id = uuid4()
    branch_id = uuid4()
    service_id = uuid4()
    doctor_id = uuid4()
    booking_day = date(2026, 9, 23)

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
        requires_laser_device=False,
        duration_minutes=30,
        price_minor=100_000,
        currency="EGP",
    )
    doctor = SimpleNamespace(id=doctor_id, doctor_type="regular")
    doctor_service = SimpleNamespace(custom_price_minor=None)
    doctor_branch = SimpleNamespace(doctor_id=doctor_id, branch_id=branch_id)

    def make_db():
        return _FakeDb(
            assignments=[(doctor_branch, doctor_service, doctor)],
            scalar_batches=[
                [SimpleNamespace(start_time=time(9, 0), end_time=time(18, 0))],
                [],
                [],
                [
                    SimpleNamespace(
                        doctor_id=doctor_id,
                        branch_id=branch_id,
                        start_time=time(9, 0),
                        end_time=time(18, 0),
                    )
                ],
                [],
            ],
        )

    monkeypatch.setattr(
        booking,
        "get_effective_booking_settings",
        lambda _db, _workspace_id: booking.EffectiveBookingSettings(
            slot_interval_minutes=15,
            minimum_notice_minutes=60,
            booking_horizon_days=90,
            cancellation_notice_minutes=720,
            allow_same_day_booking=True,
            require_confirmation=True,
            default_currency="EGP",
        ),
    )
    now = datetime(2026, 9, 23, 10, 7, tzinfo=UTC)

    _, regular_slots = booking.calculate_availability(
        db=make_db(),
        workspace=workspace,
        branch_id=branch.id,
        service_id=service.id,
        booking_date=booking_day,
        doctor_id=doctor_id,
        now=now,
        preloaded_branch=branch,
        preloaded_service=service,
    )
    _, staff_slots = booking.calculate_availability(
        db=make_db(),
        workspace=workspace,
        branch_id=branch.id,
        service_id=service.id,
        booking_date=booking_day,
        doctor_id=doctor_id,
        now=now,
        preloaded_branch=branch,
        preloaded_service=service,
        minimum_notice_minutes_override=0,
    )

    regular_starts = {slot.start_at for slot in regular_slots}
    staff_starts = {slot.start_at for slot in staff_slots}
    assert datetime(2026, 9, 23, 10, 15, tzinfo=UTC) not in regular_starts
    assert datetime(2026, 9, 23, 10, 15, tzinfo=UTC) in staff_starts
