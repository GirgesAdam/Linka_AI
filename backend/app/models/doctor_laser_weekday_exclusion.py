from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DoctorLaserWeekdayExclusion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Absent row means ON; stored rows deny one doctor/device on one branch weekday."""

    __tablename__ = "doctor_laser_device_weekday_exclusions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "doctor_id", "branch_id"],
            ["doctor_branches.workspace_id", "doctor_branches.doctor_id", "doctor_branches.branch_id"],
            ondelete="CASCADE",
            name="fk_doctor_laser_weekday_assignment",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "device_key"],
            ["clinic_laser_devices.workspace_id", "clinic_laser_devices.device_key"],
            ondelete="CASCADE",
            name="fk_doctor_laser_weekday_device",
        ),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_doctor_laser_weekday_valid"),
        UniqueConstraint(
            "workspace_id", "doctor_id", "branch_id", "weekday", "device_key",
            name="uq_doctor_laser_weekday_exclusion",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    doctor_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    branch_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    device_key: Mapped[str] = mapped_column(String(40), nullable=False)
