from datetime import UTC, date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import app.services.booking as booking


def test_availability_engine_applies_branch_blocks_to_full_service_interval() -> None:
    source = Path("app/services/booking.py").read_text(encoding="utf-8")
    assert "AvailabilityBlock.start_at < conflict_end_utc" in source
    assert "block.start_at < busy_end_utc and block.end_at > busy_start_utc" in source
    assert "and not branch_blocked" in source


def test_availability_block_api_is_workspace_scoped_and_reopenable() -> None:
    source = Path("app/api/routes/booking.py").read_text(encoding="utf-8")
    assert 'AvailabilityBlock.workspace_id == access.workspace.id' in source
    assert '@router.delete("/availability-blocks/{block_id}"' in source
    assert "This period overlaps an existing availability block." in source
    assert "existing_count" in source
    assert ".with_for_update()" in source
    assert "The requested period is closed for new bookings." in source


def test_appointments_ui_can_create_display_and_reopen_blocks() -> None:
    root = Path(__file__).resolve().parents[2]
    page = (root / "frontend/src/app/(dashboard)/appointments/page.tsx").read_text(encoding="utf-8")
    controls = (root / "frontend/src/app/(dashboard)/appointments/availability-block-controls.tsx").read_text(encoding="utf-8")
    actions = (root / "frontend/src/app/(dashboard)/appointments/actions.ts").read_text(encoding="utf-8")
    assert "قفل فترة" in controls
    assert "غير متاحة" in page
    assert "reopenAvailabilityBlock" in page
    assert 'method: "DELETE"' in actions


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeAvailabilityDb:
    def __init__(self, *, assignments, scalar_batches):
        self.assignments = assignments
        self.scalar_batches = list(scalar_batches)

    def execute(self, _stmt):
        return _Rows(self.assignments)

    def scalars(self, _stmt):
        assert self.scalar_batches, "Unexpected scalar query in availability-block test."
        return self.scalar_batches.pop(0)


def _availability_fixture(*, block_start: datetime, block_end: datetime):
    workspace_id = uuid4()
    branch_id = uuid4()
    service_id = uuid4()
    doctor_id = uuid4()
    workspace = SimpleNamespace(id=workspace_id, timezone="UTC")
    branch = SimpleNamespace(id=branch_id, workspace_id=workspace_id, is_active=True, timezone="UTC")
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
    assignment = (
        SimpleNamespace(doctor_id=doctor_id, branch_id=branch_id),
        SimpleNamespace(custom_price_minor=None),
        doctor,
    )
    branch_hours = [SimpleNamespace(start_time=time(10, 0), end_time=time(22, 0))]
    doctor_hours = [SimpleNamespace(
        doctor_id=doctor_id,
        branch_id=branch_id,
        start_time=time(10, 0),
        end_time=time(22, 0),
    )]
    block = SimpleNamespace(start_at=block_start, end_at=block_end)
    db = _FakeAvailabilityDb(
        assignments=[assignment],
        scalar_batches=[branch_hours, [], [block], doctor_hours, []],
    )
    return workspace, branch, service, doctor_id, db


def test_block_removes_every_slot_that_overlaps_full_service_interval(monkeypatch) -> None:
    workspace, branch, service, doctor_id, db = _availability_fixture(
        block_start=datetime(2026, 10, 6, 10, 0, tzinfo=UTC),
        block_end=datetime(2026, 10, 6, 14, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        booking,
        "get_effective_booking_settings",
        lambda *_args: booking.EffectiveBookingSettings(
            slot_interval_minutes=15,
            minimum_notice_minutes=0,
            booking_horizon_days=90,
            allow_same_day_booking=True,
        ),
    )

    _, slots = booking.calculate_availability(
        db=db,
        workspace=workspace,
        branch_id=branch.id,
        service_id=service.id,
        booking_date=date(2026, 10, 6),
        doctor_id=doctor_id,
        now=datetime(2026, 10, 5, 8, 0, tzinfo=UTC),
        preloaded_branch=branch,
        preloaded_service=service,
    )
    starts = {slot.start_at for slot in slots}

    assert datetime(2026, 10, 6, 13, 45, tzinfo=UTC) not in starts
    assert datetime(2026, 10, 6, 14, 0, tzinfo=UTC) in starts
    assert all(
        not (
            datetime(2026, 10, 6, 10, 0, tzinfo=UTC) < slot.end_at
            and datetime(2026, 10, 6, 14, 0, tzinfo=UTC) > slot.start_at
        )
        for slot in slots
    )


def test_exact_boundary_before_block_is_allowed(monkeypatch) -> None:
    workspace, branch, service, doctor_id, db = _availability_fixture(
        block_start=datetime(2026, 10, 6, 14, 0, tzinfo=UTC),
        block_end=datetime(2026, 10, 6, 15, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        booking,
        "get_effective_booking_settings",
        lambda *_args: booking.EffectiveBookingSettings(
            slot_interval_minutes=15,
            minimum_notice_minutes=0,
            booking_horizon_days=90,
            allow_same_day_booking=True,
        ),
    )

    _, slots = booking.calculate_availability(
        db=db,
        workspace=workspace,
        branch_id=branch.id,
        service_id=service.id,
        booking_date=date(2026, 10, 6),
        doctor_id=doctor_id,
        now=datetime(2026, 10, 5, 8, 0, tzinfo=UTC),
        preloaded_branch=branch,
        preloaded_service=service,
    )
    starts = {slot.start_at for slot in slots}
    assert datetime(2026, 10, 6, 13, 30, tzinfo=UTC) in starts
    assert datetime(2026, 10, 6, 13, 45, tzinfo=UTC) not in starts
    assert datetime(2026, 10, 6, 15, 0, tzinfo=UTC) in starts


def test_standard_create_and_reschedule_keep_exact_slot_reverification() -> None:
    root = Path(__file__).resolve().parents[1]
    creation = (root / "app/services/appointment_creation.py").read_text(encoding="utf-8")
    operations = (root / "app/services/appointment_operations.py").read_text(encoding="utf-8")
    routes = (root / "app/api/routes/booking.py").read_text(encoding="utf-8")
    assert "slot = find_exact_slot(" in creation
    assert "find_exact_slot(" in operations
    assert "slot = find_exact_slot(" in routes
    assert "The requested period is closed for new bookings." in routes


def test_timezone_local_day_and_branch_scope_are_server_side_contracts() -> None:
    source = Path("app/api/routes/booking.py").read_text(encoding="utf-8")
    assert "tz = workspace_timezone(access, branch)" in source
    assert "datetime.combine(date_value, time.min, tzinfo=tz).astimezone(UTC)" in source
    assert "AvailabilityBlock.branch_id == branch.id" in source
    assert "Branch.workspace_id == access.workspace.id" in source
