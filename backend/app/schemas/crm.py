from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PatientStatus = Literal["active", "inactive", "blocked"]
PatientSource = Literal[
    "whatsapp",
    "instagram",
    "facebook",
    "website",
    "referral",
    "walk_in",
    "campaign",
    "phone",
    "email",
    "other",
]
PatientNoteType = Literal["general", "preference", "customer_service", "follow_up"]
LeadStatus = Literal["new", "contacted", "qualified", "booked", "won", "lost", "spam"]
ConversationChannel = Literal[
    "whatsapp",
    "instagram",
    "facebook",
    "web",
    "email",
    "sms",
    "phone",
    "other",
]
ConversationStatus = Literal["open", "pending", "closed"]
ConversationOwnerType = Literal["ai", "human"]
MessageSenderType = Literal["patient", "ai", "staff", "system"]
MessageDirection = Literal["inbound", "outbound", "internal"]
MessageDeliveryStatus = Literal["received", "queued", "sent", "delivered", "read", "failed", "cancelled"]
CRMTaskType = Literal["follow_up", "general"]
CRMTaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
CRMTaskPriority = Literal["low", "normal", "high", "urgent"]
CRMTaskSource = Literal["manual", "ai", "system"]
CRMTaskExecutionMode = Literal["human", "ai"]


