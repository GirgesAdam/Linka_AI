from __future__ import annotations

import json

AVAILABILITY_PRESENTATION_PAGE_SIZE = 4


def availability_window_key(
    window: dict[str, object],
    *,
    service_name: object = None,
) -> str:
    """Stable presentation identity for one already-verified availability window."""
    payload = {
        "service_name": str(service_name or ""),
        "doctor_name": str(window.get("doctor_name") or ""),
        "laser_device_name": str(window.get("laser_device_name") or ""),
        "start_local": str(window.get("start_local") or ""),
        "end_local": str(window.get("end_local") or ""),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def availability_window_sort_key(window: dict[str, object]) -> tuple[str, str, str]:
    """Nearest-first without crossing doctor/device truth boundaries."""
    return (
        str(window.get("start_local") or ""),
        str(window.get("doctor_name") or ""),
        str(window.get("laser_device_name") or ""),
    )


def select_availability_window_page(
    windows: list[dict[str, object]],
    *,
    service_name: object = None,
    shown_keys: set[str] | frozenset[str] | None = None,
    page_size: int = AVAILABILITY_PRESENTATION_PAGE_SIZE,
) -> tuple[list[dict[str, object]], list[str], bool]:
    """Return the next deterministic WhatsApp-sized page of verified windows."""
    already = set(shown_keys or ())
    ordered = sorted(windows, key=availability_window_sort_key)
    remaining: list[tuple[str, dict[str, object]]] = []
    seen: set[str] = set()
    for window in ordered:
        key = availability_window_key(window, service_name=service_name)
        if key in seen or key in already:
            continue
        seen.add(key)
        remaining.append((key, window))

    selected = remaining[: max(1, page_size)]
    return (
        [window for _key, window in selected],
        [key for key, _window in selected],
        len(remaining) > len(selected),
    )


def availability_windows_from_outcome_facts(
    facts: dict[str, object],
) -> tuple[object, list[dict[str, object]]]:
    availability = facts.get("availability")
    if not isinstance(availability, dict):
        return None, []
    raw = availability.get("availability_windows")
    windows = [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    return availability.get("service_name"), windows
