from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

HandoffStatus = Literal["pending", "claimed", "resolved"]
HandoffCategory = Literal[
    "medical",
    "complaint",
    "payment",
    "customer_request",
    "booking_exception",
    "agent_uncertain",
    "other",
]
HandoffPriority = Literal["low", "normal", "high", "urgent"]
HandoffSource = Literal["ai", "staff", "system", "customer"]
ConversationStatusAfter = Literal["open", "closed"]
ConversationOwnerType = Literal["ai", "human"]
ConversationStatus = Literal["open", "pending", "closed"]


class HandoffRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    conversation_id: UUID
    patient_id: UUID
    status: HandoffStatus
    category: HandoffCategory
    priority: HandoffPriority
    source: HandoffSource
    reason: str
    context_json: dict = Field(default_factory=dict)
    assigned_user_id: UUID | None
    created_by_user_id: UUID | None
    claimed_at: datetime | None
    resolved_at: datetime | None
    resolved_by_user_id: UUID | None
    resolution_note: str | None
    created_at: datetime
    updated_at: datetime


class HandoffQueueItem(HandoffRead):
    patient_name: str
    patient_phone: str | None
    channel: str
    conversation_last_message_at: datetime | None
    conversation_owner_type: ConversationOwnerType
    conversation_unread_count: int
    assigned_user_name: str | None = None
    assigned_user_email: str | None = None


class HandoffAssignRequest(BaseModel):
    user_id: UUID


class TakeoverRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=4000)
    category: HandoffCategory = "other"
    priority: HandoffPriority = "normal"

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class StaffReplyRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10000)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be empty.")
        return value


class ResolveHandoffRequest(BaseModel):
    resolution_note: str | None = Field(default=None, max_length=4000)
    conversation_status_after: ConversationStatusAfter = "open"

    @field_validator("resolution_note")
    @classmethod
    def normalize_resolution_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class InboxMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    channel_connection_id: UUID | None
    sender_type: str
    direction: str
    message_type: str
    content: str | None
    delivery_status: str
    sent_by_user_id: UUID | None
    metadata_json: dict
    created_at: datetime


class StaffReplyResponse(BaseModel):
    message: InboxMessageRead
    dispatch_required: bool
    dispatch_id: UUID | None = None


class HandoffEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    handoff_request_id: UUID
    conversation_id: UUID
    event_type: str
    actor_type: str
    actor_user_id: UUID | None
    metadata_json: dict
    created_at: datetime


class InboxPatientRead(BaseModel):
    id: UUID
    first_name: str
    last_name: str | None
    phone: str | None




class InboxAssigneeRead(BaseModel):
    id: UUID
    full_name: str | None
    email: str


class InboxConversationListItem(BaseModel):
    id: UUID
    workspace_id: UUID
    patient_id: UUID
    channel: str
    status: str
    owner_type: ConversationOwnerType
    unread_count: int
    assigned_user_id: UUID | None
    assigned_user: InboxAssigneeRead | None
    subject: str | None
    started_at: datetime
    last_message_at: datetime | None
    patient: InboxPatientRead
    active_handoff: HandoffRead | None
    last_message: InboxMessageRead | None


class InboxConversationRead(BaseModel):
    id: UUID
    workspace_id: UUID
    patient_id: UUID
    channel: str
    channel_connection_id: UUID | None
    status: str
    assigned_user_id: UUID | None
    owner_type: ConversationOwnerType
    unread_count: int
    ownership_changed_at: datetime
    subject: str | None
    started_at: datetime
    last_message_at: datetime | None
    closed_at: datetime | None
    patient: InboxPatientRead
    assigned_user: InboxAssigneeRead | None
    active_handoff: HandoffRead | None
    handoff_history: list[HandoffRead]
    messages: list[InboxMessageRead]
    handoff_events: list[HandoffEventRead]


class InboxSummaryRead(BaseModel):
    unread_conversations: int = 0


class InboxRevisionRead(BaseModel):
    revision: str


class ConversationReadReceipt(BaseModel):
    conversation_id: UUID
    unread_count: int = 0

