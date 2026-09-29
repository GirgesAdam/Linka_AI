from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

OnboardingProviderCapability = Literal[
    "branch_configuration",
    "service_configuration",
    "doctor_configuration",
    "schedule_configuration",
    "booking_settings_configuration",
]

Weekday = Annotated[int, Field(ge=0, le=6)]


class ProviderBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    code: str
    city: str | None = None
    phone: str | None = None
    email: str | None = None
    address_line1: str | None = None
    country_code: str = "EG"
    timezone: str = "Africa/Cairo"


class ProviderService(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    slug: str
    category: str | None = None
    description: str | None = None
    duration_minutes: int
    buffer_before_minutes: int = 0
    buffer_after_minutes: int = 0
    price_minor: int = 0
    currency: str = "EGP"
    requires_medical_review: bool = False


class ProviderDoctor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    first_name: str
    last_name: str
    specialization: str | None = None
    license_number: str | None = None
    phone: str | None = None
    email: str | None = None


class ProviderBranchHour(BaseModel):
    """
    One weekly schedule rule for a branch.

    `weekdays=[0,1,2,3,4,5,6]` represents every day and is expanded by Python
    into Linka's per-weekday domain rows after structured extraction.
    """

    model_config = ConfigDict(extra="forbid")

    branch_key: str
    weekdays: list[Weekday] = Field(min_length=1, max_length=7)
    start_time: str
    end_time: str


class ProviderDoctorBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doctor_key: str
    branch_key: str
    is_primary: bool = False


class ProviderDoctorService(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doctor_key: str
    service_key: str


class ProviderDoctorHour(BaseModel):
    """
    One weekly schedule rule for a doctor at a branch.
    """

    model_config = ConfigDict(extra="forbid")

    doctor_key: str
    branch_key: str
    weekdays: list[Weekday] = Field(min_length=1, max_length=7)
    start_time: str
    end_time: str


class ProviderBookingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply: bool = False
    slot_interval_minutes: int = 15
    minimum_notice_minutes: int = 60
    booking_horizon_days: int = 90
    cancellation_notice_minutes: int = 720
    allow_same_day_booking: bool = True
    require_confirmation: bool = True
    default_currency: str = "EGP"


class OnboardingProviderDecision(BaseModel):
    """
    Compact provider transport schema.

    All top-level fields stay required. This is intentional: a partial model
    response must fail local validation rather than silently dropping requested
    doctor/service links, schedules or booking settings.

    Weekly schedules are compact arrays of weekdays instead of one record per
    day. Python expands them into the richer domain OnboardingPlan.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["propose", "revise", "confirm", "cancel", "clarify"]
    capabilities: list[OnboardingProviderCapability]
    assistant_message: str
    confidence: float = Field(ge=0.0, le=1.0)
    missing_information: list[str]
    booking_settings: ProviderBookingSettings

    branches: list[ProviderBranch]
    services: list[ProviderService]
    doctors: list[ProviderDoctor]

    doctor_branches: list[ProviderDoctorBranch]
    doctor_services: list[ProviderDoctorService]

    branch_hours: list[ProviderBranchHour]
    doctor_hours: list[ProviderDoctorHour]
