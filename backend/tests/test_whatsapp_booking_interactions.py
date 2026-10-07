from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.services.whatsapp_interactions import (
    parse_whatsapp_booking_action,
    whatsapp_booking_dispatch_metadata,
)


def test_plain_customer_text_is_not_treated_as_a_button_action() -> None:
    assert parse_whatsapp_booking_action({"text": "تأكيد الحجز"}) is None
    assert parse_whatsapp_booking_action({"interactive_reply": {"title": "تغيير الميعاد"}}) is None


def test_structured_button_id_selects_exact_booking_action() -> None:
    appointment_id = "BK-991"
    action = parse_whatsapp_booking_action(
        {
            "interactive_reply": {
                "type": "button_reply",
                "id": f"tia.booking.reschedule:{appointment_id}",
                "title": "تغيير الميعاد",
            }
        }
    )
    assert action is not None
    assert action.action == "reschedule"
    assert action.appointment_id == appointment_id


def _booking_message(run_id):
    return SimpleNamespace(
        workspace_id=uuid4(),
        conversation_id=uuid4(),
        sender_type="ai",
        metadata_json={"agent_run_id": str(run_id)},
    )


def test_pending_booking_dispatch_does_not_get_immediate_confirm_button() -> None:
    run_id = uuid4()
    appointment_id = str(uuid4())
    db = SimpleNamespace(
        scalar=lambda _statement: SimpleNamespace(
            output_json={
                "ok": True,
                "appointment": {
                    "appointment_id": appointment_id,
                    "status": "pending",
                },
            }
        )
    )

    metadata = whatsapp_booking_dispatch_metadata(db, message=_booking_message(run_id))
    assert "whatsapp_interactive" not in metadata


def test_confirmed_booking_dispatch_uses_plain_text_without_buttons() -> None:
    run_id = uuid4()
    appointment_id = str(uuid4())
    db = SimpleNamespace(
        scalar=lambda _statement: SimpleNamespace(
            output_json={
                "ok": True,
                "appointment": {
                    "appointment_id": appointment_id,
                    "status": "confirmed",
                },
            }
        )
    )

    metadata = whatsapp_booking_dispatch_metadata(db, message=_booking_message(run_id))

    assert "whatsapp_interactive" not in metadata


def test_reschedule_discovery_is_pinned_to_structured_appointment_id() -> None:
    backend = Path(__file__).resolve().parent.parent
    agent_chat = (backend / "app/services/agent_chat.py").read_text(encoding="utf-8")
    tools = (backend / "app/agents/tools/clinic_tools.py").read_text(encoding="utf-8")

    assert 'appointment_id = text_value("appointment_id")' in agent_chat
    assert '"appointment_id": appointment_id' in agent_chat
    assert 'appointment_id: str = ""' in tools
    assert 'appointment.appointment_id == appointment_id' in tools


def test_completed_booking_uses_grounded_language_layer_with_deterministic_fallback() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "app/services/agent_chat.py").read_text(encoding="utf-8")

    assert '"flow-interpreter:verified-booking"' in source
    assert "compose_grounded_customer_reply(" in source
    assert "_package_booking_success_reply(appointment, package_result)" in source


def test_native_whatsapp_transport_supports_structured_reply_buttons() -> None:
    from app.schemas.channel import DispatchClaimItem
    from app.services.meta_whatsapp_transport import _normalize_inbound, build_meta_message_payload

    inbound = _normalize_inbound(
        {
            "metadata": {"phone_number_id": "123456789"},
            "messages": [
                {
                    "id": "wamid.button",
                    "from": "201001112223",
                    "type": "interactive",
                    "interactive": {
                        "type": "button_reply",
                        "button_reply": {
                            "id": "tia.booking.confirm:abc",
                            "title": "تأكيد الحجز",
                        },
                    },
                }
            ],
        }
    )
    assert len(inbound) == 1
    assert inbound[0].metadata["interactive_reply"] == {
        "type": "button_reply",
        "id": "tia.booking.confirm:abc",
        "title": "تأكيد الحجز",
    }

    outbound = DispatchClaimItem(
        dispatch_id=uuid4(),
        message_id=uuid4(),
        channel="whatsapp",
        provider="meta_cloud",
        external_account_id="123456789",
        external_user_id="201001112223",
        external_conversation_id="201001112223",
        message_type="text",
        content="تم الحجز",
        metadata={
            "whatsapp_interactive": {
                "type": "button",
                "buttons": [
                    {"id": "tia.booking.confirm:abc", "title": "تأكيد الحجز"}
                ],
            }
        },
        attempt=1,
    )
    payload = build_meta_message_payload(outbound)
    assert payload["type"] == "interactive"
    assert payload["interactive"]["action"]["buttons"][0]["reply"]["id"] == "tia.booking.confirm:abc"
