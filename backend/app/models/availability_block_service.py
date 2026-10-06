from __future__ import annotations

from uuid import UUID

from sqlalchemy import ForeignKeyConstraint, Index, PrimaryKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class AvailabilityBlockService(Base):
    __tablename__ = "availability_block_services"
    __table_args__ = (
        PrimaryKeyConstraint(
            "availability_block_id", "service_id", name="pk_availability_block_services"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "availability_block_id"],
            ["availability_blocks.workspace_id", "availability_blocks.id"],
            ondelete="CASCADE",
            name="fk_availability_block_services_block",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "service_id"],
            ["services.workspace_id", "services.id"],
            ondelete="CASCADE",
            name="fk_availability_block_services_service",
        ),
        Index("ix_availability_block_services_workspace_service", "workspace_id", "service_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    availability_block_id: Mapped[UUID] = mapped_column(nullable=False)
    service_id: Mapped[UUID] = mapped_column(nullable=False)
