from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AvailabilityBlock(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A branch-wide interval where new standard bookings are not allowed."""

    __tablename__ = "availability_blocks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "branch_id"],
            ["branches.workspace_id", "branches.id"],
            ondelete="CASCADE",
            name="fk_availability_blocks_branch",
        ),
        CheckConstraint("end_at > start_at", name="availability_blocks_interval_valid"),
        Index(
            "ix_availability_blocks_workspace_branch_time",
            "workspace_id",
            "branch_id",
            "start_at",
            "end_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    branch_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
