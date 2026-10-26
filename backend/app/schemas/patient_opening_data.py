from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class PatientOpeningPackageUpdate(BaseModel):
    offer_id: UUID | None = None
    sessions_remaining: int = Field(gt=0, le=10_000)


class PatientOpeningPulseUpdate(BaseModel):
    device_key: str = Field(min_length=1, max_length=40)
    pulses_remaining: int = Field(gt=0, le=1_000_000_000)

    @field_validator("device_key", mode="before")
    @classmethod
    def normalize_device_key(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class PatientOpeningDataUpdate(BaseModel):
    customer_id: str | None = None
    package: PatientOpeningPackageUpdate | None = None
    pulse_balances: list[PatientOpeningPulseUpdate] = Field(default_factory=list, max_length=20)

    @field_validator("customer_id", mode="before")
    @classmethod
    def normalize_customer_id(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def validate_unique_pulse_devices(self) -> PatientOpeningDataUpdate:
        seen: set[str] = set()
        for balance in self.pulse_balances:
            key = balance.device_key.casefold()
            if key in seen:
                raise ValueError("Each laser device may only have one opening pulse balance.")
            seen.add(key)
        return self


class PatientOpeningPackageRead(BaseModel):
    package_id: UUID
    offer_id: UUID | None
    service_id: UUID
    name: str
    sessions_total: int
    sessions_remaining: int
    current_remaining: int
    sessions_reserved: int
    sessions_consumed: int
    replaceable_identity: bool


class PatientOpeningPulseRead(BaseModel):
    opening_pack_id: UUID
    device_key: str
    device_name: str
    opening_pulses: int
    current_remaining: int
    pulses_consumed: int
    replaceable_identity: bool


class PatientOpeningDataRead(BaseModel):
    patient_id: UUID
    customer_id: str | None
    package: PatientOpeningPackageRead | None
    pulse_balances: list[PatientOpeningPulseRead]
