from datetime import datetime, time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ServiceCategory = Literal["laser", "dermatology", "slimming"]


def normalize_service_category(value: object, *, allow_none: bool = False) -> object:
    if value is None:
        return None if allow_none else "dermatology"
    raw = str(value).strip().casefold()
    if not raw:
        return None if allow_none else "dermatology"
    if raw in {"laser", "ليزر"} or "laser" in raw:
        return "laser"
    if raw in {"slimming", "تخسيس"} or any(
        marker in raw for marker in ("slim", "weight", "body contour")
    ):
        return "slimming"
    if raw in {"dermatology", "جلدية"}:
        return "dermatology"
    # Historical free-text categories (facial, injectables, consultation, etc.)
    # remain compatible but are normalized into the new canonical taxonomy.
    return "dermatology"


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BranchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=50)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=320)
    address_line1: str | None = Field(default=None, max_length=300)
    address_line2: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    country_code: str = Field(default="EG", min_length=2, max_length=2)
    timezone: str | None = Field(default=None, max_length=64)


class BranchUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=320)
    address_line1: str | None = Field(default=None, max_length=300)
    address_line2: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None


class BranchRead(ORMModel):
    id: UUID
    workspace_id: UUID
    name: str
    code: str
    phone: str | None
    email: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    country_code: str
    timezone: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class StaffCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    job_title: str | None = Field(default=None, max_length=120)
    user_id: UUID | None = None


class StaffUpdate(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=120)
    last_name: str | None = Field(default=None, min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    job_title: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None


class StaffRead(ORMModel):
    id: UUID
    workspace_id: UUID
    user_id: UUID | None
    first_name: str
    last_name: str
    email: str | None
    phone: str | None
    job_title: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class DoctorCreate(BaseModel):
    staff_id: UUID
    doctor_type: str = Field(default="regular", pattern=r"^(regular|visiting)$")
    specialization: str | None = Field(default=None, max_length=200)
    license_number: str | None = Field(default=None, max_length=120)
    bio: str | None = Field(default=None, max_length=2000)
    booking_enabled: bool = True


class DoctorUpdate(BaseModel):
    doctor_type: str | None = Field(default=None, pattern=r"^(regular|visiting)$")
    specialization: str | None = Field(default=None, max_length=200)
    license_number: str | None = Field(default=None, max_length=120)
    bio: str | None = Field(default=None, max_length=2000)
    booking_enabled: bool | None = None
    is_active: bool | None = None


class DoctorRead(ORMModel):
    id: UUID
    workspace_id: UUID
    staff_id: UUID
    doctor_type: str
    specialization: str | None
    license_number: str | None
    bio: str | None
    booking_enabled: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ServiceDevicePriceInput(BaseModel):
    device_key: str = Field(min_length=1, max_length=40)
    price_minor: int = Field(ge=0)
    duration_minutes: int = Field(gt=0, le=1440)
    currency: str = Field(default="EGP", min_length=3, max_length=3)

    @field_validator("device_key")
    @classmethod
    def normalize_device_key(cls, value: str) -> str:
        return value.strip()


class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=160, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    category: str | None = Field(default=None, max_length=120)
    operational_category: ServiceCategory = "dermatology"
    description: str | None = None
    duration_minutes: int = Field(gt=0, le=1440)
    buffer_before_minutes: int = Field(default=0, ge=0, le=1440)
    buffer_after_minutes: int = Field(default=0, ge=0, le=1440)
    price_minor: int = Field(default=0, ge=0)
    currency: str = Field(default="EGP", min_length=3, max_length=3)
    requires_medical_review: bool = False
    requires_laser_device: bool = False
    device_prices: list[ServiceDevicePriceInput] = Field(default_factory=list)

    @field_validator("operational_category", mode="before")
    @classmethod
    def normalize_operational_category(cls, value: object) -> object:
        return normalize_service_category(value)

    @model_validator(mode="after")
    def derive_laser_requirement_from_category(self):
        self.requires_laser_device = self.operational_category == "laser"
        if not self.requires_laser_device:
            self.device_prices = []
        return self


class ServiceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    operational_category: ServiceCategory | None = None
    description: str | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=1440)
    buffer_before_minutes: int | None = Field(default=None, ge=0, le=1440)
    buffer_after_minutes: int | None = Field(default=None, ge=0, le=1440)
    price_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    requires_medical_review: bool | None = None
    requires_laser_device: bool | None = None
    device_prices: list[ServiceDevicePriceInput] | None = None
    is_active: bool | None = None

    @field_validator("operational_category", mode="before")
    @classmethod
    def normalize_operational_category(cls, value: object) -> object:
        return normalize_service_category(value, allow_none=True)

    @model_validator(mode="after")
    def derive_laser_requirement_when_category_is_supplied(self):
        if self.operational_category is not None:
            self.requires_laser_device = self.operational_category == "laser"
            if not self.requires_laser_device:
                self.device_prices = [] if self.device_prices is not None else None
        return self


