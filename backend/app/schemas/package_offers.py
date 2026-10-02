from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

PackageSessionCount = PositiveInt


class ServicePackageOfferUpsert(BaseModel):
    service_id: UUID
    device_key: str | None = Field(default=None, min_length=1, max_length=40)
    sessions_count: PackageSessionCount
    price_minor: int = Field(ge=0)
    currency: str = Field(default="EGP", min_length=3, max_length=3)
    is_active: bool = True


class ServicePackageOfferRead(BaseModel):
    id: UUID
    workspace_id: UUID
    service_id: UUID
    service_name: str
    device_key: str | None = None
    device_name: str | None = None
    sessions_count: PackageSessionCount
    price_minor: int
    currency: str
    is_active: bool
    standalone_session_price_minor: int
    savings_minor: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PatientPackageOfferPurchase(BaseModel):
    patient_id: UUID
    offer_id: UUID
    amount_paid_minor: int = Field(default=0, ge=0)
    payment_method: str = "unknown"
    external_reference: str | None = Field(default=None, max_length=128)
