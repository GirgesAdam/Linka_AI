from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from app.agents.v2.turn_contract import Selection

ReferenceStatus = Literal["resolved", "needs_anchor", "out_of_range", "window_ambiguous", "explicit_time_mismatch", "unavailable"]


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _same_dimension(slot: dict[str, object], window: dict[str, object], key: str) -> bool:
    window_value = str(window.get(key) or "").strip()
    if not window_value:
        return True
    return str(slot.get(key) or "").strip() == window_value


def _slots_inside_window(
    slots: list[dict[str, object]],
    window: dict[str, object],
) -> list[dict[str, object]]:
    window_start = _parse_dt(window.get("start_local"))
    window_end = _parse_dt(window.get("end_local"))
    if window_start is None or window_end is None:
        return []
    matches: list[dict[str, object]] = []
    for slot in slots:
        if not _same_dimension(slot, window, "doctor_name"):
            continue
        if not _same_dimension(slot, window, "laser_device_name"):
            continue
        slot_start = _parse_dt(slot.get("start_local"))
        if slot_start is None:
            continue
        # Availability presentation windows describe verified *bookable start*
        # ranges, not appointment-duration intervals. A discrete displayed time
        # therefore has start_local == end_local even though its canonical slot
        # has a later appointment end. Bind only by verified starts inside the
        # displayed start range; never infer from an appointment end boundary.
        if window_start <= slot_start <= window_end:
            matches.append(slot)
    return matches


def _safe_slot(slot: dict[str, object]) -> dict[str, object]:
    keys = (
        "branch_id",
        "branch_name",
        "service_id",
        "service_name",
        "doctor_id",
        "doctor_name",
        "start_at",
        "end_at",
        "start_local",
        "end_local",
        "start_time_24h",
        "end_time_24h",
        "duration_minutes",
        "laser_device_key",
        "laser_device_name",
        "price_minor",
        "currency",
    )
    return {key: slot[key] for key in keys if slot.get(key) not in (None, "")}


def build_availability_reference_options(
    *,
    displayed_windows: list[dict[str, object]],
    verified_slots: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Map each displayed availability item back to verified canonical slots.

    A displayed window is selectable as one concrete time only when exactly one raw
    verified slot fills that exact window. A compressed multi-slot window remains
    non-concrete and can never be converted into an invented appointment time.
    """
    options: list[dict[str, object]] = []
    for index, window in enumerate(displayed_windows, start=1):
        candidates = _slots_inside_window(verified_slots, window)
        window_start = _parse_dt(window.get("start_local"))
        window_end = _parse_dt(window.get("end_local"))
        discrete = (
            window_start is not None
            and window_end is not None
            and window_start == window_end
        )
        exact = [
            slot
            for slot in candidates
            if str(slot.get("start_local") or "") == str(window.get("start_local") or "")
        ]
        concrete = discrete and len(candidates) == 1 and len(exact) == 1
        option: dict[str, object] = {
            "option_ref": f"opt_{index}",
            "index": index,
            "concrete": concrete,
            "start_local": window.get("start_local"),
            "end_local": window.get("end_local"),
            "start_time_24h": window.get("start_time_24h"),
            "end_time_24h": window.get("end_time_24h"),
            "doctor_name": window.get("doctor_name"),
            "laser_device_name": window.get("laser_device_name"),
            "canonical_slot_count": len(candidates),
        }
        if concrete:
            option["slot"] = _safe_slot(exact[0])
        options.append({key: value for key, value in option.items() if value not in (None, "")})
    return options


def _target_index(selection: Selection, *, anchor: int | None, count: int) -> tuple[int | None, ReferenceStatus]:
    if count <= 0:
        return None, "unavailable"
    if selection.kind == "index" and selection.index is not None:
        target = selection.index
    elif selection.kind == "relative":
        relative = selection.relative
        if relative == "first":
            target = 1
        elif relative == "last":
            target = count
        elif relative in {"next", "previous"}:
            if anchor is None:
                return None, "needs_anchor"
            target = anchor + (1 if relative == "next" else -1)
        else:
            return None, "unavailable"
    else:
        return None, "unavailable"
    if not 1 <= target <= count:
        return None, "out_of_range"
    return target, "resolved"


def resolve_verified_availability_reference(
    selection: Selection,
    read_context: dict[str, Any] | None,
    *,
    explicit_user_time: str | None = None,
) -> dict[str, object]:
    raw_options = (read_context or {}).get("availability_reference_options")
    options = [dict(item) for item in raw_options if isinstance(item, dict)] if isinstance(raw_options, list) else []
    if selection.kind == "ref" and selection.ref:
        matches = [
            option for option in options
            if str(option.get("option_ref") or "") == selection.ref
        ]
        if len(matches) != 1:
            return {"status": "unavailable"}
        option = matches[0]
        raw_index = option.get("index")
        target = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else None
        if target is None:
            return {"status": "unavailable"}
        status: ReferenceStatus = "resolved"
    else:
        raw_anchor = (read_context or {}).get("availability_reference_anchor_index")
        anchor = raw_anchor if isinstance(raw_anchor, int) and not isinstance(raw_anchor, bool) else None
        target, status = _target_index(selection, anchor=anchor, count=len(options))
        if target is None:
            return {"status": status}
        option = options[target - 1]

    result: dict[str, object] = {"status": status, "index": target, "option": option}
    if option.get("concrete") is not True or not isinstance(option.get("slot"), dict):
        result["status"] = "window_ambiguous"
        return result

    explicit = str(explicit_user_time or "").strip()[:5]
    if explicit:
        slot = option.get("slot")
        slot_time = str(slot.get("start_time_24h") or "").strip()[:5] if isinstance(slot, dict) else ""
        if slot_time != explicit:
            result["status"] = "explicit_time_mismatch"
            return result

    result["status"] = "resolved"
    return result

def customer_safe_reference_option(option: dict[str, object]) -> dict[str, object]:
    slot = option.get("slot")
    safe_slot = slot if isinstance(slot, dict) else {}
    safe = {
        "option_ref": option.get("option_ref"),
        "index": option.get("index"),
        "concrete": option.get("concrete"),
        "start_local": safe_slot.get("start_local") or option.get("start_local"),
        "end_local": safe_slot.get("end_local") or option.get("end_local"),
        "start_time_24h": safe_slot.get("start_time_24h") or option.get("start_time_24h"),
        "end_time_24h": safe_slot.get("end_time_24h") or option.get("end_time_24h"),
        "doctor_name": safe_slot.get("doctor_name") or option.get("doctor_name"),
        "laser_device_name": safe_slot.get("laser_device_name") or option.get("laser_device_name"),
        "service_name": safe_slot.get("service_name"),
    }
    return {key: value for key, value in safe.items() if value not in (None, "")}
