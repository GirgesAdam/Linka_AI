from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

DoctorType = Literal["regular"]


class ClinicProfileUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=120)

    @field_validator("name", "phone", "address", "city")
    @classmethod
    def clean_text(cls, value: str | None):
        if value is None:
            return None
        value = value.strip()
        return value or None


class ClinicServiceCreateV2(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    duration_minutes: int = Field(gt=0, le=1440)
    price: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)

    @field_validator("name", "category")
    @classmethod
    def clean_service_text(cls, value: str | None):
        if value is None:
            return None
        value = value.strip()
        return value or None


class ClinicServiceUpdateV2(ClinicServiceCreateV2):
    pass


class ClinicServiceReadV2(BaseModel):
    id: UUID
    name: str
    category: str | None
    duration_minutes: int
    price: Decimal


class ClinicDoctorCreateV2(BaseModel):
    full_name: str = Field(min_length=1, max_length=240)
    doctor_type: DoctorType = "regular"
    specialization: str | None = Field(default=None, max_length=200)
    service_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @field_validator("full_name", "specialization")
    @classmethod
    def clean_doctor_text(cls, value: str | None):
        if value is None:
            return None
        value = " ".join(value.split())
        return value or None


class ClinicDoctorUpdateV2(BaseModel):
    full_name: str = Field(min_length=1, max_length=240)
    doctor_type: DoctorType | None = None
    specialization: str | None = Field(default=None, max_length=200)
    service_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @field_validator("full_name", "specialization")
    @classmethod
    def clean_doctor_update_text(cls, value: str | None):
        if value is None:
            return None
        value = " ".join(value.split())
        return value or None


class ClinicDoctorServicesUpdateV2(BaseModel):
    service_ids: list[UUID] = Field(default_factory=list, max_length=100)


class ClinicDoctorTypeUpdateV2(BaseModel):
    doctor_type: DoctorType


class WorkingHourInputV2(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class WorkingHoursUpdateV2(BaseModel):
    intervals: list[WorkingHourInputV2] = Field(default_factory=list, max_length=50)


class VisitingWindowInputV2(BaseModel):
    date: date
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class VisitingWindowsUpdateV2(BaseModel):
    windows: list[VisitingWindowInputV2] = Field(default_factory=list, max_length=100)


class ClinicDoctorReadV2(BaseModel):
    id: UUID
    staff_id: UUID
    full_name: str
    doctor_type: DoctorType
    specialization: str | None
    service_ids: list[UUID]
    weekly_hours: list[WorkingHourInputV2]


class ClinicProfileReadV2(BaseModel):
    branch_id: UUID | None
    name: str
    phone: str | None
    address: str | None
    city: str | None
    timezone: str = "Africa/Cairo"


class BookingPolicyUpdateV2(BaseModel):
    slot_interval_minutes: int = Field(default=15, gt=0, le=240)
    minimum_notice_minutes: int = Field(default=60, ge=0, le=43200)
    booking_horizon_days: int = Field(default=90, gt=0, le=730)
    cancellation_notice_minutes: int = Field(default=720, ge=0, le=43200)
    allow_same_day_booking: bool = True
    require_confirmation: bool = True


class ClinicSetupReadinessV2(BaseModel):
    ready: bool
    checks: dict[str, bool]
    missing: list[str]
    progress_percent: int


class ClinicSetupImportDocument(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1)


class ClinicSetupImportIssue(BaseModel):
    sheet: str
    row: int
    message: str




class ClinicSetupDraft(BaseModel):
    clinic_profile: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    services: list[dict[str, str | int | float | bool | None]] = Field(default_factory=list)
    doctors: list[dict[str, str | int | float | bool | None]] = Field(default_factory=list)
    doctor_services: list[dict[str, str | int | float | bool | None]] = Field(default_factory=list)
    clinic_hours: list[dict[str, str | int | float | bool | None]] = Field(default_factory=list)
    doctor_hours: list[dict[str, str | int | float | bool | None]] = Field(default_factory=list)
    visiting_windows: list[dict[str, str | int | float | bool | None]] = Field(default_factory=list)
    booking_policy: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ClinicSetupPreviewResponse(BaseModel):
    draft: ClinicSetupDraft
    issues: list[ClinicSetupImportIssue] = Field(default_factory=list)
    recognized_sheets: list[str] = Field(default_factory=list)


class ClinicSetupApplyDraftRequest(BaseModel):
    draft: ClinicSetupDraft

class ClinicSetupImportResponse(BaseModel):
    imported_counts: dict[str, int]
    skipped_counts: dict[str, int]
    issues: list[ClinicSetupImportIssue] = Field(default_factory=list)
    snapshot: ClinicSetupV2Snapshot


class ClinicSetupV2Snapshot(BaseModel):
    workspace_id: UUID
    clinic: ClinicProfileReadV2
    services: list[ClinicServiceReadV2]
    doctors: list[ClinicDoctorReadV2]
    clinic_hours: list[WorkingHourInputV2]
    booking_policy: dict
    readiness: ClinicSetupReadinessV2
