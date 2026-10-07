from pathlib import Path

from app.core.automation_rules import DEFAULT_AUTOMATION_RULES


def test_no_show_has_no_separate_product_automation_rule() -> None:
    keys = {rule.key for rule in DEFAULT_AUTOMATION_RULES}
    assert "cancellation_recovery" in keys
    assert "no_show_followup" not in keys


def test_cancellation_recovery_covers_no_show_and_retires_legacy_rule() -> None:
    root = Path(__file__).resolve().parents[2]
    service = (root / "backend/app/services/automations.py").read_text(encoding="utf-8")
    assert 'RETIRED_AUTOMATION_RULE_KEYS = frozenset({"no_show_followup"})' in service
    assert 'status == "no_show" and no_show_at is not None' in service
    assert "appointment.cancelled_at or appointment.no_show_at" in service
    assert 'AutomationRule.key.notin_(RETIRED_AUTOMATION_RULE_KEYS | {"booking_confirmation"})' in service


def test_admin_ui_does_not_offer_duplicate_no_show_followup() -> None:
    root = Path(__file__).resolve().parents[2]
    page = (root / "frontend/src/app/(dashboard)/automations/page.tsx").read_text(encoding="utf-8")
    assert 'cancellation_recovery: "استرجاع الحجوزات الملغاة"' in page
    assert "no_show_followup" not in page
