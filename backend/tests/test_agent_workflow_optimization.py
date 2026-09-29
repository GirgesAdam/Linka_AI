from datetime import UTC, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.agents.prompts.customer_service import build_customer_service_system_prompt
from app.agents.tools.clinic_tools import _filter_slots_by_local_window


def _slot(start_hour: int, start_minute: int, duration_minutes: int = 60):
    start = datetime(
        2026,
        8,
        13,
        start_hour,
        start_minute,
        tzinfo=ZoneInfo("Africa/Cairo"),
    )
    from datetime import timedelta

    end = start + timedelta(minutes=duration_minutes)

    return SimpleNamespace(
        start_at=start.astimezone(UTC),
        end_at=end.astimezone(UTC),
    )


def test_upper_bound_requires_whole_appointment_to_fit() -> None:
    tz = ZoneInfo("Africa/Cairo")
    slots = [
        _slot(20, 0),
        _slot(20, 15),
        _slot(20, 30),
    ]

    filtered = _filter_slots_by_local_window(
        slots,
        tz=tz,
        lower_bound=time(20, 0),
        upper_bound=time(21, 0),
    )

    assert len(filtered) == 1
    assert filtered[0][1].strftime("%H:%M") == "20:00"


def test_prompt_keeps_composite_tool_mechanics_out_of_customer_contract() -> None:
    prompt = build_customer_service_system_prompt(
        clinic_name="Linka",
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 8, 12, 20, 0),
    )

    assert "get_booking_options" not in prompt
    assert "get_reschedule_options" not in prompt
    assert "نتيجة Tool ناجحة" in prompt
    assert "سياق موثّق" in prompt