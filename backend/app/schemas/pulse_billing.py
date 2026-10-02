from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PulseBillingSettingsUpsert(BaseModel):
    device_key: str = Field(min_length=1, max_length=40)
    overage_price_minor: int = Field(gt=0)
    currency: str = Field(default="EGP", min_length=3, max_length=3)


class PulseBillingSettingsRead(BaseModel):
    device_key: str
    device_name: str
    overage_price_minor: int | None = None
    currency: str = "EGP"


class PulsePackOfferUpsert(BaseModel):
    device_key: str
    pulses_count: int = Field(gt=0, le=10_000_000)
    price_minor: int = Field(ge=0)
    currency: str = Field(default="EGP", min_length=3, max_length=3)
    is_active: bool = True
class PulsePackOfferRead(BaseModel):
    id: UUID
    workspace_id: UUID
    device_key: str
    device_name: str
    pulses_count: int
    price_minor: int
    currency: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PulsePackPurchase(BaseModel):
    patient_id: UUID
    offer_id: UUID
    amount_paid_minor: int = Field(default=0, ge=0)
    payment_method: str = "unknown"
    external_reference: str | None = Field(default=None, max_length=128)
    expires_at: date | None = None


class PulsePackPaymentCreate(BaseModel):
    amount_minor: int = Field(gt=0)
    payment_method: str
    external_reference: str | None = Field(default=None, max_length=128)
class PatientPulsePackRead(BaseModel):
    id: UUID
    workspace_id: UUID
    patient_id: UUID
    pulse_pack_offer_id: UUID | None
    origin_appointment_id: UUID | None
    purchase_transaction_id: UUID | None
    device_key: str
    device_name: str
    pulses_purchased: int
    pulses_consumed: int
    pulses_remaining: int
    sale_price_minor: int
    amount_paid_minor: int = 0
    amount_refunded_minor: int = 0
    balance_due_minor: int = 0
    standalone_pulse_price_minor_at_purchase: int | None = None
    currency: str
    purchased_at: datetime
    expires_at: date | None
    status: str
    effective_status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
class PulseBalanceRead(BaseModel):
    device_key: str
    device_name: str
    pulses_purchased: int
    pulses_consumed: int
    pulses_remaining: int
    active_pack_count: int


class AppointmentPulseSettlementRead(BaseModel):
    appointment_id: UUID
    pulses_used: int
    pulses_from_balance: int
    deficit_pulses: int
    resolution: Literal["pending", "balance", "new_pack", "overage"]
    resolution_pulse_pack_id: UUID | None = None
    overage_unit_price_minor: int | None = None
    overage_charge_minor: int = 0
    resolved_at: datetime | None = None
    available_balance_after: int = 0
    currency: str = "EGP"


class PulseDeficitPackResolution(BaseModel):
    offer_id: UUID
    amount_paid_minor: int = Field(default=0, ge=0)
    payment_method: str = "unknown"
    external_reference: str | None = Field(default=None, max_length=128)
