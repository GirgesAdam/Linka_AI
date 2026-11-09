from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies.page_permissions import (
    require_page_permission,
)
from app.api.dependencies.security import WorkspaceAccess
from app.database.session import get_db
from app.models.channel_connection import ChannelConnection
from app.models.channel_identity import ChannelIdentity
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.models.patient import Patient
from app.schemas.inbox import InboxMessageRead
from app.services.activity import record_activity_event
from app.services.channels import queue_message_dispatch
from app.services.conversation_ownership import mark_conversation_read, record_outbound_activity
from app.services.handoffs import (
    HandoffStateError,
    add_handoff_event,
    claim_handoff,
    create_handoff,
    get_active_handoff,
)
from app.services.staff_whatsapp_followup import (
    STAFF_FOLLOWUP_TEMPLATE_LANGUAGE,
    STAFF_FOLLOWUP_TEMPLATE_NAME,
    ensure_staff_followup_template,
    render_staff_followup_message,
)

router = APIRouter()


class StaffWhatsAppFollowupResponse(BaseModel):
    status: Literal["queued", "template_pending", "unavailable"]
    conversation_id: UUID
    detail: str | None = None
    message: InboxMessageRead | None = None
    dispatch_id: UUID | None = None


def _unavailable(conversation_id: UUID, detail: str) -> StaffWhatsAppFollowupResponse:
    return StaffWhatsAppFollowupResponse(
        status="unavailable",
        conversation_id=conversation_id,
        detail=detail,
    )


