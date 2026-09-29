from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ClinicKnowledgeEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Curated explanatory knowledge used by Linka after entity grounding.

    Operational facts such as prices, duration, availability and booking rules
    remain owned by their canonical tables and must never be sourced from here.
    """

    __tablename__ = "clinic_knowledge_entries"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('clinic', 'service', 'laser_device')",
            name="clinic_knowledge_scope_valid",
        ),
        CheckConstraint(
            "(scope_type = 'clinic' AND service_id IS NULL AND device_key IS NULL) OR "
            "(scope_type = 'service' AND service_id IS NOT NULL AND device_key IS NULL) OR "
            "(scope_type = 'laser_device' AND service_id IS NULL AND device_key IS NOT NULL)",
            name="clinic_knowledge_scope_fields_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "service_id"],
            ["services.workspace_id", "services.id"],
            name="fk_clinic_knowledge_entries_service",
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(24), index=True, nullable=False)
    service_id: Mapped[UUID | None] = mapped_column(index=True, nullable=True)
    device_key: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
