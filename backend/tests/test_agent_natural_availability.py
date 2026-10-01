from __future__ import annotations

from app.agents.availability_presentation import (
    availability_windows_from_slots,
    customer_visible_verified_data,
    format_availability_windows_reply,
)
from app.agents.semantic_actions import (
    format_booking_success,
    select_slot_from_structured_selection,
)


def _slot(
    *,
    doctor_id: str,
    doctor_name: str,
    start: str,
    end: str,
    branch_name: str = "Internal Main Branch",
) -> dict[str, object]:
    return {
        "doctor_id": doctor_id,
        "doctor_name": doctor_name,
        "branch_id": "internal-branch-id",
        "branch_name": branch_name,
        "service_id": "service-id",
        "service_name": "Underarm Laser",
        "start_local": f"2026-09-10T{start}:00+03:00",
        "end_local": f"2026-09-10T{end}:00+03:00",
        "start_time_24h": start,
        "end_time_24h": end,
    }


def test_quarter_hour_starts_become_one_bookable_start_window() -> None:
    slots = [
        _slot(doctor_id="d1", doctor_name="د. مريم", start="15:00", end="15:15"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="15:15", end="15:30"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="15:30", end="15:45"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="15:45", end="16:00"),
    ]

    windows = availability_windows_from_slots(slots)

    assert len(windows) == 1
    assert windows[0]["start_time_24h"] == "15:00"
    assert windows[0]["end_time_24h"] == "15:45"


def test_five_half_hour_starts_are_presented_as_six_to_eight_window() -> None:
    slots = [
        _slot(doctor_id="d1", doctor_name="د. مريم", start="18:00", end="18:30"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="18:30", end="19:00"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="19:00", end="19:30"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="19:30", end="20:00"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="20:00", end="20:30"),
    ]

    windows = availability_windows_from_slots(slots)
    reply = format_availability_windows_reply(
        {"ok": True, "availability_windows": windows},
        booking_authorized=False,
    )

    assert [(row["start_time_24h"], row["end_time_24h"]) for row in windows] == [
        ("18:00", "20:00")
    ]
    assert reply is not None
    assert "من 6 م لـ8 م" in reply
    assert "8:30" not in reply


def test_existing_booking_splits_availability_into_two_start_windows() -> None:
    slots = [
        _slot(doctor_id="d1", doctor_name="د. مريم", start="15:00", end="16:00"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="16:00", end="17:00"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="17:00", end="18:00"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="19:00", end="20:00"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="20:00", end="21:00"),
    ]

    windows = availability_windows_from_slots(slots)

    assert [(row["start_time_24h"], row["end_time_24h"]) for row in windows] == [
        ("15:00", "17:00"),
        ("19:00", "20:00"),
    ]


def test_real_gap_is_not_hidden_inside_availability_range() -> None:
    slots = [
        _slot(doctor_id="d1", doctor_name="د. مريم", start="18:00", end="18:30"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="18:30", end="19:00"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="19:00", end="19:30"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="21:00", end="21:30"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="21:30", end="22:00"),
    ]

    windows = availability_windows_from_slots(slots)

    assert [(row["start_time_24h"], row["end_time_24h"]) for row in windows] == [
        ("18:00", "19:00"),
        ("21:00", "21:30"),
    ]


def test_isolated_slot_is_presented_as_one_time_not_zero_length_range() -> None:
    slots = [_slot(doctor_id="d1", doctor_name="د. مريم", start="18:00", end="18:30")]

    reply = format_availability_windows_reply(
        {"ok": True, "availability_windows": availability_windows_from_slots(slots)},
        booking_authorized=False,
    )

    assert reply is not None
    assert "الساعة 6 م" in reply
    assert "من 6 م لـ6 م" not in reply


def test_windows_never_merge_different_doctors() -> None:
    slots = [
        _slot(doctor_id="d1", doctor_name="د. مريم", start="15:00", end="18:00"),
        _slot(doctor_id="d2", doctor_name="د. سارة", start="15:00", end="18:00"),
    ]

    windows = availability_windows_from_slots(slots)

    assert len(windows) == 2
    assert {row["doctor_id"] for row in windows} == {"d1", "d2"}


