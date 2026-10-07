from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.appointment_confirmation import (
    can_customer_confirm_appointment,
    clamp_confirmation_delivery_time,
    initial_confirmation_decision,
)


def _db(*, branch_timezone: str | None, workspace_timezone: str):
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(timezone=branch_timezone)
    db.get.return_value = SimpleNamespace(timezone=workspace_timezone)
    return db


def test_far_booking_is_pending() -> None:
    db = _db(branch_timezone="Africa/Cairo", workspace_timezone="UTC")
    decision = initial_confirmation_decision(
        db,
        workspace_id=uuid4(),
        branch_id=uuid4(),
        start_at=datetime(2026, 10, 10, 16, 0, tzinfo=UTC),  # 18:00 Cairo
        now=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    )
    assert decision.status == "pending"
    assert decision.confirmed_at is None


def test_previous_local_calendar_day_booking_is_confirmed() -> None:
    db = _db(branch_timezone="Africa/Cairo", workspace_timezone="UTC")
    now = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
    decision = initial_confirmation_decision(
        db,
        workspace_id=uuid4(),
        branch_id=uuid4(),
        start_at=datetime(2026, 10, 10, 16, 0, tzinfo=UTC),
        now=now,
    )
    assert decision.status == "confirmed"
    assert decision.confirmed_at == now


def test_same_local_calendar_day_booking_is_confirmed() -> None:
    db = _db(branch_timezone="Africa/Cairo", workspace_timezone="UTC")
    decision = initial_confirmation_decision(
        db,
        workspace_id=uuid4(),
        branch_id=uuid4(),
        start_at=datetime(2026, 10, 10, 16, 0, tzinfo=UTC),
        now=datetime(2026, 10, 10, 6, 0, tzinfo=UTC),
    )
    assert decision.status == "confirmed"


def test_timezone_boundary_uses_branch_local_date_not_utc_date() -> None:
    db = _db(branch_timezone="Pacific/Kiritimati", workspace_timezone="UTC")
    # UTC is still Oct 8, but branch local time is already Oct 9: the previous
    # calendar day for an Oct 10 local appointment.
    now = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)
    start = datetime(2026, 10, 10, 4, 0, tzinfo=UTC)  # Oct 10 18:00 +14
    decision = initial_confirmation_decision(
        db,
        workspace_id=uuid4(),
        branch_id=uuid4(),
        start_at=start,
        now=now,
    )
    assert decision.status == "confirmed"


def test_workspace_timezone_is_fallback_when_branch_timezone_missing() -> None:
    db = _db(branch_timezone=None, workspace_timezone="Asia/Dubai")
    now = datetime(2026, 10, 8, 20, 30, tzinfo=UTC)  # Oct 9 00:30 Dubai
    start = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)  # Oct 10 18:00 Dubai
    decision = initial_confirmation_decision(
        db,
        workspace_id=uuid4(),
        branch_id=uuid4(),
        start_at=start,
        now=now,
    )
    assert decision.status == "confirmed"
    assert decision.timezone == "Asia/Dubai"


def test_customer_confirmation_rejected_too_early_and_after_start() -> None:
    db = _db(branch_timezone="Africa/Cairo", workspace_timezone="UTC")
    start = datetime(2026, 10, 10, 16, 0, tzinfo=UTC)
    assert not can_customer_confirm_appointment(
        db,
        workspace_id=uuid4(),
        branch_id=uuid4(),
        start_at=start,
        now=datetime(2026, 10, 8, 10, 0, tzinfo=UTC),
    )
    assert not can_customer_confirm_appointment(
        db,
        workspace_id=uuid4(),
        branch_id=uuid4(),
        start_at=start,
        now=start,
    )


def test_confirmation_delivery_is_clamped_to_previous_local_midnight() -> None:
    db = _db(branch_timezone="Africa/Cairo", workspace_timezone="UTC")
    start = datetime(2026, 10, 10, 16, 0, tzinfo=UTC)
    clamped = clamp_confirmation_delivery_time(
        db,
        workspace_id=uuid4(),
        branch_id=uuid4(),
        start_at=start,
        scheduled_for=datetime(2026, 10, 8, 16, 0, tzinfo=UTC),
    )
    assert clamped == datetime(2026, 10, 8, 21, 0, tzinfo=UTC)  # Oct 9 00:00 Cairo


def test_invalid_branch_timezone_falls_back_to_workspace_timezone() -> None:
    db = _db(branch_timezone="Invalid/Zone", workspace_timezone="Africa/Cairo")
    now = datetime(2026, 10, 9, 20, 30, tzinfo=UTC)
    start = datetime(2026, 10, 10, 22, 30, tzinfo=UTC)  # Oct 11 00:30 Cairo
    decision = initial_confirmation_decision(
        db,
        workspace_id=uuid4(),
        branch_id=uuid4(),
        start_at=start,
        now=now,
    )
    assert decision.timezone == "Africa/Cairo"
    assert decision.status == "pending"


def test_invalid_branch_and_workspace_timezones_fall_back_to_utc() -> None:
    db = _db(branch_timezone="Invalid/Branch", workspace_timezone="Invalid/Workspace")
    decision = initial_confirmation_decision(
        db,
        workspace_id=uuid4(),
        branch_id=uuid4(),
        start_at=datetime(2026, 10, 10, 22, 30, tzinfo=UTC),
        now=datetime(2026, 10, 9, 20, 30, tzinfo=UTC),
    )
    assert decision.timezone == "UTC"
