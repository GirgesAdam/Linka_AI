from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.tools.clinic_tools import AgentToolContext, build_clinic_tools
from app.models.appointment import Appointment
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.patient import Patient
from app.models.workspace import Workspace
from app.schemas.agent import AgentChatResponse
from app.services.appointment_confirmation import (
    can_customer_confirm_appointment,
    confirmation_timezone,
)
from app.services.appointment_operations import (
    AppointmentOperationError,
    confirm_appointment_operation,
)
from app.services.conversation_flows import start_flow
from app.services.conversation_ownership import OWNER_HUMAN, agent_can_reply
from app.services.handoffs import get_active_handoff

_CONFIRM_PREFIX = "tia.booking.confirm:"
_RESCHEDULE_PREFIX = "tia.booking.reschedule:"
_MAX_APPOINTMENT_REF_LENGTH = 180


@dataclass(frozen=True)
class WhatsAppBookingAction:
    action: str
    appointment_id: str


def parse_whatsapp_booking_action(metadata: object) -> WhatsAppBookingAction | None:
    """Parse only a structured Meta button reply; customer-visible text is never routing input."""
    if not isinstance(metadata, dict):
        return None
    reply = metadata.get("interactive_reply")
    if not isinstance(reply, dict) or reply.get("type") != "button_reply":
        return None
    raw_id = reply.get("id")
    if not isinstance(raw_id, str):
        return None
    raw_id = raw_id.strip()

    for prefix, action in (
        (_CONFIRM_PREFIX, "confirm"),
        (_RESCHEDULE_PREFIX, "reschedule"),
    ):
        if not raw_id.startswith(prefix):
            continue
        appointment_id = raw_id[len(prefix) :].strip()
        if not appointment_id or len(appointment_id) > _MAX_APPOINTMENT_REF_LENGTH:
            return None
        return WhatsAppBookingAction(action=action, appointment_id=appointment_id)
    return None


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _invoke_tool(ctx: AgentToolContext, tool_name: str, arguments: dict) -> dict | None:
    tool = next((item for item in build_clinic_tools(ctx) if item.name == tool_name), None)
    if tool is None:
        return None
    raw = tool.invoke(arguments)
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _existing_response(
    db: Session,
    *,
    conversation: Conversation,
    inbound: Message,
    run_id: UUID,
) -> AgentChatResponse | None:
    outbound = db.scalar(
        select(Message)
        .where(
            Message.workspace_id == conversation.workspace_id,
            Message.conversation_id == conversation.id,
            Message.sender_type == "ai",
            Message.direction == "outbound",
            Message.in_reply_to_message_id == inbound.id,
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    if outbound is None:
        return None
    metadata = outbound.metadata_json or {}
    return AgentChatResponse(
        run_id=_uuid(metadata.get("agent_run_id")) or run_id,
        conversation_id=conversation.id,
        inbound_message_id=inbound.id,
        outbound_message_id=outbound.id,
        reply=outbound.content,
        handoff_required=conversation.owner_type == OWNER_HUMAN,
        agent_paused=False,
        model=metadata.get("model"),
    )


def _persist_reply(
    db: Session,
    *,
    conversation: Conversation,
    inbound: Message,
    run_id: UUID,
    reply: str,
    action: str,
) -> AgentChatResponse:
    now = datetime.now(UTC)
    outbound = Message(
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        channel_connection_id=conversation.channel_connection_id,
        sender_type="ai",
        direction="outbound",
        in_reply_to_message_id=inbound.id,
        message_type="text",
        content=reply,
        delivery_status="queued",
        metadata_json={
            "agent_run_id": str(run_id),
            "model": "structured:whatsapp-button",
            "source": "whatsapp_interactive",
            "in_reply_to_message_id": str(inbound.id),
            "dispatch_required": True,
            "interactive_action": action,
        },
    )
    conversation.last_message_at = now
    db.add(outbound)
    db.commit()
    return AgentChatResponse(
        run_id=run_id,
        conversation_id=conversation.id,
        inbound_message_id=inbound.id,
        outbound_message_id=outbound.id,
        reply=reply,
        handoff_required=False,
        agent_paused=False,
        model="structured:whatsapp-button",
    )


def _action_appointment(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    appointment_ref: str,
) -> Appointment | None:
    appointment_id = _uuid(appointment_ref)
    if appointment_id is None:
        return None
    return db.scalar(
        select(Appointment).where(
            Appointment.id == appointment_id,
            Appointment.workspace_id == workspace_id,
            Appointment.patient_id == patient_id,
        )
    )


def _confirmation_reply(
    db: Session,
    *,
    workspace: Workspace,
    patient: Patient,
    appointment: Appointment,
) -> str:
    timezone = confirmation_timezone(
        db,
        workspace_id=workspace.id,
        branch_id=appointment.branch_id,
    )
    local_start = appointment.start_at.astimezone(timezone)
    customer_name = (patient.first_name or "").strip()
    greeting = f"تمام يا {customer_name} 💛" if customer_name else "تمام 💛"
    return (
        f"{greeting}\nأكدنا حضورك.\n"
        f"مستنيينك يوم {local_start.strftime('%d/%m/%Y')} "
        f"الساعة {local_start.strftime('%H:%M')}، وهتنورنا ✨"
    )


def process_whatsapp_booking_action(
    db: Session,
    *,
    workspace: Workspace,
    patient: Patient,
    conversation: Conversation,
    inbound: Message,
) -> AgentChatResponse | None:
    """Execute trusted appointment-specific WhatsApp quick replies only."""
    action = parse_whatsapp_booking_action(inbound.metadata_json or {})
    if action is None:
        return None
    if not agent_can_reply(conversation):
        return None
    if get_active_handoff(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
    ) is not None:
        return None

    inbound_metadata = dict(inbound.metadata_json or {})
    run_id = _uuid(inbound_metadata.get("agent_run_id")) or uuid4()
    inbound_metadata["agent_run_id"] = str(run_id)
    inbound.metadata_json = inbound_metadata

    recovered = _existing_response(
        db,
        conversation=conversation,
        inbound=inbound,
        run_id=run_id,
    )
    if recovered is not None:
        return recovered

    appointment = _action_appointment(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        appointment_ref=action.appointment_id,
    )
    now = datetime.now(UTC)
    if appointment is None:
        return _persist_reply(
            db,
            conversation=conversation,
            inbound=inbound,
            run_id=run_id,
            reply="مش لاقي الموعد ده ضمن مواعيدك الحالية. ممكن أراجعلك المواعيد القادمة.",
            action=action.action,
        )

    if action.action == "confirm":
        if appointment.status == "confirmed" and appointment.start_at > now:
            reply = "تمام 💛 موعدك متأكد بالفعل، ومستنيينك في ميعادك."
        elif appointment.status != "pending" or appointment.start_at <= now:
            reply = "الطلب ده لم يعد صالحًا للموعد الحالي. ممكن أراجعلك حالة موعدك الحالية."
        elif not can_customer_confirm_appointment(
            db,
            workspace_id=workspace.id,
            branch_id=appointment.branch_id,
            start_at=appointment.start_at,
            now=now,
        ):
            reply = "لسه بدري على تأكيد الحضور 💛 هنطلب منك التأكيد لما يقرب موعدك."
        else:
            try:
                appointment = confirm_appointment_operation(
                    db,
                    workspace_id=workspace.id,
                    appointment_id=appointment.id,
                    patient_id=patient.id,
                    changed_by_user_id=None,
                    reason="customer_confirmed_from_whatsapp_ticket",
                    actor_type="ai",
                    now=now,
                    enforce_customer_window=True,
                )
            except AppointmentOperationError:
                db.rollback()
                reply = "الطلب ده لم يعد صالحًا للموعد الحالي. ممكن أراجعلك حالة موعدك الحالية."
            else:
                reply = _confirmation_reply(
                    db,
                    workspace=workspace,
                    patient=patient,
                    appointment=appointment,
                )
        return _persist_reply(
            db,
            conversation=conversation,
            inbound=inbound,
            run_id=run_id,
            reply=reply,
            action="confirm",
        )

    if appointment.status not in {"pending", "confirmed"} or appointment.start_at <= now:
        return _persist_reply(
            db,
            conversation=conversation,
            inbound=inbound,
            run_id=run_id,
            reply="الطلب ده لم يعد صالحًا للموعد الحالي. ممكن أراجعلك المواعيد القادمة.",
            action="reschedule",
        )

    current = {
        "appointment_id": str(appointment.id),
        "branch_id": str(appointment.branch_id),
        "doctor_id": str(appointment.doctor_id),
        "service_id": str(appointment.service_id),
        "laser_device_key": appointment.laser_device_key,
        "start_at": appointment.start_at.isoformat(),
        "status": appointment.status,
    }
    start_flow(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        patient_id=patient.id,
        flow_type="appointment_reschedule",
        capabilities=["appointment_reschedule"],
        entity_state={
            "appointment_id": str(appointment.id),
            "locked_service_id": str(appointment.service_id),
            "locked_laser_device_key": appointment.laser_device_key,
            "current_appointment": current,
            "reschedule_origin": "confirmation_ticket",
        },
        missing_information=["requested_date"],
        last_decision={
            "source": "whatsapp_confirmation_ticket",
            "action": "reschedule",
            "appointment_id": str(appointment.id),
        },
        run_id=run_id,
    )
    return _persist_reply(
        db,
        conversation=conversation,
        inbound=inbound,
        run_id=run_id,
        reply="تمام، تحب تغيّر الموعد ليوم إيه ووقت كام؟",
        action="reschedule",
    )


def whatsapp_booking_dispatch_metadata(
    db: Session,
    *,
    message: Message,
) -> dict:
    """Do not attach immediate confirmation buttons to booking success replies."""
    del db
    return dict(message.metadata_json or {})