def test_windows_never_merge_different_devices() -> None:
    base = _slot(
        doctor_id="d1",
        doctor_name="د. مريم",
        start="15:00",
        end="18:00",
    )
    slots = [
        {**base, "laser_device_key": "device-a", "laser_device_name": "Candela"},
        {**base, "laser_device_key": "device-b", "laser_device_name": "Prime Lase"},
    ]

    windows = availability_windows_from_slots(slots)

    assert len(windows) == 2
    assert {row["laser_device_key"] for row in windows} == {"device-a", "device-b"}


def test_customer_reply_uses_start_ranges_not_grid_or_branch() -> None:
    slots = [
        _slot(doctor_id="d1", doctor_name="د. مريم", start="15:00", end="15:15"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="15:15", end="15:30"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="15:30", end="15:45"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="15:45", end="16:00"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="19:00", end="20:00"),
        _slot(doctor_id="d1", doctor_name="د. مريم", start="20:00", end="21:00"),
    ]
    payload = {
        "ok": True,
        "date": "2026-09-10",
        "branch": {"branch_name": "Internal Main Branch"},
        "slots": slots,
        "availability_windows": availability_windows_from_slots(slots),
    }

    reply = format_availability_windows_reply(payload, booking_authorized=True)

    assert reply is not None
    assert "المتاح مع د. مريم من 3 م لـ3:45 م، ومن 7 م لـ8 م." in reply
    assert "15:15" not in reply
    assert "15:30" not in reply
    assert "Internal Main Branch" not in reply
    assert "فرع" not in reply
    assert "قولي الوقت" in reply


def test_booking_success_does_not_expose_internal_branch() -> None:
    reply = format_booking_success(
        {
            "status": "confirmed",
            "service": "ليزر إبط",
            "branch": "Internal Main Branch",
            "doctor": "د. مريم",
            "start_local": "2026-09-10T15:00:00+03:00",
            "end_local": "2026-09-10T15:15:00+03:00",
            "price": "550 EGP",
        }
    )

    assert "Internal Main Branch" not in reply
    assert "فرع" not in reply
    assert "ليزر إبط" in reply
    assert "د. مريم" in reply


def test_time_selection_does_not_silently_choose_between_doctors() -> None:
    booking_output = {
        "ok": True,
        "slots": [
            _slot(doctor_id="d1", doctor_name="د. مريم", start="15:00", end="15:15"),
            _slot(doctor_id="d2", doctor_name="د. سارة", start="15:00", end="15:15"),
        ],
    }

    selected = select_slot_from_structured_selection(
        booking_output,
        selection_index=None,
        selection_time="15:00",
    )

    assert selected is None


def test_time_selection_uses_explicit_doctor_when_same_time_is_shared() -> None:
    booking_output = {
        "ok": True,
        "slots": [
            _slot(doctor_id="d1", doctor_name="د. مريم", start="15:00", end="15:15"),
            _slot(doctor_id="d2", doctor_name="د. سارة", start="15:00", end="15:15"),
        ],
    }

    selected = select_slot_from_structured_selection(
        booking_output,
        selection_index=None,
        selection_time="15:00",
        doctor_id="d2",
    )

    assert selected is not None
    assert selected["doctor_id"] == "d2"


def test_legacy_numbered_slot_selection_still_works() -> None:
    booking_output = {
        "ok": True,
        "slots": [
            _slot(doctor_id="d1", doctor_name="د. مريم", start="15:00", end="15:15"),
            _slot(doctor_id="d1", doctor_name="د. مريم", start="15:15", end="15:30"),
        ],
    }

    selected = select_slot_from_structured_selection(
        booking_output,
        selection_index=2,
        selection_time=None,
    )

    assert selected is not None
    assert selected["start_time_24h"] == "15:15"


def test_customer_composer_data_strips_all_storage_location_fields() -> None:
    value = {
        "branch": {"branch_id": "b1", "branch_name": "Main"},
        "catalog": {
            "branches": [{"id": "b1", "name": "Main"}],
            "doctors": [
                {
                    "name": "د. مريم",
                    "branch_ids": ["b1"],
                    "scheduled_branch_ids": ["b1"],
                    "service_ids": ["s1"],
                }
            ],
        },
        "service": {"name": "ليزر إبط"},
    }

    clean = customer_visible_verified_data(value)

    encoded = str(clean)
    assert "branch" not in encoded.lower()
    assert "Main" not in encoded
    assert "ليزر إبط" in encoded
