from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ClinicLaserDevice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "clinic_laser_devices"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_clinic_laser_devices_workspace_id_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "device_key",
            name="uq_clinic_laser_devices_workspace_device_key",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_clinic_laser_devices_workspace",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    device_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )


class ServiceDevicePrice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "service_device_prices"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_service_device_prices_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "service_id",
            "device_key",
            name="uq_service_device_prices_workspace_service_device",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "device_key"],
            ["clinic_laser_devices.workspace_id", "clinic_laser_devices.device_key"],
            ondelete="RESTRICT",
            name="fk_service_device_prices_clinic_device",
        ),
        CheckConstraint(
            "price_minor IS NULL OR price_minor >= 0",
            name="service_device_price_non_negative",
        ),
        CheckConstraint(
            "duration_minutes > 0 AND duration_minutes <= 1440",
            name="service_device_price_duration_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "service_id"],
            ["services.workspace_id", "services.id"],
            ondelete="CASCADE",
            name="fk_service_device_prices_service",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    service_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    device_key: Mapped[str] = mapped_column(String(40), nullable=False)
    device_name: Mapped[str] = mapped_column(String(120), nullable=False)
    price_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EGP", server_default="EGP")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))


class ClinicProduct(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "clinic_products"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_clinic_products_workspace_id_id"),
        UniqueConstraint("workspace_id", "name", name="uq_clinic_products_workspace_name"),
        CheckConstraint("quantity_on_hand >= 0", name="clinic_product_quantity_non_negative"),
    )

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))


class AppointmentProductLine(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "appointment_product_lines"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_appointment_product_lines_workspace_id_id"),
        CheckConstraint("quantity > 0", name="appointment_product_line_quantity_positive"),
        CheckConstraint("unit_price_minor >= 0", name="appointment_product_line_price_non_negative"),
        ForeignKeyConstraint(
            ["workspace_id", "appointment_id"],
            ["appointments.workspace_id", "appointments.id"],
            ondelete="CASCADE",
            name="fk_appointment_product_lines_appointment",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["clinic_products.workspace_id", "clinic_products.id"],
            ondelete="RESTRICT",
            name="fk_appointment_product_lines_product",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    appointment_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    product_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    product_name: Mapped[str] = mapped_column(String(180), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    unit_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EGP", server_default="EGP")
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class InventoryItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inventory_items"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_inventory_items_workspace_id_id"),
        UniqueConstraint("workspace_id", "name", name="uq_inventory_items_workspace_name"),
        CheckConstraint("quantity_ml >= 0", name="inventory_item_quantity_non_negative"),
        CheckConstraint("concentration_mg_per_ml > 0", name="inventory_item_concentration_positive"),
    )

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="injectable", server_default="injectable")
    quantity_ml: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=Decimal("0"), server_default="0")
    concentration_mg_per_ml: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    low_stock_threshold_ml: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))


class InventoryUsage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inventory_usages"
    __table_args__ = (
        CheckConstraint("used_mg > 0", name="inventory_usage_mg_positive"),
        CheckConstraint("used_ml > 0", name="inventory_usage_ml_positive"),
        ForeignKeyConstraint(
            ["workspace_id", "inventory_item_id"],
            ["inventory_items.workspace_id", "inventory_items.id"],
            ondelete="RESTRICT",
            name="fk_inventory_usages_item",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "appointment_id"],
            ["appointments.workspace_id", "appointments.id"],
            ondelete="SET NULL",
            name="fk_inventory_usages_appointment",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    inventory_item_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    appointment_id: Mapped[UUID | None] = mapped_column(index=True, nullable=True)
    used_mg: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    used_ml: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
