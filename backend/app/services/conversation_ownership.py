from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.models.channel_inbound_event import ChannelInboundEvent
from app.models.conversation import Conversation
from app.models.handoff_request import HandoffRequest
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch

OWNER_AI = "ai"
OWNER_HUMAN = "human"


DISPATCH_SEND_LEASE = timedelta(minutes=10)
_AGENT_OWNERSHIP_EPOCH_ATTR = "_tia_agent_ownership_epoch"


class OwnershipTransitionBlockedError(RuntimeError):
    """Raised when ownership cannot safely change while a provider send is in flight."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _channel_turn_is_after_latest_handback(conversation: Conversation) -> bool:
    """Block a delayed provider event that belongs to the human-owned period.

    Channel processing holds a short durable lease before entering the agent.
    Comparing that event's exact inbound message with the latest persisted handoff
    resolution means returning ownership to AI is silent until a newer customer
    message arrives. Direct agent API turns have no channel event and are already
    explicit new customer requests, so they are unaffected.
    """
    if not isinstance(conversation, Conversation):
        return True
    db = object_session(conversation)
    if db is None:
        return True

    resolved_at = db.scalar(
        select(HandoffRequest.resolved_at)
        .where(
            HandoffRequest.workspace_id == conversation.workspace_id,
            HandoffRequest.conversation_id == conversation.id,
            HandoffRequest.status == "resolved",
            HandoffRequest.resolved_at.is_not(None),
        )
        .order_by(HandoffRequest.resolved_at.desc())
        .limit(1)
    )
    if not isinstance(resolved_at, datetime):
        return True

    inbound_created_at = db.scalar(
        select(Message.created_at)
        .join(ChannelInboundEvent, ChannelInboundEvent.message_id == Message.id)
        .where(
            ChannelInboundEvent.workspace_id == conversation.workspace_id,
            ChannelInboundEvent.processing_token.is_not(None),
            ChannelInboundEvent.processing_lease_expires_at > datetime.now(UTC),
            Message.workspace_id == conversation.workspace_id,
            Message.conversation_id == conversation.id,
            Message.sender_type == "patient",
            Message.direction == "inbound",
        )
        .order_by(ChannelInboundEvent.updated_at.desc())
        .limit(1)
    )
    if not isinstance(inbound_created_at, datetime):
        return True
    return _as_utc(inbound_created_at) > _as_utc(resolved_at)


def ai_dispatch_is_sendable(
    *,
    conversation: Conversation,
    message: Message,
    active_handoff: HandoffRequest | None,
) -> bool:
    """Return whether an AI outbound still has authority to reach the provider."""
    if message.sender_type != "ai":
        return True

    metadata = message.metadata_json or {}
    if metadata.get("handoff_continuation") is True:
        expected_handoff_id = str(metadata.get("handoff_id") or "")
        return (
            bool(expected_handoff_id)
            and conversation.owner_type == OWNER_HUMAN
            and conversation.status == "pending"
            and active_handoff is not None
            and active_handoff.source == "ai"
            and active_handoff.status == "pending"
            and active_handoff.assigned_user_id is None
            and str(active_handoff.id) == expected_handoff_id
        )

    if metadata.get("handoff_ack") is True:
        return (
            conversation.owner_type == OWNER_HUMAN
            and conversation.status == "pending"
            and active_handoff is not None
            and active_handoff.source == "ai"
            and active_handoff.status == "pending"
            and active_handoff.assigned_user_id is None
        )

    # Conversational replies belong to the ownership epoch in which they were
    # created. A queued reply from before a takeover/hand-back must never become
    # sendable again. Templates and independent automations do not carry this
    # in-reply-to marker and are intentionally unaffected.
    if metadata.get("in_reply_to_message_id"):
        created_at = getattr(message, "created_at", None)
        ownership_changed_at = getattr(conversation, "ownership_changed_at", None)
        if isinstance(created_at, datetime) and isinstance(ownership_changed_at, datetime):
            if _as_utc(created_at) <= _as_utc(ownership_changed_at):
                return False

    return agent_can_reply(conversation) and active_handoff is None


def cancel_dispatch_for_ownership(
    dispatch: MessageDispatch,
    message: Message,
    *,
    reason: str,
) -> None:
    """Cancel an outbound row that became invalid after an ownership change."""
    dispatch.status = "cancelled"
    dispatch.last_error = reason[:2000]
    dispatch.next_attempt_at = None
    dispatch.locked_at = None
    dispatch.metadata_json = {
        **(dispatch.metadata_json or {}),
        "cancelled_by_ownership": True,
        "cancel_reason": reason[:500],
    }
    message.delivery_status = "cancelled"
    message.metadata_json = {
        **(message.metadata_json or {}),
        "cancelled_by_ownership": True,
        "cancel_reason": reason[:500],
    }


def _fresh_processing_dispatch(
    dispatch: MessageDispatch,
    *,
    now: datetime,
) -> bool:
    return (
        dispatch.status == "processing"
        and dispatch.locked_at is not None
        and dispatch.locked_at > now - DISPATCH_SEND_LEASE
    )


def quiesce_ai_dispatches_for_human(
    db: Session,
    *,
    conversation: Conversation,
    now: datetime | None = None,
    allow_inflight: bool = False,
) -> int:
    """Cancel queued/stale AI sends before staff can answer.

    Callers must lock the conversation first. A fresh `processing` dispatch is a
    short provider-send lease: staff cannot race it. AI-triggered handoff creation
    may keep that lease alive, but claim/assign/reply must wait for it to finish.
    """
    current = now or _now()
    rows = list(
        db.execute(
            select(MessageDispatch, Message)
            .join(Message, Message.id == MessageDispatch.message_id)
            .where(
                Message.workspace_id == conversation.workspace_id,
                Message.conversation_id == conversation.id,
                Message.sender_type == "ai",
                Message.direction == "outbound",
                MessageDispatch.status.in_(("queued", "processing")),
            )
            .with_for_update()
        )
    )

    cancelled = 0
    for dispatch, message in rows:
        if _fresh_processing_dispatch(dispatch, now=current):
            if allow_inflight:
                continue
            raise OwnershipTransitionBlockedError(
                "An AI reply is already being delivered. Retry after provider delivery completes."
            )
        cancel_dispatch_for_ownership(
            dispatch,
            message,
            reason="Cancelled because the conversation is moving to human ownership.",
        )
        cancelled += 1
    return cancelled


def ensure_staff_outbox_drained_before_ai(
    db: Session,
    *,
    conversation: Conversation,
) -> None:
    """Do not resume AI while a human reply is still queued or being sent."""
    pending = db.scalar(
        select(MessageDispatch.id)
        .join(Message, Message.id == MessageDispatch.message_id)
        .where(
            Message.workspace_id == conversation.workspace_id,
            Message.conversation_id == conversation.id,
            Message.sender_type == "staff",
            Message.direction == "outbound",
            MessageDispatch.status.in_(("queued", "processing")),
        )
        .limit(1)
    )
    if pending is not None:
        raise OwnershipTransitionBlockedError(
            "A staff reply is still waiting for provider delivery. Retry after it is sent."
        )


def _now() -> datetime:
    return datetime.now(UTC)


def agent_can_reply(conversation: Conversation) -> bool:
    """Return whether this AI run still owns a valid customer-triggered turn.

    The ownership epoch is captured when the turn first checks authority. If
    ownership changes while that run is thinking, a final refreshed ownership
    check sees a different persisted timestamp and suppresses the stale reply even
    if staff has already handed the conversation back to AI.
    """
    if conversation.owner_type != OWNER_AI or conversation.status != "open":
        return False
    if not _channel_turn_is_after_latest_handback(conversation):
        return False

    ownership_changed_at = getattr(conversation, "ownership_changed_at", None)
    run_epoch = getattr(conversation, _AGENT_OWNERSHIP_EPOCH_ATTR, None)
    if run_epoch is None:
        if isinstance(ownership_changed_at, datetime):
            setattr(conversation, _AGENT_OWNERSHIP_EPOCH_ATTR, ownership_changed_at)
        return True
    if not isinstance(run_epoch, datetime) or not isinstance(ownership_changed_at, datetime):
        return False
    return _as_utc(run_epoch) == _as_utc(ownership_changed_at)


def transfer_to_human(
    conversation: Conversation,
    *,
    assigned_user_id: UUID | None = None,
    now: datetime | None = None,
) -> None:
    conversation.owner_type = OWNER_HUMAN
    conversation.assigned_user_id = assigned_user_id
    conversation.status = "pending"
    conversation.closed_at = None
    conversation.ownership_changed_at = now or _now()


def return_to_ai(
    conversation: Conversation,
    *,
    close: bool = False,
    now: datetime | None = None,
) -> None:
    changed_at = now or _now()
    conversation.owner_type = OWNER_AI
    conversation.assigned_user_id = None
    conversation.status = "closed" if close else "open"
    conversation.closed_at = changed_at if close else None
    conversation.ownership_changed_at = changed_at


def record_customer_inbound(
    conversation: Conversation,
    *,
    now: datetime | None = None,
) -> None:
    conversation.unread_count = max(0, int(conversation.unread_count or 0)) + 1
    conversation.last_message_at = now or _now()
    ownership_changed_at = getattr(conversation, "ownership_changed_at", None)
    if isinstance(ownership_changed_at, datetime):
        setattr(conversation, _AGENT_OWNERSHIP_EPOCH_ATTR, ownership_changed_at)


def record_outbound_activity(
    conversation: Conversation,
    *,
    now: datetime | None = None,
) -> None:
    conversation.last_message_at = now or _now()


def mark_conversation_read(conversation: Conversation) -> None:
    conversation.unread_count = 0


def lock_conversation_ownership(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
) -> Conversation | None:
    """Reload and lock a conversation before an ownership-sensitive write.

    `populate_existing=True` is deliberate: the same SQLAlchemy session may have
    loaded the Conversation before an LLM call. A staff member can take over
    during that latency, so the final AI write must refresh ownership from the DB.
    """
    stmt = (
        select(Conversation)
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.id == conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.scalar(stmt)