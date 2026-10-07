from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.automation_rules import DEFAULT_AUTOMATION_RULES, scheduled_for


def _rules_by_key():
    return {rule.key: rule for rule in DEFAULT_AUTOMATION_RULES}


def test_current_product_keeps_only_followup_automations() -> None:
    rules = _rules_by_key()
    assert set(rules) == {
        "appointment_reminder_6h",
        "post_visit_followup",
        "cancellation_recovery",
        "lead_not_booked_followup",
    }
    assert "booking_confirmation" not in rules

    reminder = rules["appointment_reminder_6h"]
    assert reminder.name == "Appointment confirmation"
    assert reminder.trigger_kind == "before_appointment"
    assert reminder.offset_minutes == -360
    assert reminder.template_name == "tia_appointment_confirmation_01"
    assert reminder.enabled_by_default is True

    assert rules["post_visit_followup"].enabled_by_default is False
    assert rules["cancellation_recovery"].enabled_by_default is False
    assert rules["lead_not_booked_followup"].enabled_by_default is False


def test_rule_timing_is_data_not_a_separate_rule_per_delay() -> None:
    start = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    created = start - timedelta(days=2)
    completed = start + timedelta(hours=1)

    assert scheduled_for(
        trigger_kind="before_appointment",
        offset_minutes=-90,
        appointment_created_at=created,
        appointment_start_at=start,
        completed_at=None,
        no_show_at=None,
    ) == start - timedelta(minutes=90)

    assert scheduled_for(
        trigger_kind="after_completed",
        offset_minutes=2 * 1440,
        appointment_created_at=created,
        appointment_start_at=start,
        completed_at=completed,
        no_show_at=None,
    ) == completed + timedelta(days=2)


def test_automation_runtime_is_whatsapp_only() -> None:
    root = Path(__file__).resolve().parents[2]
    workflows = root / "n8n" / "workflows"
    route = (root / "backend/app/api/routes/whatsapp_setup.py").read_text(encoding="utf-8")

    assert not (workflows / "tia_gmail_outbox_worker.json").exists()
    assert (workflows / "tia_whatsapp_outbox_worker.json").exists()
    assert not (workflows / "tia_whatsapp_inbound_status.json").exists()
    assert (workflows / "tia_automation_scheduler.json").exists()
    assert '@router.post("/webhook/{connection_id}"' in route
    assert '@router.post("/transport/tick")' in route


def test_admin_ui_keeps_optional_rules_and_timing_simple() -> None:
    root = Path(__file__).resolve().parents[2]
    page = (root / "frontend" / "src" / "app" / "(dashboard)" / "automations" / "page.tsx").read_text(
        encoding="utf-8"
    )
    actions = (root / "frontend" / "src" / "app" / "(dashboard)" / "automations" / "actions.ts").read_text(
        encoding="utf-8"
    )
    component = (root / "frontend" / "src" / "components" / "automation-timing-form.tsx").read_text(
        encoding="utf-8"
    )

    assert '"cancellation_recovery"' in page
    assert '"lead_not_booked_followup"' in page
    assert '"booking_confirmation"' not in page.split("const visibleProductRuleKeys", 1)[1].split("]);", 1)[0]
    assert '"no_show_followup"' not in page
    assert "AutomationTimingForm" in page
    assert "saveAutomationTiming" in component
    assert 'name="timing_value"' in component
    assert 'name="timing_unit"' in component
    assert "saveAutomationTiming" in actions
    assert "timing_unit" in actions
