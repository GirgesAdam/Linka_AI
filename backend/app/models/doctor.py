from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Doctor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "doctors"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_doctors_workspace_id_id"),
        CheckConstraint(
            "doctor_type IN ('regular', 'visiting')",
            name="doctor_type_valid",
        ),
        UniqueConstraint("workspace_id", "staff_id", name="uq_doctors_workspace_staff"),
        ForeignKeyConstraint(
            ["workspace_id", "staff_id"],
            ["staff.workspace_id", "staff.id"],
            ondelete="CASCADE",
            name="fk_doctors_workspace_staff",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    staff_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    # Archival legacy discriminator. Never used by operational scheduling.
    doctor_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="regular", server_default="regular"
    )
    specialization: Mapped[str | None] = mapped_column(String(200), nullable=True)
    license_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bio: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    booking_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
