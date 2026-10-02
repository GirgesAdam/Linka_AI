from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClinicProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    description: str | None = Field(default=None, max_length=1000)
    quantity_on_hand: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Product name is required.")
        return value


class ClinicProductQuantityUpdate(BaseModel):
    quantity_on_hand: int = Field(ge=0, le=1_000_000)


class ClinicProductRead(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    description: str | None
    quantity_on_hand: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class AppointmentProductCreate(BaseModel):
    product_id: UUID
    unit_price_minor: int = Field(ge=0)
    quantity: int = Field(default=1, ge=1, le=100)


class AppointmentProductLineRead(BaseModel):
    id: UUID
    appointment_id: UUID
    product_id: UUID
    product_name: str
    quantity: int
    unit_price_minor: int
    currency: str
    total_minor: int


class InventoryItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    quantity_ml: Decimal = Field(ge=0)
    low_stock_threshold_ml: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)


class InventoryItemAdjust(BaseModel):
    quantity_ml: Decimal = Field(gt=0)


class InventoryUsageCreate(BaseModel):
    used_ml: Decimal = Field(gt=0)
    note: str | None = Field(default=None, max_length=2000)


class InventoryItemRead(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    category: str
    quantity_ml: Decimal
    low_stock_threshold_ml: Decimal | None
    notes: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class InventoryUsageRead(BaseModel):
    id: UUID
    inventory_item_id: UUID
    used_ml: Decimal
    note: str | None
    created_at: datetime


class ClinicLaserDeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Device name is required.")
        return value


class ClinicLaserDeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("Device name is required.")
        return value


class ClinicLaserDeviceRead(BaseModel):
    id: UUID
    workspace_id: UUID
    device_key: str
    name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class LaserDevicePriceUpsert(BaseModel):
    service_id: UUID
    device_key: str = Field(min_length=1, max_length=40)
    price_minor: int = Field(ge=0)
    duration_minutes: int | None = Field(default=None, gt=0, le=1440)
    currency: str = Field(default="EGP", min_length=3, max_length=3)

    @field_validator("device_key")
    @classmethod
    def normalize_device_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Laser device is required.")
        return value


class LaserDevicePriceRead(BaseModel):
    service_id: UUID
    service_name: str
    device_key: str
    device_name: str
    price_minor: int | None
    duration_minutes: int | None
    currency: str
    configured: bool
