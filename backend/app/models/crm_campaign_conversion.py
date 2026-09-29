from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

CRM_CAMPAIGN_ATTRIBUTION_KINDS = ("direct_same_conversation_response",)


class CRMCampaignConversion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Explicit, auditable campaign-to-booking attribution.

    A row is created only when Linka books an appointment after a patient reply in
    the same conversation as a previously sent CRM campaign message. We never
    infer conversions later from patient/date proximity alone.
    """

    __tablename__ = "crm_campaign_conversions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_crm_campaign_conversions_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "appointment_id",
            name="uq_crm_campaign_conversions_workspace_appointment",
        ),
        CheckConstraint(
            "attribution_kind IN ('direct_same_conversation_response')",
            name="crm_campaign_conversion_attribution_kind_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "campaign_id"],
            ["crm_campaigns.workspace_id", "crm_campaigns.id"],
            ondelete="CASCADE",
            name="fk_crm_campaign_conversions_campaign",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "recipient_id"],
            ["crm_campaign_recipients.workspace_id", "crm_campaign_recipients.id"],
            ondelete="CASCADE",
            name="fk_crm_campaign_conversions_recipient",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "patient_id"],
            ["patients.workspace_id", "patients.id"],
            ondelete="RESTRICT",
            name="fk_crm_campaign_conversions_patient",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversations.workspace_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_crm_campaign_conversions_conversation",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "original_appointment_id"],
            ["appointments.workspace_id", "appointments.id"],
            ondelete="RESTRICT",
            name="fk_crm_campaign_conversions_original_appointment",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "appointment_id"],
            ["appointments.workspace_id", "appointments.id"],
            ondelete="RESTRICT",
            name="fk_crm_campaign_conversions_current_appointment",
        ),
        Index("ix_crm_campaign_conversions_campaign_booked", "workspace_id", "campaign_id", "booked_at"),
        Index("ix_crm_campaign_conversions_recipient", "workspace_id", "recipient_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False)
    campaign_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    recipient_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    patient_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    original_appointment_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    appointment_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    response_message_id: Mapped[UUID] = mapped_column(ForeignKey("messages.id", ondelete="RESTRICT"), index=True, nullable=False)
    attribution_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    campaign_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    patient_replied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    booked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
