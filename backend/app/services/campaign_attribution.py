from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.crm_campaign import CRMCampaign, CRMCampaignRecipient
from app.models.crm_campaign_conversion import CRMCampaignConversion
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch

CAMPAIGN_DIRECT_RESPONSE_WINDOW_DAYS = 30
_TRACKABLE_DISPATCH_STATUSES = ("sent", "delivered", "read")


def record_direct_campaign_booking_conversion(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    conversation_id: UUID,
    appointment_id: UUID,
    booked_at: datetime | None = None,
) -> CRMCampaignConversion | None:
    """Record a direct-response campaign booking when the evidence is explicit.

    Attribution requires all of the following:
    - the appointment exists locally and belongs to the same patient/workspace;
    - a confirmed campaign recipient for that patient used the same conversation;
    - its dispatch has a real ``sent_at`` timestamp within the bounded window;
    - the patient sent an inbound message after that campaign send and before the
      booking was created.

    This intentionally does *not* attribute by name, patient/date proximity, or
    "booked within N days" alone.
    """
    existing = db.scalar(
        select(CRMCampaignConversion).where(
            CRMCampaignConversion.workspace_id == workspace_id,
            CRMCampaignConversion.appointment_id == appointment_id,
        )
    )
    if existing is not None:
        return existing

    booking_time = booked_at or datetime.now(UTC)
    if booking_time.tzinfo is None or booking_time.utcoffset() is None:
        booking_time = booking_time.replace(tzinfo=UTC)
    cutoff = booking_time - timedelta(days=CAMPAIGN_DIRECT_RESPONSE_WINDOW_DAYS)

    candidates = db.execute(
        select(CRMCampaignRecipient, CRMCampaign, MessageDispatch)
        .join(
            CRMCampaign,
            (CRMCampaign.workspace_id == CRMCampaignRecipient.workspace_id)
            & (CRMCampaign.id == CRMCampaignRecipient.campaign_id),
        )
        .join(
            MessageDispatch,
            (MessageDispatch.workspace_id == CRMCampaignRecipient.workspace_id)
            & (MessageDispatch.id == CRMCampaignRecipient.dispatch_id),
        )
        .where(
            CRMCampaignRecipient.workspace_id == workspace_id,
            CRMCampaignRecipient.patient_id == patient_id,
            CRMCampaignRecipient.conversation_id == conversation_id,
            CRMCampaign.status.in_(("confirmed", "cancelled")),
            MessageDispatch.status.in_(_TRACKABLE_DISPATCH_STATUSES),
            MessageDispatch.sent_at.is_not(None),
            MessageDispatch.sent_at >= cutoff,
            MessageDispatch.sent_at <= booking_time,
        )
        .order_by(MessageDispatch.sent_at.desc(), CRMCampaignRecipient.id.desc())
        .limit(20)
    ).all()

    for recipient, campaign, dispatch in candidates:
        assert dispatch.sent_at is not None
        response = db.execute(
            select(Message.id, Message.created_at)
            .where(
                Message.workspace_id == workspace_id,
                Message.conversation_id == conversation_id,
                Message.direction == "inbound",
                Message.created_at >= dispatch.sent_at,
                Message.created_at <= booking_time,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        ).first()
        if response is None:
            continue

        appointment_row = db.execute(
            select(Appointment.id, Appointment.created_at).where(
                Appointment.workspace_id == workspace_id,
                Appointment.id == appointment_id,
                Appointment.patient_id == patient_id,
            )
        ).first()
        if appointment_row is None:
            return None
        if booked_at is None and appointment_row.created_at is not None:
            booking_time = appointment_row.created_at
            if booking_time.tzinfo is None or booking_time.utcoffset() is None:
                booking_time = booking_time.replace(tzinfo=UTC)

        conversion = CRMCampaignConversion(
            workspace_id=workspace_id,
            campaign_id=campaign.id,
            recipient_id=recipient.id,
            patient_id=patient_id,
            conversation_id=conversation_id,
            original_appointment_id=appointment_id,
            appointment_id=appointment_id,
            response_message_id=response.id,
            attribution_kind="direct_same_conversation_response",
            campaign_sent_at=dispatch.sent_at,
            patient_replied_at=response.created_at,
            booked_at=booking_time,
        )
        db.add(conversion)
        db.flush()
        return conversion

    return None


def transfer_campaign_booking_conversion(
    db: Session,
    *,
    workspace_id: UUID,
    from_appointment_id: UUID,
    to_appointment_id: UUID,
) -> CRMCampaignConversion | None:
    """Follow a tracked booking across Linka's replacement-row reschedule model."""
    conversion = db.scalar(
        select(CRMCampaignConversion).where(
            CRMCampaignConversion.workspace_id == workspace_id,
            CRMCampaignConversion.appointment_id == from_appointment_id,
        )
    )
    if conversion is None:
        return None
    conversion.appointment_id = to_appointment_id
    db.flush()
    return conversion
