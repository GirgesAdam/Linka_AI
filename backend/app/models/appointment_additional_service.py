from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AppointmentAdditionalService(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Billing-only service added during a visit without extending its reserved time."""

    __tablename__ = "appointment_additional_services"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_appointment_additional_services_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "appointment_id",
            "service_id",
            name="uq_appointment_additional_services_appointment_service",
        ),
        CheckConstraint("unit_price_minor >= 0", name="appointment_additional_service_price_non_negative"),
        ForeignKeyConstraint(
            ["workspace_id", "laser_device_key"],
            ["clinic_laser_devices.workspace_id", "clinic_laser_devices.device_key"],
            ondelete="RESTRICT",
            name="fk_appointment_additional_services_laser_device",
        ),
        CheckConstraint(
            "billing_context IN ('standard', 'package_prepaid', 'pulse_prepaid')",
            name="appointment_additional_service_billing_context_valid",
        ),
        CheckConstraint(
            "laser_pulses_used IS NULL OR laser_pulses_used >= 0",
            name="appointment_additional_service_pulses_non_negative",
        ),
        CheckConstraint(
            "pulse_resolution IS NULL OR pulse_resolution IN ('balance', 'new_pack', 'overage')",
            name="appointment_additional_service_pulse_resolution_valid",
        ),
        CheckConstraint(
            "pulse_overage_unit_price_minor IS NULL OR pulse_overage_unit_price_minor > 0",
            name="appointment_additional_service_pulse_overage_price_positive",
        ),
        CheckConstraint(
            "pulse_overage_charge_minor >= 0",
            name="appointment_additional_service_pulse_overage_non_negative",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "appointment_id"],
            ["appointments.workspace_id", "appointments.id"],
            ondelete="CASCADE",
            name="fk_appointment_additional_services_appointment",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "service_id"],
            ["services.workspace_id", "services.id"],
            ondelete="RESTRICT",
            name="fk_appointment_additional_services_service",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "patient_package_id"],
            ["patient_packages.workspace_id", "patient_packages.id"],
            ondelete="RESTRICT",
            name="fk_appointment_additional_services_patient_package",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "pulse_resolution_pulse_pack_id"],
            ["patient_pulse_packs.workspace_id", "patient_pulse_packs.id"],
            ondelete="RESTRICT",
            name="fk_additional_service_pulse_resolution_pack",
        ),
        Index(
            "ix_appointment_additional_services_workspace_appointment",
            "workspace_id",
            "appointment_id",
        ),
        Index(
            "ix_additional_service_pulse_resolution_pack",
            "pulse_resolution_pulse_pack_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    appointment_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    service_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    patient_package_id: Mapped[UUID | None] = mapped_column(index=True, nullable=True)
    service_name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="EGP", server_default="EGP"
    )
    laser_device_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    laser_device_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    billing_context: Mapped[str] = mapped_column(
        String(24), nullable=False, default="standard", server_default="standard"
    )
    laser_pulses_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pulse_resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pulse_resolution_pulse_pack_id: Mapped[UUID | None] = mapped_column(nullable=True)
    pulse_overage_unit_price_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pulse_overage_charge_minor: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
