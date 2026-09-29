from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

AutomationWorkerStatus = Literal["active", "paused", "revoked"]
AutomationJobStatus = Literal[
    "queued", "processing", "dispatched", "skipped", "failed", "cancelled"
]
AutomationChannel = Literal["auto", "whatsapp", "email", "sms"]


class AutomationTemplateVariant(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    language_code: str = Field(default="ar", min_length=1, max_length=20)

    @field_validator("name", "language_code")
    @classmethod
    def clean_variant_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be empty.")
        return value


class AutomationRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    key: str
    name: str
    enabled: bool
    trigger_kind: str
    offset_minutes: int
    channel: AutomationChannel
    template_name: str
    template_language: str
    max_lateness_minutes: int
    config_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AutomationRuleUpdate(BaseModel):
    enabled: bool | None = None
    template_variants: list[AutomationTemplateVariant] | None = Field(default=None, max_length=20)
    offset_minutes: int | None = None
    channel: AutomationChannel | None = None
    template_name: str | None = Field(default=None, min_length=1, max_length=160)
    template_language: str | None = Field(default=None, min_length=1, max_length=20)
    max_lateness_minutes: int | None = Field(default=None, ge=0)
    config: dict[str, Any] | None = None

    @field_validator("template_name", "template_language")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be empty.")
        return value


class AutomationWorkerCreate(BaseModel):
    name: str = Field(default="n8n automation worker", min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Worker name cannot be empty.")
        return value


class AutomationWorkerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    status: AutomationWorkerStatus
    created_by_user_id: UUID | None
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AutomationWorkerCreated(AutomationWorkerRead):
    worker_token: str
    token_note: str = "Store this token securely. Only its SHA-256 hash is stored by Linka."


class AutomationWorkerTokenRotated(BaseModel):
    worker_id: UUID
    worker_token: str
    token_note: str = "The previous automation worker token is now invalid."


class AutomationWorkerStatusUpdate(BaseModel):
    status: AutomationWorkerStatus


class AutomationJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    rule_id: UUID | None
    appointment_id: UUID | None
    crm_task_id: UUID | None
    patient_id: UUID
    job_kind: Literal["appointment_rule", "crm_follow_up"]
    status: AutomationJobStatus
    scheduled_for: datetime
    attempts: int
    next_attempt_at: datetime | None
    message_id: UUID | None
    dispatch_id: UUID | None
    last_error: str | None
    payload_json: dict[str, Any]
    result_json: dict[str, Any]
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    dispatch_status: str | None = None
    dispatch_last_error: str | None = None
    attention_reason: Literal["execution_failed", "delivery_failed", "stuck_processing"] | None = None




class AutomationOperationsOverview(BaseModel):
    now: datetime
    enabled_rules: int
    queued_jobs: int
    due_jobs: int
    processing_jobs: int
    failed_jobs: int
    delivery_failed_jobs: int
    attention_count: int
    next_job_at: datetime | None
    worker_state: Literal["healthy", "stale", "missing", "not_required"]
    worker_last_seen_at: datetime | None
    worker_fresh_within_minutes: int


class AutomationJobActionResponse(BaseModel):
    job_id: UUID
    job_status: AutomationJobStatus
    dispatch_status: str | None = None
    action: Literal["retry", "cancel"]

class AutomationTickRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    planning_horizon_days: int = Field(default=14, ge=1, le=90)


class AutomationClaimedJob(BaseModel):
    job_id: UUID
    job_kind: Literal["appointment_rule", "crm_follow_up"]
    rule_key: str | None = None
    appointment_id: UUID | None = None
    crm_task_id: UUID | None = None
    patient_id: UUID
    scheduled_for: datetime
    attempt: int


class AutomationTickResponse(BaseModel):
    planned: int
    cancelled: int
    claimed: list[AutomationClaimedJob]


class AutomationExecuteResponse(BaseModel):
    job_id: UUID
    status: AutomationJobStatus
    message_id: UUID | None = None
    dispatch_id: UUID | None = None
    reason: str | None = None
