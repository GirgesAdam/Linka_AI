from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

PATIENT_STATUSES = ("active", "inactive", "blocked")
PATIENT_SOURCES = (
    "whatsapp",
    "instagram",
    "facebook",
    "website",
    "referral",
    "walk_in",
    "campaign",
    "phone",
    "email",
    "other",
)


class Patient(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "patients"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_patients_workspace_id_id"),
        CheckConstraint(
            "status IN ('active', 'inactive', 'blocked')",
            name="patient_status_valid",
        ),
        CheckConstraint(
            "source IN ('whatsapp', 'instagram', 'facebook', 'website', "
            "'referral', 'walk_in', 'campaign', 'phone', 'email', 'other')",
            name="patient_source_valid",
        ),
        Index(
            "uq_patients_workspace_phone_normalized",
            "workspace_id",
            "phone_normalized",
            unique=True,
            postgresql_where=text("phone_normalized IS NOT NULL"),
        ),
        Index("ix_patients_workspace_status", "workspace_id", "status"),
        Index("ix_patients_workspace_source", "workspace_id", "source"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    first_name: Mapped[str] = mapped_column(String(120), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    phone_normalized: Mapped[str | None] = mapped_column(String(40), nullable=True)
    customer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(32), nullable=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    preferred_language: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="ar",
        server_default="ar",
    )
    preferred_branch_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="other",
        server_default="other",
    )
    source_detail: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
        server_default="active",
    )
    marketing_consent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    marketing_consent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    whatsapp_opt_in: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    whatsapp_opt_in_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    whatsapp_opt_in_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    last_contact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    __table_args__ = __table_args__ + (
        ForeignKeyConstraint(
            ["workspace_id", "preferred_branch_id"],
            ["branches.workspace_id", "branches.id"],
            name="fk_patients_preferred_branch",
        ),
    )
