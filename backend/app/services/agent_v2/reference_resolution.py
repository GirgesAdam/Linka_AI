from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from app.agents.v2.turn_contract import Selection

ReferenceStatus = Literal["resolved", "needs_anchor", "out_of_range", "window_ambiguous", "unavailable"]

_DISCOURSE_TOKENS = frozenset({"طب", "طيب"})
_REFERENCE_NOUNS = frozenset({"المعاد", "الميعاد", "الموعد", "ميعاد", "موعد", "الاختيار", "اختيار"})
_UNIT_TOKENS = frozenset({"واحد", "واحدة"})
_FIRST_TOKENS = frozenset({"اول", "الاول", "الاولي", "اولي"})
_SECOND_TOKENS = frozenset({"التاني", "تاني", "الثاني", "ثاني", "الثانية", "التانية"})
_THIRD_TOKENS = frozenset({"التالت", "تالت", "الثالث", "ثالث", "الثالثة", "التالتة"})
_LAST_TOKENS = frozenset({"اخر", "الاخر", "الاخير", "اخير"})
_NEXT_TOKENS = frozenset({"بعده", "بعدها"})
_PREVIOUS_TOKENS = frozenset({"قبله", "قبلها"})
_RELATIVE_LINKERS = frozenset({"اللي", "الي"})


def _reference_tokens(text: str) -> list[str]:
    normalized = str(text or "").strip().lower()
    for old, new in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي")):
        normalized = normalized.replace(old, new)
    for mark in ("؟", "?", "!", ".", ",", "،", ":", ";", "؛"):
        normalized = normalized.replace(mark, " ")
    tokens = [token for token in normalized.split() if token]
    while tokens and tokens[0] in _DISCOURSE_TOKENS:
        tokens.pop(0)
    return tokens


def infer_positional_reference_selection(text: str) -> Selection | None:
    """Parse one complete positional-reference utterance into the typed selection contract.

    This is a deliberately small grammar, not a broad keyword router: the entire
    customer turn must reduce to an ordinal/relative reference. Extra action words
    such as a refresh request keep the turn on the normal semantic/read path.
    """
    tokens = _reference_tokens(text)
    if not tokens:
        return None

    if tokens and tokens[0] in _REFERENCE_NOUNS:
        tokens = tokens[1:]
    if tokens and tokens[0] in _RELATIVE_LINKERS:
        tokens = tokens[1:]
    if not tokens:
        return None

    if len(tokens) == 1 and tokens[0] in _NEXT_TOKENS:
        return Selection(kind="relative", relative="next")
    if len(tokens) == 1 and tokens[0] in _PREVIOUS_TOKENS:
        return Selection(kind="relative", relative="previous")

    core = list(tokens)
    if len(core) == 2 and core[1] in _UNIT_TOKENS:
        core = core[:1]
    if len(core) != 1:
        return None
    token = core[0]
    if token in _FIRST_TOKENS:
        return Selection(kind="relative", relative="first")
    if token in _SECOND_TOKENS:
        return Selection(kind="index", index=2)
    if token in _THIRD_TOKENS:
        return Selection(kind="index", index=3)
    if token in _LAST_TOKENS:
        return Selection(kind="relative", relative="last")
    return None


def recover_verified_availability_reference_selection(
    *,
    operation_type: str,
    continues_previous: bool,
    existing_selection: Selection | None,
    latest_customer_text: str,
    recent_read_context: dict[str, Any] | None,
) -> Selection | None:
    if existing_selection is not None:
        return existing_selection
    if operation_type != "availability" or not continues_previous:
        return None
    raw_options = (recent_read_context or {}).get("availability_reference_options")
    if not isinstance(raw_options, list) or not raw_options:
        return None
    return infer_positional_reference_selection(latest_customer_text)


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
        slot_end = _parse_dt(slot.get("end_local"))
        if slot_start is None or slot_end is None:
            continue
        if window_start <= slot_start and slot_end <= window_end:
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
        exact = [
            slot
            for slot in candidates
            if str(slot.get("start_local") or "") == str(window.get("start_local") or "")
            and str(slot.get("end_local") or "") == str(window.get("end_local") or "")
        ]
        concrete = len(candidates) == 1 and len(exact) == 1
        option: dict[str, object] = {
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
) -> dict[str, object]:
    raw_options = (read_context or {}).get("availability_reference_options")
    options = [dict(item) for item in raw_options if isinstance(item, dict)] if isinstance(raw_options, list) else []
    raw_anchor = (read_context or {}).get("availability_reference_anchor_index")
    anchor = raw_anchor if isinstance(raw_anchor, int) and not isinstance(raw_anchor, bool) else None
    target, status = _target_index(selection, anchor=anchor, count=len(options))
    result: dict[str, object] = {"status": status}
    if target is None:
        return result
    option = options[target - 1]
    result["index"] = target
    result["option"] = option
    if option.get("concrete") is not True or not isinstance(option.get("slot"), dict):
        result["status"] = "window_ambiguous"
        return result
    result["status"] = "resolved"
    return result


def customer_safe_reference_option(option: dict[str, object]) -> dict[str, object]:
    slot = option.get("slot")
    safe_slot = slot if isinstance(slot, dict) else {}
    safe = {
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
