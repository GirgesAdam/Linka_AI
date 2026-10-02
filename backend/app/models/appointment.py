from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

APPOINTMENT_STATUSES = (
    "pending",
    "confirmed",
    "checked_in",
    "in_progress",
    "completed",
    "cancelled",
    "no_show",
    "rescheduled",
)

ACTIVE_APPOINTMENT_STATUSES = (
    "pending",
    "confirmed",
    "checked_in",
    "in_progress",
)

APPOINTMENT_SOURCES = (
    "ai",
    "staff",
    "whatsapp",
    "instagram",
    "website",
    "phone",
    "walk_in",
    "facebook",
    "email",
    "other",
)


class Appointment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_appointments_workspace_id_id"),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'checked_in', 'in_progress', "
            "'completed', 'cancelled', 'no_show', 'rescheduled')",
            name="appointment_status_valid",
        ),
        CheckConstraint(
            "source IN ('ai', 'staff', 'whatsapp', 'instagram', 'website', "
            "'phone', 'walk_in', 'facebook', 'email', 'other')",
            name="appointment_source_valid",
        ),
        CheckConstraint("end_at > start_at", name="appointment_interval_valid"),
        CheckConstraint("busy_end_at > busy_start_at", name="appointment_busy_interval_valid"),
        CheckConstraint("busy_start_at <= start_at", name="appointment_busy_start_valid"),
        CheckConstraint("busy_end_at >= end_at", name="appointment_busy_end_valid"),
        CheckConstraint("duration_minutes > 0", name="appointment_duration_positive"),
        CheckConstraint("price_minor >= 0", name="appointment_price_non_negative"),
        CheckConstraint("discount_minor >= 0", name="appointment_discount_non_negative"),
        CheckConstraint(
            "payment_status IN ('unknown', 'unpaid', 'partial', 'paid', 'refunded')",
            name="appointment_payment_status_valid",
        ),
        CheckConstraint(
            "payment_method IN ('unknown', 'cash', 'visa', 'instapay', 'card', 'bank_transfer', 'wallet', 'other')",
            name="appointment_payment_method_valid",
        ),
        CheckConstraint(
            "amount_paid_minor IS NULL OR amount_paid_minor >= 0",
            name="appointment_amount_paid_non_negative",
        ),
        CheckConstraint(
            "laser_pulses_used IS NULL OR laser_pulses_used >= 0",
            name="appointment_laser_pulses_non_negative",
        ),
        CheckConstraint(
            "NOT is_quick_booking OR (source = 'staff' AND created_by_user_id IS NOT NULL)",
            name="appointment_quick_booking_staff_only",
        ),
        CheckConstraint(
            "billing_context IN ('standard', 'package_prepaid', 'pulse_prepaid')",
            name="appointment_billing_context_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "laser_device_key"],
            ["clinic_laser_devices.workspace_id", "clinic_laser_devices.device_key"],
            ondelete="RESTRICT",
            name="fk_appointments_laser_device",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_appointments_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "patient_id"],
            ["patients.workspace_id", "patients.id"],
            ondelete="RESTRICT",
            name="fk_appointments_patient",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "branch_id"],
            ["branches.workspace_id", "branches.id"],
            ondelete="RESTRICT",
            name="fk_appointments_branch",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "doctor_id"],
            ["doctors.workspace_id", "doctors.id"],
            ondelete="RESTRICT",
            name="fk_appointments_doctor",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "service_id"],
            ["services.workspace_id", "services.id"],
            ondelete="RESTRICT",
            name="fk_appointments_service",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "patient_package_id"],
            ["patient_packages.workspace_id", "patient_packages.id"],
            ondelete="RESTRICT",
            name="fk_appointments_patient_package",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "lead_id"],
            ["leads.workspace_id", "leads.id"],
            ondelete="RESTRICT",
            name="fk_appointments_lead",
        ),
        ExcludeConstraint(
            ("workspace_id", "="),
            ("doctor_id", "="),
            (func.tstzrange(text("busy_start_at"), text("busy_end_at"), "[)"), "&&"),
            where=text("doctor_assignment_known AND NOT is_quick_booking AND status IN ('pending', 'confirmed', 'checked_in', 'in_progress')"),
            using="gist",
            name="excl_appointments_doctor_busy_time",
        ),
        ExcludeConstraint(
            ("workspace_id", "="),
            ("laser_device_key", "="),
            (func.tstzrange(text("busy_start_at"), text("busy_end_at"), "[)"), "&&"),
            where=text("laser_device_key IS NOT NULL AND NOT is_quick_booking AND status IN ('pending', 'confirmed', 'checked_in', 'in_progress')"),
            using="gist",
            name="excl_appointments_laser_device_busy_time",
        ),
        Index("ix_appointments_workspace_start", "workspace_id", "start_at"),
        Index("ix_appointments_patient_start", "patient_id", "start_at"),
        Index("ix_appointments_doctor_start", "doctor_id", "start_at"),
        Index("ix_appointments_branch_start", "branch_id", "start_at"),
        Index("ix_appointments_workspace_status", "workspace_id", "status"),
        Index(
            "ix_appointments_workspace_status_start_patient",
            "workspace_id",
            "status",
            "start_at",
            "patient_id",
        ),
        Index(
            "uq_appointments_workspace_idempotency_key",
            "workspace_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    patient_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    branch_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    doctor_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    doctor_assignment_known: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_quick_booking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    service_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    patient_package_id: Mapped[UUID | None] = mapped_column(index=True, nullable=True)
    # Multiple service appointments that form one customer-visible visit share this id.
    visit_group_id: Mapped[UUID | None] = mapped_column(nullable=True)
    lead_id: Mapped[UUID | None] = mapped_column(index=True, nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    rescheduled_from_appointment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="staff",
        server_default="staff",
    )

    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    busy_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    busy_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    discount_minor: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="EGP", server_default="EGP"
    )
    laser_device_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    laser_device_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    laser_pulses_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    amount_paid_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown", server_default="unknown"
    )
    # ``package_prepaid`` means the appointment is settled by a package purchase
    # recorded elsewhere. It must not create appointment-level revenue.
    billing_context: Mapped[str] = mapped_column(
        String(24), nullable=False, default="standard", server_default="standard"
    )
    package_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    customer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    no_show_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
