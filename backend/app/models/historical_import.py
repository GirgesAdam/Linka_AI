from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ClinicHistoricalImportBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "clinic_historical_import_batches"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('append', 'replace_previous_imports')",
            name="clinic_historical_import_batch_mode_valid",
        ),
        CheckConstraint(
            "status IN ('preview_ready', 'importing', 'imported', 'failed')",
            name="clinic_historical_import_batch_status_valid",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_clinic_historical_import_batches_workspace_id_id"),
        Index("ix_clinic_historical_import_batches_workspace_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="preview_ready", server_default="preview_ready"
    )
    schema_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="tia_history_v1", server_default="tia_history_v1"
    )
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_json: Mapped[dict] = mapped_column(
        "summary", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    error_message: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClinicHistoricalImportRow(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "clinic_historical_import_rows"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('patient', 'appointment', 'payment', 'payment_allocation', 'package', 'holding')",
            name="clinic_historical_import_row_entity_type_valid",
        ),
        CheckConstraint(
            "row_status IN ('ready', 'rejected')",
            name="clinic_historical_import_row_status_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["clinic_historical_import_batches.workspace_id", "clinic_historical_import_batches.id"],
            ondelete="CASCADE",
            name="fk_clinic_historical_import_rows_batch",
        ),
        UniqueConstraint(
            "batch_id", "entity_type", "source_record_id",
            name="uq_clinic_historical_import_rows_batch_entity_source",
        ),
        Index("ix_clinic_historical_import_rows_batch_status", "batch_id", "row_status"),
        Index("ix_clinic_historical_import_rows_workspace_entity", "workspace_id", "entity_type"),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    batch_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_sheet: Mapped[str] = mapped_column(String(64), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_status: Mapped[str] = mapped_column(String(16), nullable=False)
    normalized_json: Mapped[dict] = mapped_column(
        "normalized", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    issue_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    issue_message: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ClinicHistoricalImportLink(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "clinic_historical_import_links"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('patient', 'appointment', 'payment', 'package', 'pulse_pack')",
            name="clinic_historical_import_link_entity_type_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["clinic_historical_import_batches.workspace_id", "clinic_historical_import_batches.id"],
            ondelete="CASCADE",
            name="fk_clinic_historical_import_links_batch",
        ),
        UniqueConstraint(
            "workspace_id", "entity_type", "source_record_id",
            name="uq_clinic_historical_import_links_source",
        ),
        Index("ix_clinic_historical_import_links_batch_entity", "batch_id", "entity_type"),
        Index("ix_clinic_historical_import_links_canonical", "workspace_id", "entity_type", "canonical_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    batch_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
