from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Service(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_services_workspace_id_id"),
        UniqueConstraint("workspace_id", "slug", name="uq_services_workspace_slug"),
        CheckConstraint(
            "duration_minutes > 0 AND duration_minutes <= 1440", name="service_duration_valid"
        ),
        CheckConstraint("price_minor >= 0", name="service_price_non_negative"),
        CheckConstraint(
            "operational_category IN ('laser', 'dermatology', 'slimming')",
            name="service_operational_category_valid",
        ),
        CheckConstraint(
            "requires_laser_device = (operational_category = 'laser')",
            name="service_laser_requirement_matches_category",
        ),
        CheckConstraint("buffer_before_minutes >= 0", name="service_buffer_before_non_negative"),
        CheckConstraint("buffer_after_minutes >= 0", name="service_buffer_after_non_negative"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    operational_category: Mapped[str] = mapped_column(
        String(20), nullable=False, default="dermatology", server_default="dermatology"
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    buffer_before_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    buffer_after_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="EGP", server_default="EGP"
    )
    requires_medical_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    requires_laser_device: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
