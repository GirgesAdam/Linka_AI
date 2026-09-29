from datetime import datetime
from zoneinfo import ZoneInfo

from app.agents.prompts.customer_service import build_customer_service_system_prompt


def _prompt() -> str:
    return build_customer_service_system_prompt(
        clinic_name="Linka",
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 8, 12, 18, 0, tzinfo=ZoneInfo("Africa/Cairo")),
    )


def test_customer_prompt_keeps_customer_facing_contract_small_and_clear() -> None:
    prompt = _prompt()

    assert "العربي المصري" in prompt
    assert "نتيجة Tool ناجحة" in prompt
    assert "UUIDs" in prompt
    assert "مش دكتور" in prompt
    assert "Africa/Cairo" in prompt
    assert "2026-08-12" in prompt
    assert len(prompt) < 6500


def test_customer_prompt_does_not_duplicate_deterministic_scheduling_contracts() -> None:
    prompt = _prompt()

    for implementation_detail in (
        "requested_start_time",
        "not_before_time",
        "not_after_time",
        "start_time_24h",
        "book_appointment",
        "cancel_appointment",
    ):
        assert implementation_detail not in prompt


def test_customer_prompt_requires_verified_execution_before_success_claims() -> None:
    prompt = _prompt()

    success_rule = next(
        line for line in prompt.splitlines() if "أي إجراء تم" in line
    )
    assert "نتيجة Tool ناجحة" in success_rule or "سياق موثّق" in success_rule
