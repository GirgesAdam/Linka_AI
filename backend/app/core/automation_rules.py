from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class DefaultAutomationRule:
    key: str
    name: str
    trigger_kind: str
    offset_minutes: int
    channel: str
    template_name: str
    template_language: str
    max_lateness_minutes: int
    enabled_by_default: bool = False


DEFAULT_AUTOMATION_RULES: tuple[DefaultAutomationRule, ...] = (
    DefaultAutomationRule(
        # Keep the historical key for database compatibility. Timing is admin-configurable.
        key="appointment_reminder_6h",
        name="Appointment confirmation",
        trigger_kind="before_appointment",
        offset_minutes=-360,
        channel="whatsapp",
        template_name="tia_appointment_confirmation_01",
        template_language="ar_EG",
        max_lateness_minutes=30,
        enabled_by_default=True,
    ),
    DefaultAutomationRule(
        key="post_visit_followup",
        name="Post-visit follow-up",
        trigger_kind="after_completed",
        offset_minutes=1440,
        channel="whatsapp",
        template_name="tia_post_visit_01",
        template_language="ar_EG",
        max_lateness_minutes=1440,
        enabled_by_default=False,
    ),
    DefaultAutomationRule(
        key="cancellation_recovery",
        name="Cancellation recovery follow-up",
        trigger_kind="after_cancelled",
        offset_minutes=60,
        channel="whatsapp",
        template_name="tia_cancellation_recovery_ar",
        template_language="ar_EG",
        max_lateness_minutes=1440,
        enabled_by_default=False,
    ),
    DefaultAutomationRule(
        key="lead_not_booked_followup",
        name="Lead not-booked follow-up",
        trigger_kind="after_lead_activity",
        offset_minutes=1440,
        channel="whatsapp",
        template_name="tia_ai_followup_ar",
        template_language="ar_EG",
        max_lateness_minutes=1440,
        enabled_by_default=False,
    ),
)


def scheduled_for(
    *,
    trigger_kind: str,
    offset_minutes: int,
    appointment_created_at: datetime,
    appointment_start_at: datetime,
    completed_at: datetime | None,
    no_show_at: datetime | None,
    cancelled_at: datetime | None = None,
) -> datetime | None:
    if trigger_kind == "appointment_created":
        anchor = appointment_created_at
    elif trigger_kind == "before_appointment":
        anchor = appointment_start_at
    elif trigger_kind == "after_completed":
        anchor = completed_at
    elif trigger_kind == "after_no_show":
        anchor = no_show_at
    elif trigger_kind == "after_cancelled":
        anchor = cancelled_at
    else:
        return None

    if anchor is None:
        return None
    return anchor + timedelta(minutes=offset_minutes)