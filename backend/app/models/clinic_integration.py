from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

CLINIC_INTEGRATION_MODES = (
    "tia_native",
    "external_api",
    "hybrid",
    "imported",
)
CLINIC_INTEGRATION_STATUSES = (
    "active",
    "setup_required",
    "paused",
    "error",
)
CLINIC_INTEGRATION_ENTITY_TYPES = (
    "service",
    "branch",
    "doctor",
    "patient",
    "appointment",
    "payment",
    "patient_package",
    "package_usage",
)


class ClinicIntegration(TimestampMixin, Base):
    """One clinic-system configuration per workspace.

    ``workspace_id`` is intentionally the primary key. Besides expressing the
    one-to-one relationship in PostgreSQL, it lets ``Session.get`` reuse the
    configuration from SQLAlchemy's identity map when the agent resolves the
    adapter multiple times during one request.
    """

    __tablename__ = "clinic_integrations"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('tia_native', 'external_api', 'hybrid', 'imported')",
            name="clinic_integration_mode_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'setup_required', 'paused', 'error')",
            name="clinic_integration_status_valid",
        ),
        Index("ix_clinic_integrations_adapter_status", "adapter_key", "status"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="tia_native", server_default="tia_native"
    )
    adapter_key: Mapped[str] = mapped_column(
        String(80), nullable=False, default="tia_database", server_default="tia_database"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", server_default="active"
    )
    external_clinic_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    config_json: Mapped[dict] = mapped_column(
        "config",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    authority_policy_json: Mapped[dict] = mapped_column(
        "authority_policy",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )


class ClinicIntegrationEntityLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Maps a Linka canonical entity id to the clinic source system's id."""

    __tablename__ = "clinic_integration_entity_links"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('service', 'branch', 'doctor', 'patient', 'appointment', 'payment', 'patient_package', 'package_usage')",
            name="clinic_integration_entity_link_type_valid",
        ),
        UniqueConstraint(
            "workspace_id",
            "entity_type",
            "canonical_id",
            name="uq_clinic_integration_entity_links_canonical",
        ),
        UniqueConstraint(
            "workspace_id",
            "entity_type",
            "external_id",
            name="uq_clinic_integration_entity_links_external",
        ),
        Index(
            "ix_clinic_integration_entity_links_workspace_type",
            "workspace_id",
            "entity_type",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("clinic_integrations.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