@router.post(
    "/conversations/{conversation_id}/whatsapp-followup",
    response_model=StaffWhatsAppFollowupResponse,
)
def send_staff_whatsapp_followup(
    conversation_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("inbox.detail", legacy_access="reader"))],
    db: Annotated[Session, Depends(get_db)],
) -> StaffWhatsAppFollowupResponse:
    conversation = db.scalar(
        select(Conversation)
        .where(
            Conversation.workspace_id == access.workspace.id,
            Conversation.id == conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    if conversation.channel != "whatsapp" or conversation.channel_connection_id is None:
        return _unavailable(conversation.id, "This conversation is not connected to WhatsApp.")

    patient = db.scalar(
        select(Patient).where(
            Patient.workspace_id == access.workspace.id,
            Patient.id == conversation.patient_id,
        )
    )
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found.")
    if patient.status != "active":
        return _unavailable(conversation.id, "The patient is not active.")
    if not patient.whatsapp_opt_in:
        return _unavailable(
            conversation.id,
            "WhatsApp opt-in is required before starting a proactive follow-up.",
        )

    connection = db.scalar(
        select(ChannelConnection).where(
            ChannelConnection.workspace_id == access.workspace.id,
            ChannelConnection.id == conversation.channel_connection_id,
        )
    )
    if connection is None or connection.status != "active":
        return _unavailable(conversation.id, "The WhatsApp connection is not active.")
    if connection.provider != "meta_cloud":
        return _unavailable(conversation.id, "This WhatsApp route does not use Meta Cloud API.")

    identity = db.scalar(
        select(ChannelIdentity).where(
            ChannelIdentity.workspace_id == access.workspace.id,
            ChannelIdentity.channel_connection_id == connection.id,
            ChannelIdentity.patient_id == patient.id,
        )
    )
    if identity is None:
        return _unavailable(
            conversation.id,
            "No verified WhatsApp identity exists for this patient on the current connection.",
        )

    template_status, template_error = ensure_staff_followup_template(
        db,
        connection=connection,
    )
    if template_status != "approved":
        # Persist a newly requested/pending template status without changing the
        # clinic's normal onboarding/automation readiness contract.
        db.commit()
        if template_status == "pending":
            return StaffWhatsAppFollowupResponse(
                status="template_pending",
                conversation_id=conversation.id,
                detail="The staff follow-up template is waiting for Meta approval.",
            )
        return _unavailable(
            conversation.id,
            template_error
            or "The approved staff follow-up template is not available on this WhatsApp account.",
        )

    now = datetime.now(UTC)
    rendered = render_staff_followup_message(
        patient_first_name=(patient.first_name or "العميل")[:256],
        clinic_name=(access.workspace.name or "العيادة")[:256],
    )

    # Protect against accidental double-submit while keeping the endpoint simple.
    recent_message = db.scalar(
        select(Message)
        .where(
            Message.workspace_id == access.workspace.id,
            Message.conversation_id == conversation.id,
            Message.sender_type == "staff",
            Message.direction == "outbound",
            Message.message_type == "template",
            Message.content == rendered,
            Message.delivery_status.in_(("queued", "sent", "delivered", "read")),
            Message.created_at >= now - timedelta(minutes=2),
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    if recent_message is not None:
        existing_dispatch = db.scalar(
            select(MessageDispatch).where(
                MessageDispatch.workspace_id == access.workspace.id,
                MessageDispatch.message_id == recent_message.id,
            )
        )
        return StaffWhatsAppFollowupResponse(
            status="queued",
            conversation_id=conversation.id,
            message=InboxMessageRead.model_validate(recent_message),
            dispatch_id=existing_dispatch.id if existing_dispatch else None,
        )

    handoff = get_active_handoff(
        db,
        workspace_id=access.workspace.id,
        conversation_id=conversation.id,
        for_update=True,
    )
    try:
        if handoff is None:
            handoff = create_handoff(
                db,
                workspace_id=access.workspace.id,
                conversation=conversation,
                patient=patient,
                reason="Manual WhatsApp follow-up from Team Inbox.",
                category="customer_request",
                priority="normal",
                source="staff",
                created_by_user_id=access.user.id,
                commit=False,
            )
        handoff = claim_handoff(
            db,
            handoff=handoff,
            conversation=conversation,
            user=access.user,
            commit=False,
        )
    except HandoffStateError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    message = Message(
        workspace_id=access.workspace.id,
        conversation_id=conversation.id,
        channel_connection_id=connection.id,
        sender_type="staff",
        direction="outbound",
        message_type="template",
        content=rendered,
        delivery_status="queued",
        sent_by_user_id=access.user.id,
        metadata_json={
            "source": "staff_whatsapp_followup",
            "dispatch_required": True,
            "handoff_request_id": str(handoff.id),
            "whatsapp_template": {
                "name": STAFF_FOLLOWUP_TEMPLATE_NAME,
                "language_code": STAFF_FOLLOWUP_TEMPLATE_LANGUAGE,
                "body_parameters": [
                    (patient.first_name or "العميل")[:256],
                    (access.workspace.name or "العيادة")[:256],
                ],
            },
        },
    )
    db.add(message)
    db.flush()
    dispatch = queue_message_dispatch(
        db,
        message=message,
        conversation=conversation,
        commit=False,
    )
    if dispatch is None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The conversation does not have an active provider route.",
        )

    mark_conversation_read(conversation)
    record_outbound_activity(conversation, now=now)
    add_handoff_event(
        db,
        handoff=handoff,
        event_type="staff_replied",
        actor_type="staff",
        actor_user_id=access.user.id,
        metadata={
            "message_id": str(message.id),
            "delivery_status": "queued",
            "message_type": "template",
            "source": "staff_whatsapp_followup",
        },
    )
    record_activity_event(
        db,
        workspace_id=access.workspace.id,
        actor_type="staff",
        actor_user_id=access.user.id,
        action="handoff.staff_replied",
        entity_type="handoff",
        entity_id=handoff.id,
        summary="Staff sent approved WhatsApp follow-up template",
        metadata={
            "conversation_id": conversation.id,
            "delivery_status": "queued",
            "message_type": "template",
        },
    )
    db.commit()
    db.refresh(message)
    db.refresh(dispatch)

    return StaffWhatsAppFollowupResponse(
        status="queued",
        conversation_id=conversation.id,
        message=InboxMessageRead.model_validate(message),
        dispatch_id=dispatch.id,
    )
