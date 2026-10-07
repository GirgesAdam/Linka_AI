from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.workspace import Workspace


@dataclass(frozen=True)
class AppointmentConfirmationDecision:
    status: str
    confirmed_at: datetime | None
    timezone: str
    window_opens_on: date
    appointment_date: date


def resolve_confirmation_timezone(
    *,
    branch_timezone: str | None,
    workspace_timezone: str | None,
) -> ZoneInfo:
    for timezone_name in (branch_timezone, workspace_timezone, "UTC"):
        if not timezone_name:
            continue
        try:
            return ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def confirmation_timezone(
    db: Session,
    *,
    workspace_id: UUID,
    branch_id: UUID,
) -> ZoneInfo:
    branch = db.scalar(
        select(Branch).where(
            Branch.workspace_id == workspace_id,
            Branch.id == branch_id,
        )
    )
    workspace = db.get(Workspace, workspace_id)
    return resolve_confirmation_timezone(
        branch_timezone=branch.timezone if branch is not None else None,
        workspace_timezone=workspace.timezone if workspace is not None else None,
    )


def confirmation_window_dates(*, start_at: datetime, timezone: ZoneInfo) -> tuple[date, date]:
    appointment_date = start_at.astimezone(timezone).date()
    return appointment_date - timedelta(days=1), appointment_date


def confirmation_window_is_open(
    *,
    start_at: datetime,
    now: datetime,
    timezone: ZoneInfo,
) -> bool:
    local_date = now.astimezone(timezone).date()
    opens_on, appointment_date = confirmation_window_dates(
        start_at=start_at,
        timezone=timezone,
    )
    return opens_on <= local_date <= appointment_date


def initial_confirmation_decision(
    db: Session,
    *,
    workspace_id: UUID,
    branch_id: UUID,
    start_at: datetime,
    now: datetime | None = None,
) -> AppointmentConfirmationDecision:
    occurred_at = (now or datetime.now(UTC)).astimezone(UTC)
    timezone = confirmation_timezone(
        db,
        workspace_id=workspace_id,
        branch_id=branch_id,
    )
    opens_on, appointment_date = confirmation_window_dates(
        start_at=start_at,
        timezone=timezone,
    )
    confirmed = confirmation_window_is_open(
        start_at=start_at,
        now=occurred_at,
        timezone=timezone,
    )
    return AppointmentConfirmationDecision(
        status="confirmed" if confirmed else "pending",
        confirmed_at=occurred_at if confirmed else None,
        timezone=timezone.key,
        window_opens_on=opens_on,
        appointment_date=appointment_date,
    )


def can_customer_confirm_appointment(
    db: Session,
    *,
    workspace_id: UUID,
    branch_id: UUID,
    start_at: datetime,
    now: datetime | None = None,
) -> bool:
    occurred_at = (now or datetime.now(UTC)).astimezone(UTC)
    if occurred_at >= start_at.astimezone(UTC):
        return False
    timezone = confirmation_timezone(
        db,
        workspace_id=workspace_id,
        branch_id=branch_id,
    )
    return confirmation_window_is_open(
        start_at=start_at,
        now=occurred_at,
        timezone=timezone,
    )


def clamp_confirmation_delivery_time_for_timezones(
    *,
    branch_timezone: str | None,
    workspace_timezone: str | None,
    start_at: datetime,
    scheduled_for: datetime,
) -> datetime:
    timezone = resolve_confirmation_timezone(
        branch_timezone=branch_timezone,
        workspace_timezone=workspace_timezone,
    )
    opens_on, _ = confirmation_window_dates(start_at=start_at, timezone=timezone)
    window_open_local = datetime.combine(opens_on, datetime.min.time(), tzinfo=timezone)
    window_open_utc = window_open_local.astimezone(UTC)
    scheduled_utc = scheduled_for.astimezone(UTC)
    return max(scheduled_utc, window_open_utc)


def clamp_confirmation_delivery_time(
    db: Session,
    *,
    workspace_id: UUID,
    branch_id: UUID,
    start_at: datetime,
    scheduled_for: datetime,
) -> datetime:
    branch = db.scalar(
        select(Branch).where(
            Branch.workspace_id == workspace_id,
            Branch.id == branch_id,
        )
    )
    workspace = db.get(Workspace, workspace_id)
    return clamp_confirmation_delivery_time_for_timezones(
        branch_timezone=branch.timezone if branch is not None else None,
        workspace_timezone=workspace.timezone if workspace is not None else None,
        start_at=start_at,
        scheduled_for=scheduled_for,
    )
