from datetime import UTC, datetime
from pathlib import Path

from app.core.automation_rules import DEFAULT_AUTOMATION_RULES, scheduled_for


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_patient_lifecycle_keeps_essential_reminder_enabled() -> None:
    rules = {rule.key: rule for rule in DEFAULT_AUTOMATION_RULES}
    assert rules["appointment_reminder_6h"].enabled_by_default is True
    assert "appointment_reminder_24h" not in rules
    assert "appointment_reminder_2h" not in rules
    assert rules["post_visit_followup"].enabled_by_default is False
    assert "booking_confirmation" not in rules
    assert "no_show_followup" not in rules


def test_default_offsets_are_starting_values_and_anchor_to_real_events() -> None:
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


def test_existing_workspaces_have_historical_six_hour_migration() -> None:
    migration = (_root() / "backend/alembic/versions/0026_appointment_reminder_6h.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision: str | Sequence[str] | None = "0025_auto_patient_lifecycle"' in migration
    assert "appointment_reminder_24h" in migration
    assert "appointment_reminder_2h" in migration
    assert "appointment_reminder_6h" in migration
    assert "SET enabled = FALSE" in migration
    assert "offset_minutes = -360" in migration


def test_new_workspaces_materialize_rules_using_definition_default() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")
    assert "enabled=definition.enabled_by_default" in service


def test_appointment_templates_use_rule_specific_db_owned_parameters() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")
    assert "def _appointment_template_body_parameters(" in service
    assert "template_name: str | None = None" in service
    assert 'rule_key == "appointment_reminder_6h"' in service
    assert 'return [patient_name, service_name, date, time]' in service
    assert 'return [patient_name, service_name, time, branch_name]' not in service
    assert 'rule_key == "post_visit_followup"' in service
    assert 'return [patient_name, service_name, date]' in service
    assert 'template_name=template_name' in service


def test_lifecycle_message_copy_matches_configurable_product_spec() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")
    reminder = service.split('if rule_key == "appointment_reminder_6h":', 1)[1].split(
        'if rule_key == "appointment_reminder_24h":', 1
    )[0]
    post_visit = service.split('if rule_key == "post_visit_followup":', 1)[1].split(
        'if rule_key == "no_show_followup":', 1
    )[0]

    assert "فاضل حوالي 6 ساعات" not in reminder
    assert "تأكد حضورك" in reminder
    assert "تغيير الميعاد" in reminder
    assert "{data['date']}" in reminder
    assert "{data['branch_name']}" not in reminder
    assert "حبيت أطمن عليكي بعد {data['service_name']}" in post_visit
    assert "كل حاجة تمام؟" in post_visit


def test_native_whatsapp_transport_supports_three_four_and_five_parameter_templates() -> None:
    from uuid import uuid4

    from app.schemas.channel import DispatchClaimItem
    from app.services.meta_whatsapp_transport import build_meta_message_payload

    for count in (3, 4, 5):
        body_parameters = [f"value-{index}" for index in range(count)]
        item = DispatchClaimItem(
            dispatch_id=uuid4(),
            message_id=uuid4(),
            channel="whatsapp",
            provider="meta_cloud",
            external_account_id="123456789",
            external_user_id="201001112223",
            external_conversation_id="201001112223",
            message_type="template",
            content=None,
            metadata={
                "whatsapp_template": {
                    "name": "contract_template",
                    "language_code": "ar",
                    "body_parameters": body_parameters,
                }
            },
            attempt=1,
        )

        payload = build_meta_message_payload(item)
        params = payload["template"]["components"][0]["parameters"]
        assert len(params) == count
        assert [param["text"] for param in params] == body_parameters


def test_setup_documents_exact_template_contract_and_optional_care_messages() -> None:
    setup = (_root() / "n8n/AUTOMATIONS_SETUP.md").read_text(encoding="utf-8")
    assert "exact number of positional body parameters" in setup
    assert "**4 parameters**" in setup
    assert "**3 parameters**" in setup
    assert "tia_reminder_01" in setup
    assert "tia_post_visit_01" in setup
    assert "Only the appointment reminder is enabled by default" in setup
    assert "post-visit" in setup.lower()


def test_customer_reply_after_proactive_message_still_uses_normal_agent_runtime() -> None:
    channels = (_root() / "backend/app/services/channels.py").read_text(encoding="utf-8")
    assert "run_agent_for_existing_inbound(" in channels
    assert "return_to_ai(conversation, now=now)" in channels


def test_reenabled_lifecycle_rule_can_revive_only_lifecycle_cancelled_jobs() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")
    assert "REPLANNABLE_CANCELLATION_REASONS" in service
    assert '"rule_disabled_by_admin"' in service
    assert '"rule_disabled_or_appointment_no_longer_eligible"' in service
    assert "_cancelled_job_can_be_replanned(existing)" in service
    assert "existing.message_id = None" in service
    assert "existing.dispatch_id = None" in service
