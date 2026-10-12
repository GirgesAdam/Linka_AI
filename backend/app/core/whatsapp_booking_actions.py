from __future__ import annotations

from uuid import UUID

ATTENDANCE_CONFIRM_PREFIX = "tia.appointment.attendance_confirm:"
LEGACY_BOOKING_CONFIRM_PREFIX = "tia.booking.confirm:"
RESCHEDULE_PREFIX = "tia.booking.reschedule:"


def attendance_confirm_action_id(appointment_id: UUID | str) -> str:
    return f"{ATTENDANCE_CONFIRM_PREFIX}{appointment_id}"


def reschedule_action_id(appointment_id: UUID | str) -> str:
    return f"{RESCHEDULE_PREFIX}{appointment_id}"
