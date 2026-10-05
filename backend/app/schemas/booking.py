from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

AppointmentStatus = Literal[
    "pending",
    "confirmed",
    "checked_in",
    "in_progress",
    "completed",
    "cancelled",
    "no_show",
    "rescheduled",
]
AppointmentSource = Literal[
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
]
LaserDeviceKey = str
OperationalAppointmentStatus = Literal["completed", "no_show"]
AppointmentListScope = Literal["all", "today", "upcoming", "past"]
AppointmentOperationAction = Literal[
    "confirm", "reschedule", "cancel", "complete", "no_show"
]


def require_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime must include a timezone offset.")
    return value


class AvailabilityBlockCreate(BaseModel):
    branch_id: UUID
    date: date
    start_time: time
    end_time: time
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class AvailabilityBlockRead(BaseModel):
    id: UUID
    workspace_id: UUID
    branch_id: UUID
    start_at: datetime
    end_at: datetime
    reason: str | None
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime
    overlapping_appointments: int = 0

    model_config = ConfigDict(from_attributes=True)


class AvailabilitySlot(BaseModel):
    branch_id: UUID
    doctor_id: UUID
    doctor_assignment_known: bool = True
    is_quick_booking: bool = False
    service_id: UUID
    start_at: datetime
    end_at: datetime
    price_minor: int
    currency: str
    laser_device_key: LaserDeviceKey | None = None
    laser_device_name: str | None = None


class AvailabilityResponse(BaseModel):
    date: date
    timezone: str
    slots: list[AvailabilitySlot]


class QuickAppointmentCreate(BaseModel):
    patient_id: UUID
    branch_id: UUID
    doctor_id: UUID
    service_id: UUID
    patient_package_id: UUID | None = None
    use_pulse_balance: bool = False
    start_at: datetime
    customer_note: str | None = Field(default=None, max_length=5000)
    laser_device_key: LaserDeviceKey | None = None

    @field_validator("start_at")
    @classmethod
    def validate_start_at(cls, value: datetime) -> datetime:
        return require_timezone_aware(value)

    @field_validator("customer_note", mode="before")
    @classmethod
    def normalize_note(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class AppointmentLaserUsageUpdate(BaseModel):
    pulses_used: int = Field(ge=0, le=10_000_000)


class AppointmentCreate(BaseModel):
    patient_id: UUID
    branch_id: UUID
    doctor_id: UUID
    doctor_assignment_known: bool = True
    service_id: UUID
    patient_package_id: UUID | None = None
    use_pulse_balance: bool = False
    lead_id: UUID | None = None
    start_at: datetime
    source: AppointmentSource = "staff"
    customer_note: str | None = Field(default=None, max_length=5000)
    laser_device_key: LaserDeviceKey | None = None

    @field_validator("start_at")
    @classmethod
    def validate_start_at(cls, value: datetime) -> datetime:
        return require_timezone_aware(value)

    @field_validator("customer_note", mode="before")
    @classmethod
    def normalize_note(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class AppointmentCancel(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    override_policy: bool = False

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Cancellation reason cannot be empty.")
        return value


class AppointmentReschedule(BaseModel):
    start_at: datetime
    branch_id: UUID | None = None
    doctor_id: UUID | None = None
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("start_at")
    @classmethod
    def validate_start_at(cls, value: datetime) -> datetime:
        return require_timezone_aware(value)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class AppointmentOperationalStatusUpdate(BaseModel):
    status: OperationalAppointmentStatus
    reason: str | None = Field(default=None, max_length=2000)


class AppointmentRead(BaseModel):
    id: UUID
    workspace_id: UUID
    patient_id: UUID
    branch_id: UUID
    doctor_id: UUID
    doctor_assignment_known: bool = True
    is_quick_booking: bool = False
    service_id: UUID
    patient_package_id: UUID | None = None
    lead_id: UUID | None
    created_by_user_id: UUID | None
    rescheduled_from_appointment_id: UUID | None
    status: AppointmentStatus
    source: AppointmentSource
    start_at: datetime
    end_at: datetime
    busy_start_at: datetime
    busy_end_at: datetime
    duration_minutes: int
    price_minor: int
    currency: str
    laser_device_key: LaserDeviceKey | None = None
    laser_device_name: str | None = None
    laser_pulses_used: int | None = None
    payment_status: str = "unknown"
    amount_paid_minor: int | None = None
    payment_method: str = "unknown"
    billing_context: str = "standard"
    package_external_id: str | None = None
    customer_note: str | None
    cancellation_reason: str | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    completed_at: datetime | None
    no_show_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AppointmentStatusHistoryRead(BaseModel):
    id: UUID
    workspace_id: UUID
    appointment_id: UUID
    changed_by_user_id: UUID | None
    from_status: AppointmentStatus | None
    to_status: AppointmentStatus
    reason: str | None
    metadata: dict = Field(validation_alias="metadata_json")
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class AppointmentPatientSummary(BaseModel):
    id: UUID
    name: str
    phone: str | None


class AppointmentEntitySummary(BaseModel):
    id: UUID
    name: str


class AppointmentAutomationRead(BaseModel):
    id: UUID
    rule_key: str
    rule_name: str
    status: str
    scheduled_for: datetime
    attempts: int
    last_error: str | None


class AppointmentOperationsRead(BaseModel):
    appointment: AppointmentRead
    patient: AppointmentPatientSummary
    branch: AppointmentEntitySummary
    service: AppointmentEntitySummary
    doctor: AppointmentEntitySummary
    timezone: str
    history: list[AppointmentStatusHistoryRead]
    automations: list[AppointmentAutomationRead]
    allowed_actions: list[AppointmentOperationAction]
    cancellation_override_required: bool
    can_override_cancellation_policy: bool