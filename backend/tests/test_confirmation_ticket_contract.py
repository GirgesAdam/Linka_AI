from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.core.meta_whatsapp_templates import STANDARD_TEMPLATE_BY_RULE_KEY, template_create_payload
from app.schemas.channel import DispatchClaimItem
from app.services.meta_whatsapp_transport import build_meta_message_payload


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_confirmation_template_is_utility_with_exact_two_quick_replies() -> None:
    template = STANDARD_TEMPLATE_BY_RULE_KEY["appointment_reminder_6h"]
    assert template.name == "tia_appointment_confirmation_01"
    assert template.category == "UTILITY"
    assert template.quick_reply_buttons == ("تأكيد الحضور", "تغيير الميعاد")

    payload = template_create_payload(template)
    buttons = next(component for component in payload["components"] if component["type"] == "BUTTONS")
    assert buttons["buttons"] == [
        {"type": "QUICK_REPLY", "text": "تأكيد الحضور"},
        {"type": "QUICK_REPLY", "text": "تغيير الميعاد"},
    ]


def test_template_dispatch_has_appointment_specific_quick_reply_payloads() -> None:
    appointment_id = uuid4()
    item = DispatchClaimItem(
        dispatch_id=uuid4(),
        message_id=uuid4(),
        channel="whatsapp",
        provider="meta_cloud",
        external_account_id="123456",
        external_user_id="201000000000",
        external_conversation_id="201000000000",
        message_type="template",
        content="confirmation",
        metadata={
            "whatsapp_template": {
                "name": "tia_appointment_confirmation_01",
                "language_code": "ar_EG",
                "body_parameters": ["مريم", "ليزر", "15/10/2026", "18:00"],
                "button_payloads": [
                    f"tia.booking.confirm:{appointment_id}",
                    f"tia.booking.reschedule:{appointment_id}",
                ],
            }
        },
        attempt=1,
    )
    payload = build_meta_message_payload(item)
    assert payload["type"] == "template"
    components = payload["template"]["components"]
    button_components = [component for component in components if component["type"] == "button"]
    assert button_components == [
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": "0",
            "parameters": [{"type": "payload", "payload": f"tia.booking.confirm:{appointment_id}"}],
        },
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": "1",
            "parameters": [{"type": "payload", "payload": f"tia.booking.reschedule:{appointment_id}"}],
        },
    ]


def test_confirmation_ticket_reschedule_flow_has_server_owned_locks() -> None:
    interactions = (_root() / "app/services/whatsapp_interactions.py").read_text(encoding="utf-8")
    tools = (_root() / "app/agents/tools/clinic_tools.py").read_text(encoding="utf-8")
    assert '"locked_service_id": str(appointment.service_id)' in interactions
    assert '"locked_laser_device_key": appointment.laser_device_key' in interactions
    assert '"reschedule_origin": "confirmation_ticket"' in interactions
    assert 'service_id = str(ticket_state["locked_service_id"])' in tools
    assert 'target_device_key = ticket_state.get("locked_laser_device_key")' in tools
    assert 'laser_device_key=locked_device_key' in tools


def test_frontend_appointment_pending_label_is_booked() -> None:
    source = (_root().parent / "frontend/src/lib/status.ts").read_text(encoding="utf-8")
    appointment_block = source.split("export const appointmentLabels", 1)[1].split("};", 1)[0]
    presentation_block = source.split("const appointmentStatus", 1)[1].split("};", 1)[0]
    assert 'pending: "محجوز"' in appointment_block
    assert 'pending: status("محجوز"' in presentation_block


def test_reconciliation_migration_is_future_only_and_uses_branch_workspace_timezone() -> None:
    migration = (
        _root() / "alembic/versions/0092_appointment_confirmation_lifecycle.py"
    ).read_text(encoding="utf-8")
    assert "a.start_at > now()" in migration
    assert "COALESCE(NULLIF(b.timezone, ''), w.timezone)" in migration
    assert "SET status = 'pending'" in migration
    assert "SET status = 'confirmed'" in migration
    assert "Africa/Cairo" not in migration


def test_template_quick_reply_webhook_uses_structured_payload_not_visible_text() -> None:
    from app.services.meta_whatsapp_transport import _normalize_inbound

    appointment_id = uuid4()
    inbound = _normalize_inbound(
        {
            "metadata": {"phone_number_id": "123456"},
            "messages": [
                {
                    "id": "wamid.template-button",
                    "from": "201000000000",
                    "type": "button",
                    "button": {
                        "payload": f"tia.booking.confirm:{appointment_id}",
                        "text": "تأكيد الحضور",
                    },
                }
            ],
        }
    )
    assert len(inbound) == 1
    assert inbound[0].metadata["interactive_reply"] == {
        "type": "button_reply",
        "id": f"tia.booking.confirm:{appointment_id}",
        "title": "تأكيد الحضور",
    }
