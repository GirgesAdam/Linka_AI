from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PatientPackageCreate(BaseModel):
    patient_id: UUID
    service_id: UUID
    name: str = Field(min_length=1, max_length=200)
    sessions_purchased: int = Field(ge=1, le=1000)
    sale_price_minor: int = Field(ge=0)
    amount_paid_minor: int | None = Field(default=None, ge=0)
    purchased_at: datetime | None = None
    expires_at: date | None = None
    payment_method: str = "unknown"
    external_reference: str | None = Field(default=None, max_length=128)
    external_id: str | None = Field(default=None, max_length=128)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Package name cannot be empty.")
        return value

    @model_validator(mode="after")
    def validate_payment(self):
        amount_paid = self.sale_price_minor if self.amount_paid_minor is None else self.amount_paid_minor
        if amount_paid > self.sale_price_minor:
            raise ValueError("Initial payment cannot exceed the package price.")
        if amount_paid > 0 and self.payment_method == "unknown":
            raise ValueError("A paid package requires an explicit payment method.")
        return self


class PatientPackageRead(BaseModel):
    id: UUID
    workspace_id: UUID
    patient_id: UUID
    service_id: UUID
    purchase_transaction_id: UUID | None
    package_offer_id: UUID | None = None
    opening_sessions_remaining: int | None = None
    sessions_total_known: bool = True
    external_id: str | None
    name: str
    sessions_purchased: int
    sessions_reserved: int = 0
    sessions_consumed: int = 0
    sessions_remaining: int = 0
    sale_price_minor: int
    amount_paid_minor: int = 0
    amount_refunded_minor: int = 0
    balance_due_minor: int = 0
    cancellation_consumed_sessions: int = 0
    cancellation_default_charge_minor: int | None = None
    standalone_session_price_minor_at_purchase: int | None = None
    laser_device_key: str | None = None
    laser_device_name: str | None = None
    currency: str
    purchased_at: datetime
    expires_at: date | None
    status: str
    effective_status: str
    source: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PatientPackagePaymentCreate(BaseModel):
    amount_minor: int = Field(gt=0)
    payment_method: str
    external_reference: str | None = Field(default=None, max_length=128)


class PatientPackageCancelRefundCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    settlement_target_minor: int | None = Field(default=None, ge=0)
    payment_method: str = Field(default="cash", min_length=1, max_length=20)
    standalone_session_price_minor_at_purchase: int | None = Field(default=None, ge=0)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Refund reason cannot be empty.")
        return value


class PatientPackageCancelRefundRead(BaseModel):
    package: PatientPackageRead
    collected_minor: int
    consumed_sessions: int
    consumed_value_minor: int
    settlement_target_minor: int
    previously_refunded_minor: int
    collected_now_minor: int
    refunded_now_minor: int
    refund_transaction_ids: list[UUID]
