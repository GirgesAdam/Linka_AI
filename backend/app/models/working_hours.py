from __future__ import annotations

from datetime import datetime, time
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BranchWorkingHour(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "branch_working_hours"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "branch_id"],
            ["branches.workspace_id", "branches.id"],
            ondelete="CASCADE",
            name="fk_branch_working_hours_branch",
        ),
        UniqueConstraint(
            "workspace_id",
            "branch_id",
            "weekday",
            "start_time",
            "end_time",
            name="uq_branch_working_hours_interval",
        ),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="branch_working_hours_weekday_valid"),
        CheckConstraint("end_time > start_time", name="branch_working_hours_interval_valid"),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    branch_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time] = mapped_column(Time(), nullable=False)
    end_time: Mapped[time] = mapped_column(Time(), nullable=False)


class DoctorWorkingHour(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "doctor_working_hours"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "doctor_id", "branch_id"],
            [
                "doctor_branches.workspace_id",
                "doctor_branches.doctor_id",
                "doctor_branches.branch_id",
            ],
            ondelete="CASCADE",
            name="fk_doctor_working_hours_assignment",
        ),
        UniqueConstraint(
            "workspace_id",
            "doctor_id",
            "branch_id",
            "weekday",
            "start_time",
            "end_time",
            name="uq_doctor_working_hours_interval",
        ),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="doctor_working_hours_weekday_valid"),
        CheckConstraint("end_time > start_time", name="doctor_working_hours_interval_valid"),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    doctor_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    branch_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time] = mapped_column(Time(), nullable=False)
    end_time: Mapped[time] = mapped_column(Time(), nullable=False)


class DoctorAvailabilityWindow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Deprecated dated-window history; never used for current scheduling."""

    __tablename__ = "doctor_availability_windows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "doctor_id", "branch_id"],
            [
                "doctor_branches.workspace_id",
                "doctor_branches.doctor_id",
                "doctor_branches.branch_id",
            ],
            ondelete="CASCADE",
            name="fk_doctor_availability_windows_assignment",
        ),
        UniqueConstraint(
            "workspace_id",
            "doctor_id",
            "branch_id",
            "start_at",
            "end_at",
            name="uq_doctor_availability_windows_interval",
        ),
        CheckConstraint("end_at > start_at", name="doctor_availability_window_interval_valid"),
        Index(
            "ix_doctor_availability_windows_workspace_time",
            "workspace_id",
            "start_at",
            "end_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    doctor_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    branch_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
