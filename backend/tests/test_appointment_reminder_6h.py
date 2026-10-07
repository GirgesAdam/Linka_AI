from datetime import UTC, datetime
from pathlib import Path

from app.core.automation_rules import DEFAULT_AUTOMATION_RULES, scheduled_for


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_default_lifecycle_has_one_configurable_appointment_reminder() -> None:
    rules = {rule.key: rule for rule in DEFAULT_AUTOMATION_RULES}

    assert "appointment_reminder_24h" not in rules
    assert "appointment_reminder_2h" not in rules

    reminder = rules["appointment_reminder_6h"]
    assert reminder.enabled_by_default is True
    assert reminder.trigger_kind == "before_appointment"
    assert reminder.offset_minutes == -360
    assert reminder.name == "Appointment confirmation"
    assert reminder.template_name == "tia_appointment_confirmation_01"

    assert rules["post_visit_followup"].enabled_by_default is False


def test_default_reminder_and_post_visit_offsets_remain_valid_starting_values() -> None:
    start = datetime(2026, 9, 5, 18, 0, tzinfo=UTC)
    completed = datetime(2026, 9, 5, 19, 15, tzinfo=UTC)
    created = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

    reminder = scheduled_for(
        trigger_kind="before_appointment",
        offset_minutes=-360,
        appointment_created_at=created,
        appointment_start_at=start,
        completed_at=None,
        no_show_at=None,
    )
    post_visit = scheduled_for(
        trigger_kind="after_completed",
        offset_minutes=1440,
        appointment_created_at=created,
        appointment_start_at=start,
        completed_at=completed,
        no_show_at=None,
    )

    assert reminder == datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert post_visit == datetime(2026, 9, 6, 19, 15, tzinfo=UTC)


def test_historical_migration_disabled_old_reminders_and_materialized_6h_key() -> None:
    migration = (
        _root() / "backend/alembic/versions/0026_appointment_reminder_6h.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | Sequence[str] | None = "0025_auto_patient_lifecycle"' in migration
    assert "appointment_reminder_24h" in migration
    assert "appointment_reminder_2h" in migration
    assert "SET enabled = FALSE" in migration
    assert "superseded_by_6h_reminder" in migration
    assert "UPDATE message_dispatches" in migration
    assert "UPDATE messages" in migration
    assert "status = 'dispatched'" in migration
    assert "appointment_reminder_6h" in migration
    assert "offset_minutes = -360" in migration
    assert "tia_appointment_reminder_6h_ar" in migration


def test_configurable_reminder_copy_is_timing_neutral_and_post_visit_matches_meta() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")

    reminder = service.split('if rule_key == "appointment_reminder_6h":', 1)[1].split(
        'if rule_key == "appointment_reminder_24h":', 1
    )[0]
    post_visit = service.split('if rule_key == "post_visit_followup":', 1)[1].split(
        'if rule_key == "no_show_followup":', 1
    )[0]

    assert "فاضل حوالي 6 ساعات" not in reminder
    assert "{data['date']}" in reminder
    assert "{data['branch_name']}" not in reminder
    assert "{data['time']}" in reminder
    assert "النهارده" not in reminder
    assert "تأكد حضورك" in reminder
    assert "تغيير الميعاد" in reminder
    assert "حبيت أطمن عليكي بعد {data['service_name']}" in post_visit
    assert "كل حاجة تمام؟" in post_visit


def test_automation_ui_exposes_configurable_reminder_timing() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/automations/page.tsx").read_text(
        encoding="utf-8"
    )
    component = (_root() / "frontend/src/components/automation-timing-form.tsx").read_text(
        encoding="utf-8"
    )
    actions = (_root() / "frontend/src/app/(dashboard)/automations/actions.ts").read_text(
        encoding="utf-8"
    )

    assert page.count("appointment_reminder_6h") >= 2
    assert "AutomationTimingForm" in page
    assert "saveAutomationTiming" in component
    assert 'name="timing_value"' in component
    assert 'name="timing_unit"' in component
    assert "saveAutomationTiming" in actions


def test_setup_documents_timing_neutral_template_contract() -> None:
    setup = (_root() / "n8n/AUTOMATIONS_SETUP.md").read_text(encoding="utf-8")

    assert "tia_reminder_01" in setup
    assert "admin controls the timing" in setup
    assert 'Do not hardcode "6 hours"' in setup
    reminder_line = next(
        line for line in setup.splitlines()
        if "tia_reminder_01" in line and "أهلًا" in line
    )
    assert "جلسة {{2}}" in reminder_line
    assert "{{3}}" in reminder_line
    assert "{{4}}" not in reminder_line and "{{5}}" not in reminder_line
    assert "فاضل" not in reminder_line
    assert "النهارده" not in reminder_line

    post_visit_line = next(
        line for line in setup.splitlines()
        if "tia_post_visit_01" in line and "إزيك" in line
    )
    assert "{{1}}" in post_visit_line and "{{2}}" in post_visit_line and "{{3}}" in post_visit_line
    assert "{{4}}" not in post_visit_line and "{{5}}" not in post_visit_line
    assert "كل حاجة تمام؟" in post_visit_line
