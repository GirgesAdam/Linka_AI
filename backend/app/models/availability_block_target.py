from __future__ import annotations

from uuid import UUID

from sqlalchemy import ForeignKeyConstraint, Index, PrimaryKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class AvailabilityBlockTarget(Base):
    """Canonical schedule-resource target for a new availability block."""

    __tablename__ = "availability_block_targets"
    __table_args__ = (
        PrimaryKeyConstraint(
            "availability_block_id", "target_key", name="pk_availability_block_targets"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "availability_block_id"],
            ["availability_blocks.workspace_id", "availability_blocks.id"],
            ondelete="CASCADE",
            name="fk_availability_block_targets_block",
        ),
        Index(
            "ix_availability_block_targets_workspace_target",
            "workspace_id",
            "target_key",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    availability_block_id: Mapped[UUID] = mapped_column(nullable=False)
    target_key: Mapped[str] = mapped_column(String(80), nullable=False)
