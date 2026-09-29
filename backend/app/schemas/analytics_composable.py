from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.analytics_bi import AnalyticsBIPlan, AnalyticsBIResultRow
from app.schemas.analytics_business import AnalyticsBusinessPlan

AudienceAppointmentStatus = Literal[
    "pending",
    "confirmed",
    "checked_in",
    "in_progress",
    "completed",
    "cancelled",
    "no_show",
]
AudiencePatientStatus = Literal["active", "inactive", "blocked"]
AudienceSort = Literal[
    "last_activity_desc",
    "last_activity_asc",
    "matching_visits_desc",
    "net_paid_desc",
    "first_seen_desc",
]
AnalyticsActionKind = Literal[
    "none",
    "save_audience",
    "follow_up_tasks",
    "whatsapp_campaign",
]
AnalyticsActionPriority = Literal["low", "normal", "high", "urgent"]


class AnalyticsAudiencePlan(BaseModel):
    """Composable, deterministic patient-list query.

    The semantic planner may combine these bounded dimensions, but execution is
    always performed by backend SQLAlchemy queries against canonical Linka data.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["patient_audience"]
    lookback_days: int | None = Field(ge=1, le=3650)
    inactivity_days: int | None = Field(ge=30, le=3650)
    limit: int = Field(ge=1, le=25)
    service_ids: list[str]
    branch_ids: list[str]
    doctor_ids: list[str]
    appointment_statuses: list[AudienceAppointmentStatus]
    min_matching_visits: int = Field(ge=1, le=100)
    max_matching_visits: int | None = Field(ge=1, le=100)
    has_future_appointment: bool | None
    marketing_consent: bool | None
    patient_statuses: list[AudiencePatientStatus]
    min_net_paid_minor: int | None = Field(ge=0)
    max_net_paid_minor: int | None = Field(ge=0)
    currency: str | None
    sort_by: AudienceSort
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("service_ids", "branch_ids", "doctor_ids")
    @classmethod
    def dedupe_ids(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            candidate = str(value).strip()
            if candidate and candidate not in seen:
                result.append(candidate)
                seen.add(candidate)
        return result

    @field_validator("appointment_statuses", "patient_statuses")
    @classmethod
    def dedupe_literals(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be a 3-letter ISO-style code")
        return normalized

    @model_validator(mode="after")
    def validate_ranges(self) -> AnalyticsAudiencePlan:
        if not self.appointment_statuses:
            raise ValueError("appointment_statuses must contain at least one status")
        if not self.patient_statuses:
            raise ValueError("patient_statuses must contain at least one status")
        if self.max_matching_visits is not None and self.max_matching_visits < self.min_matching_visits:
            raise ValueError("max_matching_visits must be >= min_matching_visits")
        if self.max_net_paid_minor is not None and self.min_net_paid_minor is not None:
            if self.max_net_paid_minor < self.min_net_paid_minor:
                raise ValueError("max_net_paid_minor must be >= min_net_paid_minor")
        if (self.min_net_paid_minor is not None or self.max_net_paid_minor is not None or self.sort_by == "net_paid_desc") and not self.currency:
            raise ValueError("currency is required for patient value filters/ranking")
        return self


class AnalyticsActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: AnalyticsActionKind
    title: str | None = Field(max_length=200)
    description: str | None = Field(max_length=1000)
    due_in_days: int | None = Field(ge=0, le=365)
    priority: AnalyticsActionPriority | None
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_action_fields(self) -> AnalyticsActionProposal:
        if self.kind == "none":
            return self
        if self.kind == "follow_up_tasks":
            if self.due_in_days is None:
                self.due_in_days = 1
            if self.priority is None:
                self.priority = "normal"
        return self


class AnalyticsComposePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["metric", "business", "audience"]
    reuse_previous_audience: bool
    metric_plan: AnalyticsBIPlan | None
    business_plan: AnalyticsBusinessPlan | None
    audience_plan: AnalyticsAudiencePlan | None
    action: AnalyticsActionProposal
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_shape(self) -> AnalyticsComposePlan:
        if self.mode == "metric":
            if self.metric_plan is None or self.business_plan is not None or self.audience_plan is not None or self.reuse_previous_audience:
                raise ValueError("metric mode requires metric_plan only")
            if self.action.kind != "none":
                raise ValueError("actions require an audience result")
            return self
        if self.mode == "business":
            if self.business_plan is None or self.metric_plan is not None or self.audience_plan is not None or self.reuse_previous_audience:
                raise ValueError("business mode requires business_plan only")
            if self.action.kind != "none":
                raise ValueError("actions require an audience result")
            return self
        if self.metric_plan is not None or self.business_plan is not None:
            raise ValueError("audience mode cannot include metric/business plans")
        if self.reuse_previous_audience:
            if self.audience_plan is not None:
                raise ValueError("reused audience must not redefine audience_plan")
        elif self.audience_plan is None:
            raise ValueError("audience mode requires audience_plan")
        return self


class AnalyticsComposeRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1200)
    previous_question: str | None = Field(default=None, max_length=1200)
    previous_audience_plan: AnalyticsAudiencePlan | None = None
    previous_business_plan: AnalyticsBusinessPlan | None = None

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("message must not be empty")
        return normalized


class AnalyticsComposeAnswerRead(BaseModel):
    question: str
    mode: Literal["metric", "business", "audience"]
    metric_plan: AnalyticsBIPlan | None = None
    business_plan: AnalyticsBusinessPlan | None = None
    audience_plan: AnalyticsAudiencePlan | None = None
    action: AnalyticsActionProposal
    period_label: str
    answer: str
    definitions: list[str] = Field(default_factory=list)
    rows: list[AnalyticsBIResultRow] = Field(default_factory=list)
    model: str | None = None