def normalize_phone(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    display = value.strip()
    if not display:
        return None, None
    if len(display) > 40:
        raise ValueError("Phone number is too long.")
    compact = re.sub(r"[\s().-]", "", display)
    if compact.startswith("+"):
        digits = compact[1:]
        normalized = f"+{digits}"
    else:
        digits = compact
        normalized = digits
    if not digits.isdigit() or not 7 <= len(digits) <= 15:
        raise ValueError("Phone must contain 7 to 15 digits, optionally starting with +.")
    return display, normalized


def normalize_patient_entry_phone(value: str | None) -> tuple[str | None, str | None]:
    """Validate manually entered patient phones as exactly 11 local digits.

    This is intentionally narrower than normalize_phone(), which stays flexible for
    channel identities, imports, analytics lookups, and legacy data.
    """

    display, normalized = normalize_phone(value)
    if display is None:
        return None, None
    compact = re.sub(r"[\s().-]", "", display)
    if compact.startswith("+") or not compact.isdigit() or len(compact) != 11:
        raise ValueError("Patient phone must contain exactly 11 digits.")
    return display, normalized


def normalize_patient_identity_phone(value: str | None) -> tuple[str | None, str | None]:
    """Return a stable phone identity key for clinic imports without rewriting legacy CRM storage.

    Linka is currently Egypt/EGP-first, so common Egyptian mobile representations
    (010..., +2010..., 002010..., 2010...) are treated as the same source identity.
    Other numbers keep the normal CRM representation.
    """

    display, normalized = normalize_phone(value)
    if not normalized:
        return display, normalized
    if normalized.startswith("0020") and len(normalized) == 14:
        return display, f"+{normalized[2:]}"
    if normalized.startswith("01") and len(normalized) == 11:
        return display, f"+20{normalized[1:]}"
    if normalized.startswith("20") and len(normalized) == 12:
        return display, f"+{normalized}"
    return display, normalized


def normalize_required_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("Value cannot be empty.")
    return normalized


class PatientCreate(BaseModel):
    first_name: str = Field(max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    gender: str | None = Field(default=None, max_length=32)
    birth_date: date | None = None
    preferred_language: str = Field(default="ar", min_length=2, max_length=10)
    preferred_branch_id: UUID | None = None
    source: PatientSource = "other"
    source_detail: str | None = Field(default=None, max_length=200)
    status: PatientStatus = "active"
    marketing_consent: bool = False
    whatsapp_opt_in: bool = False

    @field_validator("first_name")
    @classmethod
    def validate_first_name(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("last_name", "gender", "source_detail", mode="before")
    @classmethod
    def strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("phone", mode="before")
    @classmethod
    def validate_phone(cls, value: object) -> object:
        if value is None or isinstance(value, str):
            display, _ = normalize_patient_entry_phone(value)
            return display
        return value


class PatientUpdate(BaseModel):
    first_name: str | None = Field(default=None, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    gender: str | None = Field(default=None, max_length=32)
    birth_date: date | None = None
    preferred_language: str | None = Field(default=None, min_length=2, max_length=10)
    preferred_branch_id: UUID | None = None
    source: PatientSource | None = None
    source_detail: str | None = Field(default=None, max_length=200)
    status: PatientStatus | None = None
    marketing_consent: bool | None = None
    whatsapp_opt_in: bool | None = None

    @field_validator("first_name")
    @classmethod
    def validate_first_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_required_text(value)

    @field_validator("last_name", "gender", "source_detail", mode="before")
    @classmethod
    def strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("phone", mode="before")
    @classmethod
    def validate_phone(cls, value: object) -> object:
        if value is None or isinstance(value, str):
            display, _ = normalize_patient_entry_phone(value)
            return display
        return value


class PatientRead(BaseModel):
    id: UUID
    workspace_id: UUID
    first_name: str
    last_name: str | None
    phone: str | None
    gender: str | None
    birth_date: date | None
    preferred_language: str
    preferred_branch_id: UUID | None
    source: PatientSource
    source_detail: str | None
    status: PatientStatus
    marketing_consent: bool
    marketing_consent_at: datetime | None
    whatsapp_opt_in: bool
    whatsapp_opt_in_at: datetime | None
    whatsapp_opt_in_source: str | None
    source_created_at: datetime | None = None
    last_contact_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PatientTagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str | None = Field(default=None, max_length=20)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return normalize_required_text(value)


class PatientTagRead(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    color: str | None
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class PatientNoteCreate(BaseModel):
    note_type: PatientNoteType = "general"
    content: str = Field(min_length=1, max_length=10000)
    is_pinned: bool = False

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return normalize_required_text(value)


class PatientNoteRead(BaseModel):
    id: UUID
    workspace_id: UUID
    patient_id: UUID
    author_user_id: UUID | None
    note_type: PatientNoteType
    content: str
    is_pinned: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PatientTimelineAppointment(BaseModel):
    id: UUID
    status: str
    start_at: datetime
    end_at: datetime
    service_name: str
    branch_name: str
    doctor_name: str
    price_minor: int
    currency: str
    from_status: str | None = None
    to_status: str | None = None
    reason: str | None = None


class PatientTimelineMessage(BaseModel):
    id: UUID
    conversation_id: UUID
    sender_type: str
    direction: str
    message_type: str
    content: str | None
    delivery_status: str
    channel: str


class PatientTimelineHandoff(BaseModel):
    id: UUID
    conversation_id: UUID
    event_type: str
    status: str
    category: str
    priority: str
    reason: str


class PatientTimelineNote(BaseModel):
    id: UUID
    note_type: PatientNoteType
    content: str
    is_pinned: bool


class PatientTimelineTask(BaseModel):
    id: UUID
    event_type: Literal["created", "completed"]
    status: CRMTaskStatus
    priority: CRMTaskPriority
    task_type: CRMTaskType
    title: str
    due_at: datetime
    assigned_user_id: UUID | None = None


class PatientTimelinePayment(BaseModel):
    id: UUID
    appointment_id: UUID | None
    transaction_type: Literal["payment", "refund"]
    amount_minor: int
    currency: str
    payment_method: str
    reference_transaction_id: UUID | None = None
    reason: str | None = None


PatientTimelineKind = Literal[
    "patient_created",
    "note",
    "appointment",
    "appointment_status",
    "message",
    "handoff",
    "task",
    "payment",
]


class PatientTimelineEvent(BaseModel):
    id: str
    kind: PatientTimelineKind
    occurred_at: datetime
    actor_type: str | None = None
    actor_user_id: UUID | None = None
    actor_name: str | None = None
    appointment: PatientTimelineAppointment | None = None
    message: PatientTimelineMessage | None = None
    handoff: PatientTimelineHandoff | None = None
    note: PatientTimelineNote | None = None
    task: PatientTimelineTask | None = None
    payment: PatientTimelinePayment | None = None


class PatientCRMStats(BaseModel):
    total_appointments: int
    completed_appointments: int
    no_show_appointments: int
    upcoming_appointments: int
    total_conversations: int
    open_conversations: int
    active_handoffs: int
    active_leads: int
    open_tasks: int = 0
    overdue_tasks: int = 0
    next_task_at: datetime | None = None
    next_appointment_at: datetime | None = None
    last_appointment_at: datetime | None = None


class PatientProfileRead(BaseModel):
    patient: PatientRead
    stats: PatientCRMStats
    tags: list[PatientTagRead]
    notes: list[PatientNoteRead]
    timeline: list[PatientTimelineEvent]
    latest_conversation_id: UUID | None = None


class LeadCreate(BaseModel):
    patient_id: UUID
    service_id: UUID | None = None
    assigned_user_id: UUID | None = None
    source: PatientSource | None = None
    status: LeadStatus = "new"
    estimated_value_minor: int | None = Field(default=None, ge=0)
    currency: str = Field(default="EGP", min_length=3, max_length=3)
    lost_reason: str | None = Field(default=None, max_length=2000)
    next_follow_up_at: datetime | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_lost_reason(self) -> LeadCreate:
        if self.lost_reason and self.status != "lost":
            raise ValueError("lost_reason can only be set when status is lost.")
        return self


class LeadUpdate(BaseModel):
    service_id: UUID | None = None
    assigned_user_id: UUID | None = None
    source: PatientSource | None = None
    status: LeadStatus | None = None
    estimated_value_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    lost_reason: str | None = Field(default=None, max_length=2000)
    next_follow_up_at: datetime | None = None
    last_contact_at: datetime | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else value


class LeadRead(BaseModel):
    id: UUID
    workspace_id: UUID
    patient_id: UUID
    service_id: UUID | None
    assigned_user_id: UUID | None
    source: str
    status: LeadStatus
    estimated_value_minor: int | None
    currency: str
    lost_reason: str | None
    next_follow_up_at: datetime | None
    last_contact_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CRMTaskCreate(BaseModel):
    patient_id: UUID
    lead_id: UUID | None = None
    conversation_id: UUID | None = None
    assigned_user_id: UUID | None = None
    task_type: CRMTaskType = "follow_up"
    execution_mode: CRMTaskExecutionMode = "human"
    priority: CRMTaskPriority = "normal"
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    due_at: datetime

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class CRMTaskUpdate(BaseModel):
    assigned_user_id: UUID | None = None
    status: CRMTaskStatus | None = None
    priority: CRMTaskPriority | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    due_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        return normalize_required_text(value) if value is not None else None

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class CRMTaskRead(BaseModel):
    id: UUID
    workspace_id: UUID
    patient_id: UUID
    lead_id: UUID | None
    conversation_id: UUID | None
    assigned_user_id: UUID | None
    created_by_user_id: UUID | None
    completed_by_user_id: UUID | None
    task_type: CRMTaskType
    source: CRMTaskSource
    execution_mode: CRMTaskExecutionMode
    status: CRMTaskStatus
    priority: CRMTaskPriority
    title: str
    description: str | None
    due_at: datetime
    completed_at: datetime | None
    patient_name: str
    assigned_user_name: str | None = None
    assigned_user_email: str | None = None
    is_overdue: bool
    created_at: datetime
    updated_at: datetime


class ConversationCreate(BaseModel):
    patient_id: UUID
    channel: ConversationChannel
    status: ConversationStatus = "open"
    external_conversation_id: str | None = Field(default=None, max_length=255)
    assigned_user_id: UUID | None = None
    subject: str | None = Field(default=None, max_length=250)


class ConversationUpdate(BaseModel):
    status: ConversationStatus | None = None
    assigned_user_id: UUID | None = None
    subject: str | None = Field(default=None, max_length=250)


class ConversationRead(BaseModel):
    id: UUID
    workspace_id: UUID
    patient_id: UUID
    channel: ConversationChannel
    status: ConversationStatus
    external_conversation_id: str | None
    assigned_user_id: UUID | None
    owner_type: ConversationOwnerType
    unread_count: int
    ownership_changed_at: datetime
    subject: str | None
    started_at: datetime
    last_message_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MessageCreate(BaseModel):
    sender_type: MessageSenderType = "staff"
    direction: MessageDirection = "outbound"
    message_type: str = Field(default="text", min_length=1, max_length=32)
    content: str | None = Field(default=None, max_length=50000)
    external_message_id: str | None = Field(default=None, max_length=255)
    delivery_status: MessageDeliveryStatus | None = None
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_message(self) -> MessageCreate:
        if self.message_type == "text" and not (self.content and self.content.strip()):
            raise ValueError("Text messages require non-empty content.")
        if self.sender_type == "patient" and self.direction != "inbound":
            raise ValueError("Patient messages must be inbound.")
        if self.sender_type in {"staff", "ai"} and self.direction == "inbound":
            raise ValueError("Staff and AI messages cannot be inbound.")
        return self


class MessageRead(BaseModel):
    id: UUID
    workspace_id: UUID
    conversation_id: UUID
    sender_type: MessageSenderType
    direction: MessageDirection
    message_type: str
    content: str | None
    external_message_id: str | None
    delivery_status: MessageDeliveryStatus
    sent_by_user_id: UUID | None
    metadata: dict = Field(validation_alias="metadata_json")
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