class ServiceRead(ORMModel):
    id: UUID
    workspace_id: UUID
    name: str
    slug: str
    category: str | None
    operational_category: ServiceCategory
    description: str | None
    duration_minutes: int
    buffer_before_minutes: int
    buffer_after_minutes: int
    price_minor: int
    currency: str
    requires_medical_review: bool
    requires_laser_device: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class DoctorBranchAssignment(BaseModel):
    is_primary: bool = False
    is_active: bool = True


class DoctorBranchRead(ORMModel):
    id: UUID
    workspace_id: UUID
    doctor_id: UUID
    branch_id: UUID
    is_primary: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class DoctorServiceAssignment(BaseModel):
    custom_duration_minutes: None = None
    custom_price_minor: int | None = Field(default=None, ge=0)
    is_active: bool = True


class DoctorServiceRead(ORMModel):
    id: UUID
    workspace_id: UUID
    doctor_id: UUID
    service_id: UUID
    custom_duration_minutes: int | None
    custom_price_minor: int | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class WorkingHourInterval(BaseModel):
    weekday: int = Field(ge=0, le=6, description="Monday=0, Sunday=6")
    start_time: time
    end_time: time

    @field_validator("end_time")
    @classmethod
    def end_after_start(cls, end_time: time, info):
        start_time = info.data.get("start_time")
        if start_time is not None and end_time <= start_time:
            raise ValueError("end_time must be after start_time")
        return end_time


class WorkingHoursReplace(BaseModel):
    intervals: list[WorkingHourInterval] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def reject_overlapping_intervals(self):
        by_weekday: dict[int, list[WorkingHourInterval]] = {}
        for interval in self.intervals:
            by_weekday.setdefault(interval.weekday, []).append(interval)

        for intervals in by_weekday.values():
            ordered = sorted(intervals, key=lambda item: item.start_time)
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if current.start_time < previous.end_time:
                    raise ValueError("Working-hour intervals cannot overlap on the same weekday.")
        return self


class BranchWorkingHourRead(ORMModel):
    id: UUID
    workspace_id: UUID
    branch_id: UUID
    weekday: int
    start_time: time
    end_time: time


class DoctorWorkingHourRead(ORMModel):
    id: UUID
    workspace_id: UUID
    doctor_id: UUID
    branch_id: UUID
    weekday: int
    start_time: time
    end_time: time


class BookingSettingsUpsert(BaseModel):
    slot_interval_minutes: int = Field(default=15, gt=0, le=240)
    minimum_notice_minutes: int = Field(default=60, ge=0)
    booking_horizon_days: int = Field(default=90, gt=0, le=730)
    cancellation_notice_minutes: int = Field(default=720, ge=0)
    allow_same_day_booking: bool = True
    require_confirmation: bool = True
    default_currency: str = Field(default="EGP", min_length=3, max_length=3)


class BookingSettingsRead(ORMModel):
    id: UUID
    workspace_id: UUID
    slot_interval_minutes: int
    minimum_notice_minutes: int
    booking_horizon_days: int
    cancellation_notice_minutes: int
    allow_same_day_booking: bool
    require_confirmation: bool
    default_currency: str
    created_at: datetime
    updated_at: datetime
