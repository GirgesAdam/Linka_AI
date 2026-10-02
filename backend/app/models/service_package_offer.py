from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ServicePackageOffer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Admin-configured sellable package for one service.

    Laser services scope offers to a specific device. Other services keep the
    device fields null. PatientPackage snapshots the commercial facts at purchase
    time, so changing or disabling an offer never mutates packages patients own.
    """

    __tablename__ = "service_package_offers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_service_package_offers_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "service_id",
            "device_key",
            "sessions_count",
            name="uq_service_package_offers_workspace_service_device_sessions",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "device_key"],
            ["clinic_laser_devices.workspace_id", "clinic_laser_devices.device_key"],
            ondelete="RESTRICT",
            name="fk_service_package_offers_clinic_device",
        ),
        CheckConstraint(
            "(device_key IS NULL AND device_name IS NULL) OR "
            "(device_key IS NOT NULL AND device_name IS NOT NULL)",
            name="service_package_offer_device_pair_consistent",
        ),
        CheckConstraint(
            "sessions_count > 0",
            name="service_package_offer_sessions_valid",
        ),
        Index(
            "uq_service_package_offers_workspace_service_sessions_no_device",
            "workspace_id",
            "service_id",
            "sessions_count",
            unique=True,
            postgresql_where=text("device_key IS NULL"),
            sqlite_where=text("device_key IS NULL"),
        ),
        CheckConstraint("price_minor >= 0", name="service_package_offer_price_non_negative"),
        ForeignKeyConstraint(
            ["workspace_id", "service_id"],
            ["services.workspace_id", "services.id"],
            ondelete="CASCADE",
            name="fk_service_package_offers_service",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    service_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    device_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sessions_count: Mapped[int] = mapped_column(Integer, nullable=False)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EGP", server_default="EGP")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
