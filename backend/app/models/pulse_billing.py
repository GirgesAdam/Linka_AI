from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

PULSE_PACK_STATUSES = ("active", "cancelled")
PULSE_USAGE_STATUSES = ("consumed", "reversed")
PULSE_SETTLEMENT_RESOLUTIONS = ("pending", "balance", "new_pack", "overage")
class PulseBillingSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pulse_billing_settings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "device_key",
            name="uq_pulse_billing_settings_workspace_device",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "device_key"],
            ["clinic_laser_devices.workspace_id", "clinic_laser_devices.device_key"],
            ondelete="RESTRICT",
            name="fk_pulse_billing_settings_clinic_device",
        ),
        CheckConstraint(
            "overage_price_minor > 0",
            name="pulse_billing_overage_price_positive",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    device_key: Mapped[str] = mapped_column(String(40), nullable=False)
    overage_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="EGP", server_default="EGP"
    )


class PulsePackOffer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pulse_pack_offers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_pulse_pack_offers_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "device_key",
            "pulses_count",
            name="uq_pulse_pack_offers_workspace_device_count",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "device_key"],
            ["clinic_laser_devices.workspace_id", "clinic_laser_devices.device_key"],
            ondelete="RESTRICT",
            name="fk_pulse_pack_offers_clinic_device",
        ),
        CheckConstraint("pulses_count > 0", name="pulse_pack_offer_count_positive"),
        CheckConstraint("price_minor >= 0", name="pulse_pack_offer_price_non_negative"),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_pulse_pack_offers_workspace",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    device_key: Mapped[str] = mapped_column(String(40), nullable=False)
    device_name: Mapped[str] = mapped_column(String(120), nullable=False)
    pulses_count: Mapped[int] = mapped_column(Integer, nullable=False)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="EGP", server_default="EGP"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class PatientPulsePack(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "patient_pulse_packs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_patient_pulse_packs_workspace_id_id"),
        CheckConstraint("pulses_purchased > 0", name="patient_pulse_pack_count_positive"),
        CheckConstraint("sale_price_minor >= 0", name="patient_pulse_pack_price_non_negative"),
        CheckConstraint(
            "standalone_pulse_price_minor_at_purchase IS NULL "
            "OR standalone_pulse_price_minor_at_purchase > 0",
            name="patient_pulse_pack_standalone_price_positive",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "device_key"],
            ["clinic_laser_devices.workspace_id", "clinic_laser_devices.device_key"],
            ondelete="RESTRICT",
            name="fk_patient_pulse_packs_clinic_device",
        ),
        CheckConstraint(
            "status IN ('active', 'cancelled')",
            name="patient_pulse_pack_status_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_patient_pulse_packs_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "patient_id"],
            ["patients.workspace_id", "patients.id"],
            ondelete="RESTRICT",
            name="fk_patient_pulse_packs_patient",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "pulse_pack_offer_id"],
            ["pulse_pack_offers.workspace_id", "pulse_pack_offers.id"],
            ondelete="RESTRICT",
            name="fk_patient_pulse_packs_offer",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "origin_appointment_id"],
            ["appointments.workspace_id", "appointments.id"],
            ondelete="RESTRICT",
            name="fk_patient_pulse_packs_origin_appointment",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "purchase_transaction_id"],
            ["payment_transactions.workspace_id", "payment_transactions.id"],
            ondelete="RESTRICT",
            name="fk_patient_pulse_packs_purchase_transaction",
        ),
        Index(
            "uq_patient_pulse_packs_workspace_idempotency_key",
            "workspace_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "ix_patient_pulse_packs_workspace_patient_device",
            "workspace_id",
            "patient_id",
            "device_key",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    patient_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    pulse_pack_offer_id: Mapped[UUID | None] = mapped_column(index=True, nullable=True)
    origin_appointment_id: Mapped[UUID | None] = mapped_column(index=True, nullable=True)
    purchase_transaction_id: Mapped[UUID | None] = mapped_column(index=True, nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    device_key: Mapped[str] = mapped_column(String(40), nullable=False)
    device_name: Mapped[str] = mapped_column(String(120), nullable=False)
    pulses_purchased: Mapped[int] = mapped_column(Integer, nullable=False)
    sale_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    standalone_pulse_price_minor_at_purchase: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="EGP", server_default="EGP"
    )
    purchased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)


class PulseUsage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pulse_usages"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_pulse_usages_workspace_id_id"),
        CheckConstraint("pulses_used > 0", name="pulse_usage_count_positive"),
        CheckConstraint(
            "status IN ('consumed', 'reversed')",
            name="pulse_usage_status_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_pulse_usages_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "patient_pulse_pack_id"],
            ["patient_pulse_packs.workspace_id", "patient_pulse_packs.id"],
            ondelete="RESTRICT",
            name="fk_pulse_usages_pack",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "appointment_id"],
            ["appointments.workspace_id", "appointments.id"],
            ondelete="RESTRICT",
            name="fk_pulse_usages_appointment",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "appointment_additional_service_id"],
            ["appointment_additional_services.workspace_id", "appointment_additional_services.id"],
            ondelete="RESTRICT",
            name="fk_pulse_usages_additional_service",
        ),
        Index(
            "ix_pulse_usages_workspace_pack_status",
            "workspace_id",
            "patient_pulse_pack_id",
            "status",
        ),
        Index(
            "ix_pulse_usages_workspace_appointment",
            "workspace_id",
            "appointment_id",
        ),
        Index(
            "ix_pulse_usages_workspace_additional_service",
            "workspace_id",
            "appointment_additional_service_id",
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    patient_pulse_pack_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    appointment_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    appointment_additional_service_id: Mapped[UUID | None] = mapped_column(nullable=True)
    pulses_used: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="consumed", server_default="consumed"
    )
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AppointmentPulseSettlement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "appointment_pulse_settlements"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "appointment_id",
            name="uq_appointment_pulse_settlements_workspace_appointment",
        ),
        CheckConstraint("pulses_used >= 0", name="pulse_settlement_used_non_negative"),
        CheckConstraint(
            "pulses_from_balance >= 0",
            name="pulse_settlement_balance_non_negative",
        ),
        CheckConstraint(
            "deficit_pulses >= 0",
            name="pulse_settlement_deficit_non_negative",
        ),
        CheckConstraint(
            "overage_unit_price_minor IS NULL OR overage_unit_price_minor > 0",
            name="pulse_settlement_overage_price_positive",
        ),
        CheckConstraint(
            "overage_charge_minor >= 0",
            name="pulse_settlement_overage_charge_non_negative",
        ),
        CheckConstraint(
            "resolution IN ('pending', 'balance', 'new_pack', 'overage')",
            name="pulse_settlement_resolution_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_appointment_pulse_settlements_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "appointment_id"],
            ["appointments.workspace_id", "appointments.id"],
            ondelete="CASCADE",
            name="fk_appointment_pulse_settlements_appointment",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "resolution_pulse_pack_id"],
            ["patient_pulse_packs.workspace_id", "patient_pulse_packs.id"],
            ondelete="RESTRICT",
            name="fk_appointment_pulse_settlements_resolution_pack",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    appointment_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    pulses_used: Mapped[int] = mapped_column(Integer, nullable=False)
    pulses_from_balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deficit_pulses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolution: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    resolution_pulse_pack_id: Mapped[UUID | None] = mapped_column(index=True, nullable=True)
    overage_unit_price_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overage_charge_minor: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
