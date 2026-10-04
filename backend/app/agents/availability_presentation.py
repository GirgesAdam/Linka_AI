from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from app.agents.v2.customer_datetime import format_customer_date
from app.services.laser_slot_metadata import decode_laser_slot_branch


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _display_date(value: object) -> str:
    if not value:
        return ""
    return format_customer_date(value, arabic=True)


def _clock_ar(value: datetime) -> str:
    hour = value.hour
    minute = value.minute
    suffix = "ص" if hour < 12 else "م"
    display_hour = hour % 12 or 12
    if minute == 0:
        return f"{display_hour} {suffix}"
    return f"{display_hour}:{minute:02d} {suffix}"


def _device_from_slot(slot: dict[str, Any]) -> tuple[str, str]:
    device_key = str(slot.get("laser_device_key") or "").strip()
    device_name = str(slot.get("laser_device_name") or "").strip()
    if device_key or device_name:
        return device_key, device_name
    metadata = decode_laser_slot_branch(slot.get("branch_name"))
    return metadata.device_key or "", metadata.device_name or ""


def _bookable_start_runs(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Compress verified appointment starts into conservative start-time ranges.

    A range end is always the latest verified *bookable start*, never appointment
    end time. Repeated start-time deltas establish a trustworthy cadence. A pair
    can also be joined when the earlier verified slot ends exactly at the later
    start. Otherwise an irregular gap stays split instead of implying an
    unverified start-time range.
    """

    by_start: dict[datetime, datetime] = {}
    for start, end in intervals:
        previous_end = by_start.get(start)
        if previous_end is None or end > previous_end:
            by_start[start] = end

    ordered = sorted(by_start.items())
    if not ordered:
        return []
    if len(ordered) == 1:
        start = ordered[0][0]
        return [(start, start)]

    deltas = [
        int((current_start - previous_start).total_seconds())
        for (previous_start, _), (current_start, _) in zip(ordered, ordered[1:], strict=False)
    ]
    cadence_counts = Counter(delta for delta in deltas if delta > 0)
    reliable_cadences = {delta for delta, count in cadence_counts.items() if count >= 2}

    runs: list[tuple[datetime, datetime]] = []
    run_start = ordered[0][0]
    run_end = ordered[0][0]

    for (previous_start, previous_end), (current_start, _current_end) in zip(
        ordered, ordered[1:], strict=False
    ):
        delta = int((current_start - previous_start).total_seconds())
        same_verified_run = delta in reliable_cadences or previous_end == current_start
        if same_verified_run:
            run_end = current_start
            continue
        runs.append((run_start, run_end))
        run_start = current_start
        run_end = current_start

    runs.append((run_start, run_end))
    return runs


def availability_windows_from_slots(slots: object) -> list[dict[str, Any]]:
    if not isinstance(slots, list):
        return []

    grouped: dict[tuple[str, str, str, str], list[tuple[datetime, datetime]]] = defaultdict(list)
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        start = _parse_dt(slot.get("start_local"))
        end = _parse_dt(slot.get("end_local"))
        if start is None or end is None or end <= start:
            continue
        doctor_id = str(slot.get("doctor_id") or "")
        doctor_name = str(slot.get("doctor_name") or "الدكتور المتاح").strip() or "الدكتور المتاح"
        device_key, device_name = _device_from_slot(slot)
        grouped[(doctor_id, doctor_name, device_key, device_name)].append((start, end))

    windows: list[dict[str, Any]] = []
    for (doctor_id, doctor_name, device_key, device_name), intervals in grouped.items():
        for start, end in _bookable_start_runs(intervals):
            windows.append(
                {
                    "doctor_id": doctor_id or None,
                    "doctor_name": doctor_name,
                    "laser_device_key": device_key or None,
                    "laser_device_name": device_name or None,
                    "start_local": start.isoformat(),
                    "end_local": end.isoformat(),
                    "start_time_24h": start.strftime("%H:%M"),
                    "end_time_24h": end.strftime("%H:%M"),
                }
            )

    windows.sort(
        key=lambda row: (
            str(row.get("start_local") or ""),
            str(row.get("doctor_name") or ""),
            str(row.get("laser_device_name") or ""),
        )
    )
    return windows


def _requested_time(output: dict[str, Any]) -> str:
    requested = str(output.get("requested_start_time") or "").strip()
    if requested:
        return requested
    window = output.get("requested_time_window")
    if not isinstance(window, dict):
        return ""
    lower = str(window.get("not_before_time") or "").strip()
    upper = str(window.get("not_after_time") or "").strip()
    return lower if lower and lower == upper else ""


def _closing(
    *,
    reschedule: bool,
    booking_authorized: bool,
    ranges: bool,
    has_devices: bool = False,
) -> str:
    choice = "الجهاز والوقت" if has_devices else "الوقت"
    if reschedule:
        return f"قولي {choice} اللي يناسبك جوه الفترات دي عشان أغيّر الموعد." if ranges else f"اختار {choice} اللي يناسبك عشان أغيّره."
    if booking_authorized:
        return f"قولي {choice} اللي يناسبك جوه الفترات دي عشان أحجزه." if ranges else f"اختار {choice} اللي يناسبك عشان أحجزه."
    return f"لو حابب تحجز، قولي {choice} اللي يناسبك جوه الفترات دي." if ranges else f"لو حابب تحجز، قولي {choice} اللي يناسبك."


def _legacy_slot_reply(
    output: dict[str, Any],
    slots: list[object],
    *,
    reschedule: bool,
    booking_authorized: bool,
) -> str | None:
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    has_devices = False
    for raw_slot in slots:
        if not isinstance(raw_slot, dict):
            continue
        start = str(raw_slot.get("start_time_24h") or "").strip()
        if not start:
            continue
        doctor = str(raw_slot.get("doctor_name") or "الدكتور المتاح").strip() or "الدكتور المتاح"
        _device_key, device = _device_from_slot(raw_slot)
        has_devices = has_devices or bool(device)
        if start not in grouped[(doctor, device)]:
            grouped[(doctor, device)].append(start)

    lines: list[str] = []
    for (doctor, device), starts in list(grouped.items())[:8]:
        if starts:
            prefix = f"{device} مع {doctor}" if device else doctor
            lines.append(f"{prefix}: " + "، ".join(starts[:4]))
    if not lines:
        return None

    date_text = _display_date(output.get("date"))
    when = f" يوم {date_text}" if date_text else ""
    if output.get("requested_time_unavailable"):
        requested = _requested_time(output)
        requested_text = f" {requested}" if requested else ""
        intro = f"ميعاد{requested_text}{when} مش متاح. دي أقرب المواعيد المتاحة:"
    else:
        intro = f"دي المواعيد البديلة المتاحة{when}:" if reschedule else f"دي أقرب المواعيد المتاحة{when}:"
    return "\n".join([
        intro,
        *lines,
        _closing(
            reschedule=reschedule,
            booking_authorized=booking_authorized,
            ranges=False,
            has_devices=has_devices,
        ),
    ])


def format_availability_windows_reply(
    output: dict[str, Any],
    *,
    reschedule: bool = False,
    booking_authorized: bool = True,
) -> str | None:
    if output.get("ok") is False:
        return None

    raw_slots = output.get("slots")
    slots = raw_slots if isinstance(raw_slots, list) else []
    windows = output.get("availability_windows")
    if not isinstance(windows, list) or not windows:
        windows = availability_windows_from_slots(slots)

    if not windows:
        if slots:
            return _legacy_slot_reply(
                output,
                slots,
                reschedule=reschedule,
                booking_authorized=booking_authorized,
            )

        date_text = _display_date(output.get("date"))
        when = f" يوم {date_text}" if date_text else ""
        requested_window = output.get("requested_time_window")
        has_requested_window = isinstance(requested_window, dict) and any(
            requested_window.get(key) for key in ("not_before_time", "not_after_time")
        )
        if has_requested_window or output.get("requested_time_unavailable"):
            return f"مفيش مواعيد متاحة في الوقت المطلوب{when}. ممكن أشوفلك وقت تاني في نفس اليوم لو تحب."
        return f"مفيش مواعيد متاحة{when}. ممكن أشوفلك يوم تاني لو تحب."

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    has_devices = False
    for window in windows:
        if not isinstance(window, dict):
            continue
        doctor = str(window.get("doctor_name") or "الدكتور المتاح").strip() or "الدكتور المتاح"
        device = str(window.get("laser_device_name") or "").strip()
        has_devices = has_devices or bool(device)
        grouped[(doctor, device)].append(window)

    lines: list[str] = []
    has_ranges = False
    for (doctor, device), group_windows in list(grouped.items())[:8]:
        ranges: list[str] = []
        for window in group_windows[:4]:
            start = _parse_dt(window.get("start_local"))
            end = _parse_dt(window.get("end_local"))
            if start is None or end is None:
                continue
            if start == end:
                ranges.append(f"الساعة {_clock_ar(start)}")
            else:
                has_ranges = True
                ranges.append(f"من {_clock_ar(start)} لـ{_clock_ar(end)}")
        if not ranges:
            continue
        label = f"{device} مع {doctor}" if device else f"مع {doctor}"
        lines.append(
            f"المتاح على {label} {'، و'.join(ranges)}."
            if device
            else f"المتاح {label} {'، و'.join(ranges)}."
        )

    if not lines:
        return _legacy_slot_reply(
            output,
            slots,
            reschedule=reschedule,
            booking_authorized=booking_authorized,
        )

    date_text = _display_date(output.get("date"))
    intro = f"المتاح يوم {date_text}:" if date_text else "المتاح:"
    if output.get("requested_time_unavailable"):
        requested = _requested_time(output)
        requested_text = f" {requested}" if requested else ""
        when = f" يوم {date_text}" if date_text else ""
        intro = f"ميعاد{requested_text}{when} مش متاح. أقرب فترات متاحة:"

    return "\n".join([
        intro,
        *lines,
        _closing(
            reschedule=reschedule,
            booking_authorized=booking_authorized,
            ranges=has_ranges,
            has_devices=has_devices,
        ),
    ])


_LOCATION_KEYS = {
    "branch",
    "branches",
    "branch_id",
    "branch_name",
    "branch_query",
    "branch_candidate_ids",
    "branch_ids",
    "scheduled_branch_ids",
    "preferred_branch_id",
    "primary_branch_id",
}
_DURATION_KEYS = {
    "duration",
    "duration_minutes",
    "service_duration_minutes",
}


def customer_visible_verified_data(value: Any) -> Any:
    """Remove internal location and scheduling-duration metadata before customer composition.

    Appointment date/time remains customer-visible. Service/session duration is an
    internal scheduling fact and must never be exposed in Linka's customer reply.
    """
    if isinstance(value, dict):
        return {
            key: customer_visible_verified_data(item)
            for key, item in value.items()
            if key not in _LOCATION_KEYS and key not in _DURATION_KEYS
        }
    if isinstance(value, list):
        return [customer_visible_verified_data(item) for item in value]
    return value