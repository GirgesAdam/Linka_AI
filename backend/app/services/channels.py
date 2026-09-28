from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.channel_adapter import (
    channel_to_patient_source,
    hash_adapter_token,
)
from app.core.channel_delivery import apply_provider_delivery_status
from app.core.config import settings
from app.models.channel_connection import ChannelConnection
from app.models.channel_delivery_event import ChannelDeliveryEvent
from app.models.channel_identity import ChannelIdentity
from app.models.channel_inbound_event import ChannelInboundEvent
from app.models.conversation import Conversation
from app.models.handoff_request import HandoffRequest
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.models.patient import Patient
from app.models.workspace import Workspace
from app.schemas.agent import AgentChatResponse
from app.schemas.channel import DispatchClaimItem, NormalizedInboundMessage
from app.schemas.crm import normalize_phone
from app.services.agent_v2.live_chat import run_agent_for_existing_inbound
from app.services.conversation_ownership import (
    DISPATCH_SEND_LEASE,
    OWNER_HUMAN,
    ai_dispatch_is_sendable,
    cancel_dispatch_for_ownership,
    record_customer_inbound,
    return_to_ai,
)
from app.services.crm_campaigns import (
    guard_campaign_dispatch_before_claim,
    reconcile_campaign_dispatch,
)
from app.services.crm_tasks import reconcile_ai_followup_dispatch
from app.services.whatsapp_interactions import (
    process_whatsapp_booking_action,
    whatsapp_booking_dispatch_metadata,
)
from app.services.workspace_runtime_policy import (
    demo_whatsapp_reply_test_enabled,
    workspace_runtime_policy,
)


class ChannelError(ValueError):
    pass


class ChannelConflictError(ChannelError):
    pass


META_CONNECTION_PAUSE_ERROR_CODES = frozenset({131031})


def _meta_error_details(metadata: dict) -> tuple[int | None, str | None]:
    raw_errors = metadata.get("errors") if isinstance(metadata, dict) else None
    if not isinstance(raw_errors, list):
        return None, None
    for raw in raw_errors:
        if not isinstance(raw, dict):
            continue
        raw_code = raw.get("code")
        try:
            code = int(raw_code) if raw_code is not None else None
        except (TypeError, ValueError):
            code = None
        details = raw.get("title") or raw.get("message")
        error_data = raw.get("error_data")
        if not details and isinstance(error_data, dict):
            details = error_data.get("details")
        return code, str(details)[:2000] if details else None
    return None, None


def _provider_failure_is_permanent(error: str | None, metadata: dict) -> bool:
    code, details = _meta_error_details(metadata)
    text = " ".join(part for part in (error, details) if part).lower()
    return code in META_CONNECTION_PAUSE_ERROR_CODES or "business account locked" in text


def _record_connection_provider_health(
    connection: ChannelConnection,
    *,
    provider_status: str,
    error: str | None,
    metadata: dict,
    occurred_at: datetime | None = None,
) -> bool:
    now = occurred_at or datetime.now(UTC)
    config = dict(connection.config_json or {})
    raw_health = config.get("provider_health")
    health = dict(raw_health) if isinstance(raw_health, dict) else {}
    permanent = False

    if provider_status in {"sent", "delivered", "read"}:
        health["last_accepted_at"] = now.isoformat()
        if provider_status in {"delivered", "read"}:
            health["last_delivery_at"] = now.isoformat()
        if connection.status == "active":
            health["state"] = "healthy"
            health["current_error"] = None
            health["current_error_code"] = None
            health.pop("action_required", None)
    elif provider_status == "failed":
        code, details = _meta_error_details(metadata)
        failure = (error or details or "Provider reported message delivery failure.")[:2000]
        permanent = _provider_failure_is_permanent(failure, metadata)
        health["last_error_at"] = now.isoformat()
        health["current_error"] = failure
        health["current_error_code"] = code
        if permanent:
            connection.status = "paused"
            health["state"] = "disabled"
            health["action_required"] = "meta_account_review"
        elif connection.status == "active":
            health["state"] = "degraded"

    config["provider_health"] = health
    connection.config_json = config
    return permanent


@dataclass(frozen=True)
class AcceptedInbound:
    event: ChannelInboundEvent
    message: Message
    patient: Patient
    conversation: Conversation
    duplicate: bool


@dataclass(frozen=True)
class ProcessedInbound:
    event: ChannelInboundEvent
    agent_response: AgentChatResponse
    dispatch: MessageDispatch | None


@dataclass(frozen=True)
class RecordedProviderStatus:
    event: ChannelDeliveryEvent
    dispatch: MessageDispatch | None
    duplicate: bool


def get_connection_by_adapter_token(
    db: Session,
    raw_token: str,
) -> ChannelConnection | None:
    """
    Authenticate a channel adapter using the raw X-Channel-Token.

    Only the SHA-256 hash is stored in PostgreSQL. Paused connections may
    still authenticate so in-flight provider delivery callbacks can be recorded;
    inbound processing and new outbox claims remain guarded by _require_active.
    """
    token = raw_token.strip() if isinstance(raw_token, str) else ""
    if not token:
        return None

    return db.scalar(
        select(ChannelConnection).where(
            ChannelConnection.adapter_token_hash == hash_adapter_token(token),
            ChannelConnection.status.in_(("active", "paused")),
        )
    )



def _patient_display_name(payload: NormalizedInboundMessage, channel: str) -> str:
    if payload.display_name:
        return payload.display_name[:120]
    if payload.phone:
        return payload.phone[:120]
    return f"عميل {channel}"[:120]


def _resolve_patient_for_new_identity(
    db: Session,
    *,
    connection: ChannelConnection,
    payload: NormalizedInboundMessage,
) -> Patient:
    display_phone, normalized_phone = normalize_phone(payload.phone)
    patient = None
    if normalized_phone:
        patient = db.scalar(
            select(Patient).where(
                Patient.workspace_id == connection.workspace_id,
                Patient.phone_normalized == normalized_phone,
            )
        )


    if patient is not None:
        if patient.phone is None and display_phone:
            patient.phone = display_phone
            patient.phone_normalized = normalized_phone
        return patient

    patient = Patient(
        workspace_id=connection.workspace_id,
        first_name=_patient_display_name(payload, connection.channel),
        last_name=None,
        phone=display_phone,
        phone_normalized=normalized_phone,
        preferred_language="ar",
        source=channel_to_patient_source(connection.channel),
        source_detail=f"{connection.provider}:{connection.display_name}"[:200],
        status="active",
        marketing_consent=False,
    )
    db.add(patient)
    db.flush()
    return patient


def _resolve_identity(
    db: Session,
    *,
    connection: ChannelConnection,
    payload: NormalizedInboundMessage,
) -> tuple[ChannelIdentity, Patient]:
    identity = db.scalar(
        select(ChannelIdentity).where(
            ChannelIdentity.workspace_id == connection.workspace_id,
            ChannelIdentity.channel_connection_id == connection.id,
            ChannelIdentity.external_user_id == payload.external_user_id,
        )
    )

    if identity is not None:
        patient = db.scalar(
            select(Patient).where(
                Patient.workspace_id == connection.workspace_id,
                Patient.id == identity.patient_id,
            )
        )
        if patient is None:
            raise ChannelError("Channel identity references a missing patient.")

        if payload.display_name:
            identity.display_name = payload.display_name
        if payload.phone:
            identity.phone = payload.phone
        identity.metadata_json = {**(identity.metadata_json or {}), **payload.metadata}
        return identity, patient

    patient = _resolve_patient_for_new_identity(
        db,
        connection=connection,
        payload=payload,
    )
    identity = ChannelIdentity(
        workspace_id=connection.workspace_id,
        channel_connection_id=connection.id,
        patient_id=patient.id,
        external_user_id=payload.external_user_id,
        display_name=payload.display_name,
        phone=payload.phone,
        metadata_json=payload.metadata,
    )
    db.add(identity)
    db.flush()
    return identity, patient


def _resolve_conversation(
    db: Session,
    *,
    connection: ChannelConnection,
    patient: Patient,
    payload: NormalizedInboundMessage,
) -> Conversation:
    external_conversation_id = payload.external_conversation_id or payload.external_user_id
    conversation = db.scalar(
        select(Conversation)
        .where(
            Conversation.workspace_id == connection.workspace_id,
            Conversation.channel_connection_id == connection.id,
            Conversation.external_conversation_id == external_conversation_id,
        )
        .with_for_update()
    )

    now = datetime.now(UTC)
    if conversation is not None:
        if conversation.patient_id != patient.id:
            raise ChannelConflictError(
                "External conversation is already linked to another patient."
            )
        if conversation.status == "closed":
            return_to_ai(conversation, now=now)
        return conversation

    conversation = Conversation(
        workspace_id=connection.workspace_id,
        patient_id=patient.id,
        channel=connection.channel,
        channel_connection_id=connection.id,
        external_conversation_id=external_conversation_id,
        status="open",
        owner_type="ai",
        unread_count=0,
        ownership_changed_at=now,
        started_at=now,
        last_message_at=now,
    )
    db.add(conversation)
    db.flush()
    return conversation


def _next_conversation_activity_at(conversation: Conversation) -> datetime:
    """Return a strictly increasing local activity timestamp for one conversation."""
    now = datetime.now(UTC)
    previous = conversation.last_message_at
    if previous is None:
        return now
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    else:
        previous = previous.astimezone(UTC)
    return max(now, previous + timedelta(microseconds=1))


def accept_normalized_inbound(
    db: Session,
    *,
    connection: ChannelConnection,
    payload: NormalizedInboundMessage,
) -> AcceptedInbound:
    existing_event = db.scalar(
        select(ChannelInboundEvent).where(
            ChannelInboundEvent.channel_connection_id == connection.id,
            ChannelInboundEvent.external_event_id == payload.external_event_id,
        )
    )
    if existing_event is not None:
        message = db.get(Message, existing_event.message_id)
        if message is None:
            raise ChannelError("Existing inbound event references a missing message.")
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.workspace_id == connection.workspace_id,
                Conversation.id == message.conversation_id,
            )
        )
        patient = db.get(Patient, conversation.patient_id) if conversation is not None else None
        if conversation is None or patient is None:
            raise ChannelError("Existing inbound event references missing CRM data.")
        return AcceptedInbound(
            event=existing_event,
            message=message,
            patient=patient,
            conversation=conversation,
            duplicate=True,
        )

    existing_message = db.scalar(
        select(Message).where(
            Message.workspace_id == connection.workspace_id,
            Message.channel_connection_id == connection.id,
            Message.external_message_id == payload.external_message_id,
        )
    )
    if existing_message is not None:
        event = db.scalar(
            select(ChannelInboundEvent).where(ChannelInboundEvent.message_id == existing_message.id)
        )
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.workspace_id == connection.workspace_id,
                Conversation.id == existing_message.conversation_id,
            )
        )
        patient = db.get(Patient, conversation.patient_id) if conversation is not None else None
        if event is None or conversation is None or patient is None:
            raise ChannelConflictError(
                "External message id already exists but cannot be safely replayed."
            )
        return AcceptedInbound(
            event=event,
            message=existing_message,
            patient=patient,
            conversation=conversation,
            duplicate=True,
        )

    _, patient = _resolve_identity(
        db,
        connection=connection,
        payload=payload,
    )
    if connection.channel == "whatsapp" and not patient.whatsapp_opt_in:
        patient.whatsapp_opt_in = True
        patient.whatsapp_opt_in_at = datetime.now(UTC)
        patient.whatsapp_opt_in_source = "customer_inbound"
    conversation = _resolve_conversation(
        db,
        connection=connection,
        patient=patient,
        payload=payload,
    )

    now = _next_conversation_activity_at(conversation)
    inbound = Message(
        workspace_id=connection.workspace_id,
        conversation_id=conversation.id,
        channel_connection_id=connection.id,
        sender_type="patient",
        direction="inbound",
        message_type=payload.message_type,
        content=payload.text,
        external_message_id=payload.external_message_id,
        delivery_status="received",
        created_at=now,
        metadata_json={
            "source": "channel_adapter",
            "provider": connection.provider,
            "external_event_id": payload.external_event_id,
            "external_user_id": payload.external_user_id,
            **payload.metadata,
        },
    )
    record_customer_inbound(conversation, now=now)
    patient.last_contact_at = now
    db.add(inbound)
    db.flush()

    event = ChannelInboundEvent(
        workspace_id=connection.workspace_id,
        channel_connection_id=connection.id,
        message_id=inbound.id,
        external_event_id=payload.external_event_id,
        status="received",
        attempts=0,
        payload_json=payload.model_dump(mode="json"),
    )
    db.add(event)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ChannelConflictError(
            "Inbound event collided with an existing provider event/message id."
        ) from exc

    db.refresh(event)
    db.refresh(inbound)
    db.refresh(patient)
    db.refresh(conversation)
    return AcceptedInbound(
        event=event,
        message=inbound,
        patient=patient,
        conversation=conversation,
        duplicate=False,
    )


def queue_message_dispatch(
    db: Session,
    *,
    message: Message,
    conversation: Conversation,
    commit: bool = True,
) -> MessageDispatch | None:
    connection_id = message.channel_connection_id or conversation.channel_connection_id
    if connection_id is None:
        return None
    if message.direction != "outbound":
        raise ChannelError("Only outbound messages can be queued for dispatch.")

    existing = db.scalar(
        select(MessageDispatch).where(
            MessageDispatch.workspace_id == message.workspace_id,
            MessageDispatch.message_id == message.id,
        )
    )
    if existing is not None:
        return existing

    message.channel_connection_id = connection_id
    message.delivery_status = "queued"
    dispatch = MessageDispatch(
        workspace_id=message.workspace_id,
        channel_connection_id=connection_id,
        message_id=message.id,
        status="queued",
        attempts=0,
        metadata_json={
            "conversation_id": str(conversation.id),
            "sender_type": message.sender_type,
        },
    )
    db.add(dispatch)
    if commit:
        db.commit()
        db.refresh(dispatch)
        db.refresh(message)
    else:
        db.flush()
    return dispatch


def _processed_event_response(
    db: Session,
    *,
    event: ChannelInboundEvent,
) -> ProcessedInbound | None:
    if event.status != "processed":
        return None

    inbound = db.get(Message, event.message_id)
    if inbound is None:
        raise ChannelError("Processed inbound event references a missing message.")
    conversation = db.get(Conversation, inbound.conversation_id)
    if conversation is None:
        raise ChannelError("Processed inbound event references a missing conversation.")

    outbound = db.get(Message, event.outbound_message_id) if event.outbound_message_id else None
    dispatch = None
    if outbound is not None:
        dispatch = db.scalar(
            select(MessageDispatch).where(MessageDispatch.message_id == outbound.id)
        )

    response = AgentChatResponse(
        run_id=UUID(str((inbound.metadata_json or {}).get("agent_run_id")))
        if (inbound.metadata_json or {}).get("agent_run_id")
        else UUID(int=0),
        conversation_id=conversation.id,
        inbound_message_id=inbound.id,
        outbound_message_id=outbound.id if outbound else None,
        reply=outbound.content if outbound else None,
        handoff_required=conversation.owner_type == OWNER_HUMAN,
        agent_paused=outbound is None and conversation.owner_type == OWNER_HUMAN,
        model=(outbound.metadata_json or {}).get("model") if outbound else None,
    )
    return ProcessedInbound(event=event, agent_response=response, dispatch=dispatch)


MAX_INBOUND_PROCESS_ATTEMPTS = 3
INBOUND_AGENT_LEASE = timedelta(minutes=5)


class InboundProcessingLeaseLost(ChannelConflictError):
    pass


def _lease_is_active(event: ChannelInboundEvent, *, now: datetime) -> bool:
    return (
        event.processing_token is not None
        and event.processing_lease_expires_at is not None
        and event.processing_lease_expires_at > now
    )


def _conversation_lease_is_active(conversation: Conversation, *, now: datetime) -> bool:
    return (
        conversation.agent_processing_token is not None
        and conversation.agent_processing_lease_expires_at is not None
        and conversation.agent_processing_lease_expires_at > now
    )


def _event_context(
    db: Session, *, event: ChannelInboundEvent
) -> tuple[Message, Conversation]:
    inbound = db.get(Message, event.message_id)
    if inbound is None:
        raise ChannelError("Inbound event references a missing message.")
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.workspace_id == event.workspace_id,
            Conversation.id == inbound.conversation_id,
        )
    )
    if conversation is None:
        raise ChannelError("Inbound event references a missing conversation.")
    return inbound, conversation


def _earliest_retryable_inbound_event_id(
    db: Session,
    *,
    workspace_id: UUID,
    channel_connection_id: UUID,
    conversation_id: UUID,
) -> UUID | None:
    now = datetime.now(UTC)
    return db.scalar(
        select(ChannelInboundEvent.id)
        .join(Message, Message.id == ChannelInboundEvent.message_id)
        .where(
            ChannelInboundEvent.workspace_id == workspace_id,
            ChannelInboundEvent.channel_connection_id == channel_connection_id,
            ChannelInboundEvent.status.in_(("received", "failed")),
            or_(
                ChannelInboundEvent.attempts < MAX_INBOUND_PROCESS_ATTEMPTS,
                and_(
                    ChannelInboundEvent.processing_token.is_not(None),
                    or_(
                        ChannelInboundEvent.processing_lease_expires_at.is_(None),
                        ChannelInboundEvent.processing_lease_expires_at <= now,
                    ),
                ),
            ),
            Message.conversation_id == conversation_id,
            Message.sender_type == "patient",
            Message.direction == "inbound",
        )
        .order_by(
            Message.created_at,
            Message.id,
            ChannelInboundEvent.created_at,
            ChannelInboundEvent.id,
        )
        .limit(1)
    )


def _lock_event_and_conversation(
    db: Session, *, event_id: UUID
) -> tuple[ChannelInboundEvent, Conversation]:
    event_ref = db.scalar(
        select(ChannelInboundEvent).where(ChannelInboundEvent.id == event_id)
    )
    if event_ref is None:
        raise InboundProcessingLeaseLost("Inbound processing event no longer exists.")
    inbound = db.get(Message, event_ref.message_id)
    if inbound is None:
        raise InboundProcessingLeaseLost("Inbound processing message no longer exists.")
    conversation = db.scalar(
        select(Conversation)
        .where(
            Conversation.workspace_id == event_ref.workspace_id,
            Conversation.id == inbound.conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise InboundProcessingLeaseLost("Inbound processing conversation no longer exists.")
    event = db.scalar(
        select(ChannelInboundEvent)
        .where(ChannelInboundEvent.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None:
        raise InboundProcessingLeaseLost("Inbound processing event no longer exists.")
    return event, conversation


def _claim_inbound_event(
    db: Session,
    *,
    connection: ChannelConnection,
    event_id: UUID,
) -> tuple[ChannelInboundEvent, UUID, ProcessedInbound | None]:
    event_ref = db.scalar(
        select(ChannelInboundEvent).where(
            ChannelInboundEvent.id == event_id,
            ChannelInboundEvent.workspace_id == connection.workspace_id,
            ChannelInboundEvent.channel_connection_id == connection.id,
        )
    )
    if event_ref is None:
        raise ChannelError("Inbound event not found for this channel connection.")
    inbound, conversation_ref = _event_context(db, event=event_ref)
    conversation = db.scalar(
        select(Conversation)
        .where(
            Conversation.workspace_id == connection.workspace_id,
            Conversation.id == conversation_ref.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise ChannelError("Inbound event references a missing conversation.")
    event = db.scalar(
        select(ChannelInboundEvent)
        .where(
            ChannelInboundEvent.id == event_id,
            ChannelInboundEvent.workspace_id == connection.workspace_id,
            ChannelInboundEvent.channel_connection_id == connection.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None:
        raise ChannelError("Inbound event not found for this channel connection.")

    already_processed = _processed_event_response(db, event=event)
    if already_processed is not None:
        db.rollback()
        return event, UUID(int=0), already_processed

    now = datetime.now(UTC)
    if _conversation_lease_is_active(conversation, now=now):
        db.rollback()
        raise ChannelConflictError("Conversation already has an active agent processing lease.")
    if _lease_is_active(event, now=now):
        db.rollback()
        raise ChannelConflictError("Inbound event already has an active processing lease.")

    if event.status == "processing":
        event.status = "failed"
    if event.status not in {"received", "failed"}:
        db.rollback()
        raise ChannelConflictError("Inbound event is not retryable.")
    reclaiming_expired_lease = (
        event.processing_token is not None and not _lease_is_active(event, now=now)
    )
    if event.attempts >= MAX_INBOUND_PROCESS_ATTEMPTS and not reclaiming_expired_lease:
        db.rollback()
        raise ChannelConflictError("Inbound event exhausted its processing retry budget.")

    earliest_id = _earliest_retryable_inbound_event_id(
        db,
        workspace_id=connection.workspace_id,
        channel_connection_id=connection.id,
        conversation_id=inbound.conversation_id,
    )
    if earliest_id != event.id:
        db.rollback()
        raise ChannelConflictError("An older inbound turn must be processed first.")

    token = uuid4()
    expires_at = now + INBOUND_AGENT_LEASE
    event.processing_token = token
    event.processing_started_at = now
    event.processing_lease_expires_at = expires_at
    event.attempts += 1
    event.last_error = None
    conversation.agent_processing_token = token
    conversation.agent_processing_started_at = now
    conversation.agent_processing_lease_expires_at = expires_at
    db.commit()
    return event, token, None


def _assert_inbound_lease(
    db: Session, *, event_id: UUID, token: UUID
) -> tuple[ChannelInboundEvent, Conversation]:
    event, conversation = _lock_event_and_conversation(db, event_id=event_id)
    now = datetime.now(UTC)
    if (
        event.processing_token != token
        or event.processing_lease_expires_at is None
        or event.processing_lease_expires_at <= now
        or conversation.agent_processing_token != token
        or conversation.agent_processing_lease_expires_at is None
        or conversation.agent_processing_lease_expires_at <= now
    ):
        raise InboundProcessingLeaseLost("Inbound processing lease is no longer owned.")
    return event, conversation


@contextmanager
def _fence_inbound_commits(db: Session, *, event_id: UUID, token: UUID):
    def before_commit(session: Session) -> None:
        _assert_inbound_lease(session, event_id=event_id, token=token)

    sa_event.listen(db, "before_commit", before_commit)
    try:
        yield
    finally:
        sa_event.remove(db, "before_commit", before_commit)


def _correlated_agent_response(
    db: Session,
    *,
    conversation: Conversation,
    inbound: Message,
) -> AgentChatResponse | None:
    outbound = db.scalar(
        select(Message).where(
            Message.workspace_id == conversation.workspace_id,
            Message.conversation_id == conversation.id,
            Message.in_reply_to_message_id == inbound.id,
            Message.sender_type == "ai",
            Message.direction == "outbound",
        )
    )
    if outbound is None:
        return None
    metadata = outbound.metadata_json or {}
    try:
        run_id = UUID(str(metadata.get("agent_run_id")))
    except (TypeError, ValueError):
        run_id = UUID(int=0)
    return AgentChatResponse(
        run_id=run_id,
        conversation_id=conversation.id,
        inbound_message_id=inbound.id,
        outbound_message_id=outbound.id,
        reply=outbound.content,
        handoff_required=conversation.owner_type == OWNER_HUMAN,
        agent_paused=False,
        model=metadata.get("model"),
    )


def _mark_inbound_failed_if_owned(
    db: Session,
    *,
    event_id: UUID,
    token: UUID,
    error: Exception,
) -> bool:
    try:
        failed_event, conversation = _lock_event_and_conversation(db, event_id=event_id)
    except InboundProcessingLeaseLost:
        db.rollback()
        return False
    if (
        failed_event.processing_token != token
        or conversation.agent_processing_token != token
    ):
        db.rollback()
        return False
    failed_event.status = "failed"
    failed_event.last_error = f"{type(error).__name__}: {error}"[:2000]
    failed_event.processing_token = None
    failed_event.processing_started_at = None
    failed_event.processing_lease_expires_at = None
    conversation.agent_processing_token = None
    conversation.agent_processing_started_at = None
    conversation.agent_processing_lease_expires_at = None
    db.commit()
    return True


def process_inbound_event(
    db: Session,
    *,
    connection: ChannelConnection,
    event_id: UUID,
) -> ProcessedInbound:
    event, token, already_processed = _claim_inbound_event(
        db,
        connection=connection,
        event_id=event_id,
    )
    if already_processed is not None:
        return already_processed

    try:
        inbound = db.get(Message, event.message_id)
        if inbound is None:
            raise ChannelError("Inbound event references a missing message.")
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.workspace_id == connection.workspace_id,
                Conversation.id == inbound.conversation_id,
            )
        )
        if conversation is None:
            raise ChannelError("Inbound event references a missing conversation.")
        patient = db.scalar(
            select(Patient).where(
                Patient.workspace_id == connection.workspace_id,
                Patient.id == conversation.patient_id,
            )
        )
        workspace = db.get(Workspace, connection.workspace_id)
        if patient is None or workspace is None:
            raise ChannelError("Inbound event references missing workspace CRM data.")
        if inbound.sender_type != "patient" or inbound.direction != "inbound":
            raise ChannelError("Inbound event must reference a patient inbound message.")

        with _fence_inbound_commits(db, event_id=event_id, token=token):
            agent_response = _correlated_agent_response(
                db,
                conversation=conversation,
                inbound=inbound,
            )
            if agent_response is None and connection.channel == "whatsapp":
                agent_response = process_whatsapp_booking_action(
                    db,
                    workspace=workspace,
                    patient=patient,
                    conversation=conversation,
                    inbound=inbound,
                )
            if agent_response is None:
                agent_response = run_agent_for_existing_inbound(
                    db=db,
                    workspace=workspace,
                    patient=patient,
                    conversation=conversation,
                    inbound=inbound,
                )

        # This row lock and ownership check are in the same short transaction as
        # dispatch/event finalization. No lock is held while the LLM is running.
        event, processing_conversation = _assert_inbound_lease(
            db, event_id=event_id, token=token
        )
        if processing_conversation.id != conversation.id:
            raise ChannelError("Inbound processing lease belongs to another conversation.")
        dispatch = None
        if agent_response.outbound_message_id is not None:
            outbound = db.get(Message, agent_response.outbound_message_id)
            if outbound is None:
                raise ChannelError("Agent created an outbound id but the message is missing.")
            if (
                outbound.workspace_id != inbound.workspace_id
                or outbound.conversation_id != inbound.conversation_id
                or outbound.sender_type != "ai"
                or outbound.direction != "outbound"
            ):
                raise ChannelError("Agent outbound does not match the inbound conversation.")
            if outbound.in_reply_to_message_id != inbound.id:
                raise ChannelError("Agent outbound is missing the durable inbound reply correlation.")
            dispatch = queue_message_dispatch(
                db,
                message=outbound,
                conversation=conversation,
                commit=False,
            )
            event.outbound_message_id = outbound.id

        now = datetime.now(UTC)
        if (
            event.processing_lease_expires_at is None
            or event.processing_lease_expires_at <= now
            or processing_conversation.agent_processing_lease_expires_at is None
            or processing_conversation.agent_processing_lease_expires_at <= now
        ):
            raise InboundProcessingLeaseLost(
                "Inbound or conversation processing lease expired before finalization."
            )
        event.status = "processed"
        event.last_error = None
        event.processing_token = None
        event.processing_started_at = None
        event.processing_lease_expires_at = None
        processing_conversation.agent_processing_token = None
        processing_conversation.agent_processing_started_at = None
        processing_conversation.agent_processing_lease_expires_at = None
        db.commit()
        db.refresh(event)
        if dispatch is not None:
            db.refresh(dispatch)
        return ProcessedInbound(event=event, agent_response=agent_response, dispatch=dispatch)
    except Exception as exc:
        db.rollback()
        _mark_inbound_failed_if_owned(db, event_id=event_id, token=token, error=exc)
        raise

def _dispatch_has_retry_budget(dispatch: MessageDispatch) -> bool:
    return dispatch.attempts < settings.channel_dispatch_max_attempts


def _mark_dispatch_failed(
    dispatch: MessageDispatch,
    message: Message | None,
    *,
    error: str,
) -> None:
    dispatch.status = "failed"
    dispatch.last_error = error[:2000]
    dispatch.next_attempt_at = None
    dispatch.locked_at = None
    if message is not None:
        message.delivery_status = "failed"


def _fail_exhausted_dispatches(
    db: Session,
    *,
    connection: ChannelConnection,
    now: datetime,
    stale_before: datetime,
) -> None:
    exhausted = list(
        db.scalars(
            select(MessageDispatch)
            .where(
                MessageDispatch.workspace_id == connection.workspace_id,
                MessageDispatch.channel_connection_id == connection.id,
                MessageDispatch.attempts >= settings.channel_dispatch_max_attempts,
                or_(
                    MessageDispatch.status == "queued",
                    and_(
                        MessageDispatch.status == "processing",
                        MessageDispatch.locked_at.is_not(None),
                        MessageDispatch.locked_at <= stale_before,
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
        )
    )

    for dispatch in exhausted:
        message = db.get(Message, dispatch.message_id)
        _mark_dispatch_failed(
            dispatch,
            message,
            error=(
                dispatch.last_error
                or f"Provider dispatch retry budget exhausted after {dispatch.attempts} attempts."
            ),
        )
        if message is not None:
            reconcile_ai_followup_dispatch(db, dispatch=dispatch, message=message)
            reconcile_campaign_dispatch(db, dispatch=dispatch, message=message)


def _whatsapp_dispatch_requires_opt_in(
    connection: ChannelConnection, message: Message
) -> bool:
    if connection.channel != "whatsapp":
        return False
    metadata = message.metadata_json or {}
    source = str(metadata.get("source") or "")
    return message.message_type == "template" or source in {
        "automation_engine",
        "ai_followup",
        "crm_campaign",
    }


def _whatsapp_template_name(message: Message) -> str | None:
    metadata = message.metadata_json or {}
    template = metadata.get("whatsapp_template")
    if not isinstance(template, dict):
        return None
    raw_name = template.get("name")
    if not isinstance(raw_name, str):
        return None
    return raw_name.strip() or None


def _template_dispatch_is_allowed(
    message: Message,
    *,
    approved_template_names: frozenset[str] | set[str] | None,
) -> bool:
    if message.message_type != "template" or approved_template_names is None:
        return True
    template_name = _whatsapp_template_name(message)
    # Malformed template metadata is intentionally claimed so the provider-payload
    # builder can fail it deterministically instead of leaving it queued forever.
    return template_name is None or template_name in approved_template_names


def _dispatch_is_claimable(
    dispatch: MessageDispatch,
    *,
    now: datetime,
    stale_before: datetime,
) -> bool:
    if dispatch.attempts >= settings.channel_dispatch_max_attempts:
        return False
    if dispatch.status == "queued":
        return dispatch.next_attempt_at is None or dispatch.next_attempt_at <= now
    return (
        dispatch.status == "processing"
        and dispatch.locked_at is not None
        and dispatch.locked_at <= stale_before
    )


def claim_dispatches(
    db: Session,
    *,
    connection: ChannelConnection,
    limit: int,
    allow_templates: bool = True,
    approved_template_names: frozenset[str] | set[str] | None = None,
    allow_demo_reply_dispatch: bool = False,
) -> list[DispatchClaimItem]:
    workspace = db.get(Workspace, connection.workspace_id)
    if workspace is None:
        raise ChannelError("Channel connection references a missing workspace.")
    demo_reply_test = allow_demo_reply_dispatch and demo_whatsapp_reply_test_enabled(
        workspace, connection
    )
    if (
        not workspace_runtime_policy(workspace).allow_external_dispatch
        and not demo_reply_test
    ):
        return []
    now = datetime.now(UTC)
    stale_before = now - DISPATCH_SEND_LEASE

    _fail_exhausted_dispatches(
        db,
        connection=connection,
        now=now,
        stale_before=stale_before,
    )

    # Read candidate ids first, then lock in canonical conversation -> dispatch
    # order. This matches staff takeover/claim and avoids a dispatch->conversation
    # deadlock while still allowing multiple outbox workers to skip busy rows.
    candidate_query = (
        select(MessageDispatch.id, Message.conversation_id)
        .join(Message, Message.id == MessageDispatch.message_id)
        .where(
            MessageDispatch.workspace_id == connection.workspace_id,
            MessageDispatch.channel_connection_id == connection.id,
            MessageDispatch.attempts < settings.channel_dispatch_max_attempts,
            or_(
                and_(
                    MessageDispatch.status == "queued",
                    or_(
                        MessageDispatch.next_attempt_at.is_(None),
                        MessageDispatch.next_attempt_at <= now,
                    ),
                ),
                and_(
                    MessageDispatch.status == "processing",
                    MessageDispatch.locked_at.is_not(None),
                    MessageDispatch.locked_at <= stale_before,
                ),
            ),
        )
        .order_by(MessageDispatch.created_at)
        .limit(min(max(limit * 4, limit), 200))
    )
    if demo_reply_test:
        candidate_query = candidate_query.where(Message.in_reply_to_message_id.is_not(None))
    candidate_rows = list(db.execute(candidate_query))

    claimed: list[DispatchClaimItem] = []
    for dispatch_id, conversation_id in candidate_rows:
        if len(claimed) >= limit:
            break

        conversation = db.scalar(
            select(Conversation)
            .where(
                Conversation.workspace_id == connection.workspace_id,
                Conversation.id == conversation_id,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if conversation is None:
            continue

        dispatch = db.scalar(
            select(MessageDispatch)
            .where(
                MessageDispatch.id == dispatch_id,
                MessageDispatch.workspace_id == connection.workspace_id,
                MessageDispatch.channel_connection_id == connection.id,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if dispatch is None or not _dispatch_is_claimable(
            dispatch,
            now=now,
            stale_before=stale_before,
        ):
            continue

        message = db.get(Message, dispatch.message_id)
        if message is None:
            dispatch.status = "failed"
            dispatch.last_error = "Outbound message no longer exists."
            continue

        if not allow_templates and message.message_type == "template":
            continue
        if not _template_dispatch_is_allowed(
            message,
            approved_template_names=approved_template_names,
        ):
            # Do not burn provider retry budget while Meta is still reviewing this
            # particular template. Deferring it also lets later approved/text
            # messages move through the bounded candidate window.
            dispatch.next_attempt_at = now + timedelta(minutes=2)
            continue

        if _whatsapp_dispatch_requires_opt_in(connection, message):
            patient = db.get(Patient, conversation.patient_id)
            if patient is None or not patient.whatsapp_opt_in:
                _mark_dispatch_failed(
                    dispatch,
                    message,
                    error="WhatsApp opt-in is required before proactive messaging.",
                )
                dispatch.metadata_json = {
                    **(dispatch.metadata_json or {}),
                    "policy_block": "whatsapp_opt_in_required",
                }
                reconcile_ai_followup_dispatch(db, dispatch=dispatch, message=message)
                reconcile_campaign_dispatch(db, dispatch=dispatch, message=message)
                continue

        # Only one provider send may be in flight per conversation. A later
        # dispatch waits for the earlier result instead of racing WhatsApp order.
        other_processing = db.scalar(
            select(MessageDispatch.id)
            .join(Message, Message.id == MessageDispatch.message_id)
            .where(
                Message.workspace_id == connection.workspace_id,
                Message.conversation_id == conversation.id,
                MessageDispatch.id != dispatch.id,
                MessageDispatch.status == "processing",
                MessageDispatch.locked_at.is_not(None),
                MessageDispatch.locked_at > stale_before,
            )
            .limit(1)
        )
        if other_processing is not None:
            continue

        active_handoff = None
        if message.sender_type == "ai":
            active_handoff = db.scalar(
                select(HandoffRequest).where(
                    HandoffRequest.workspace_id == connection.workspace_id,
                    HandoffRequest.conversation_id == conversation.id,
                    HandoffRequest.status.in_(("pending", "claimed")),
                )
            )
            if not ai_dispatch_is_sendable(
                conversation=conversation,
                message=message,
                active_handoff=active_handoff,
            ):
                cancel_dispatch_for_ownership(
                    dispatch,
                    message,
                    reason="Cancelled because AI no longer owns this conversation.",
                )
                reconcile_ai_followup_dispatch(db, dispatch=dispatch, message=message)
                continue

        if not guard_campaign_dispatch_before_claim(
            db, dispatch=dispatch, message=message, conversation=conversation
        ):
            continue

        identity = db.scalar(
            select(ChannelIdentity).where(
                ChannelIdentity.workspace_id == connection.workspace_id,
                ChannelIdentity.channel_connection_id == connection.id,
                ChannelIdentity.patient_id == conversation.patient_id,
            )
        )
        if identity is None:
            dispatch.status = "failed"
            dispatch.last_error = "No channel identity found for conversation patient."
            message.delivery_status = "failed"
            reconcile_ai_followup_dispatch(db, dispatch=dispatch, message=message)
            reconcile_campaign_dispatch(db, dispatch=dispatch, message=message)
            continue

        dispatch.status = "processing"
        dispatch.attempts += 1
        dispatch.locked_at = now
        dispatch.last_error = None
        dispatch.next_attempt_at = None

        claimed.append(
            DispatchClaimItem(
                dispatch_id=dispatch.id,
                message_id=message.id,
                channel=connection.channel,
                provider=connection.provider,
                external_account_id=connection.external_account_id,
                external_user_id=identity.external_user_id,
                external_conversation_id=(
                    conversation.external_conversation_id or identity.external_user_id
                ),
                message_type=message.message_type,
                content=message.content,
                metadata=(
                    whatsapp_booking_dispatch_metadata(db, message=message)
                    if connection.channel == "whatsapp"
                    else (message.metadata_json or {})
                ),
                attempt=dispatch.attempts,
            )
        )

    db.commit()
    return claimed


def _reconcile_pending_delivery_events(
    db: Session,
    *,
    connection: ChannelConnection,
    dispatch: MessageDispatch,
    message: Message,
) -> None:
    if not dispatch.provider_message_id:
        return

    events = list(
        db.scalars(
            select(ChannelDeliveryEvent)
            .where(
                ChannelDeliveryEvent.workspace_id == connection.workspace_id,
                ChannelDeliveryEvent.channel_connection_id == connection.id,
                ChannelDeliveryEvent.provider_message_id == dispatch.provider_message_id,
                ChannelDeliveryEvent.processed_at.is_(None),
            )
            .order_by(
                ChannelDeliveryEvent.occurred_at.asc().nulls_last(),
                ChannelDeliveryEvent.created_at,
            )
            .with_for_update()
        )
    )

    for event in events:
        payload = event.payload_json or {}
        apply_provider_delivery_status(
            dispatch=dispatch,
            message=message,
            provider_status=event.status,
            occurred_at=event.occurred_at,
            error=payload.get("error") if isinstance(payload, dict) else None,
            metadata=payload.get("metadata", {}) if isinstance(payload, dict) else {},
        )
        event.processed_at = datetime.now(UTC)


def record_provider_status(
    db: Session,
    *,
    connection: ChannelConnection,
    external_event_id: str,
    provider_message_id: str,
    provider_status: str,
    occurred_at: datetime | None,
    error: str | None,
    metadata: dict,
) -> RecordedProviderStatus:
    existing = db.scalar(
        select(ChannelDeliveryEvent).where(
            ChannelDeliveryEvent.channel_connection_id == connection.id,
            ChannelDeliveryEvent.external_event_id == external_event_id,
        )
    )
    if existing is not None:
        dispatch = db.scalar(
            select(MessageDispatch).where(
                MessageDispatch.workspace_id == connection.workspace_id,
                MessageDispatch.channel_connection_id == connection.id,
                MessageDispatch.provider_message_id == existing.provider_message_id,
            )
        )
        return RecordedProviderStatus(
            event=existing,
            dispatch=dispatch,
            duplicate=True,
        )

    event = ChannelDeliveryEvent(
        workspace_id=connection.workspace_id,
        channel_connection_id=connection.id,
        provider_message_id=provider_message_id,
        external_event_id=external_event_id,
        status=provider_status,
        occurred_at=occurred_at,
        processed_at=None,
        payload_json={
            "error": error,
            "metadata": metadata,
        },
    )
    db.add(event)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(ChannelDeliveryEvent).where(
                ChannelDeliveryEvent.channel_connection_id == connection.id,
                ChannelDeliveryEvent.external_event_id == external_event_id,
            )
        )
        if existing is None:
            raise
        dispatch = db.scalar(
            select(MessageDispatch).where(
                MessageDispatch.workspace_id == connection.workspace_id,
                MessageDispatch.channel_connection_id == connection.id,
                MessageDispatch.provider_message_id == existing.provider_message_id,
            )
        )
        return RecordedProviderStatus(
            event=existing,
            dispatch=dispatch,
            duplicate=True,
        )

    dispatch = db.scalar(
        select(MessageDispatch)
        .where(
            MessageDispatch.workspace_id == connection.workspace_id,
            MessageDispatch.channel_connection_id == connection.id,
            MessageDispatch.provider_message_id == provider_message_id,
        )
        .with_for_update()
    )

    _record_connection_provider_health(
        connection,
        provider_status=provider_status,
        error=error,
        metadata=metadata,
        occurred_at=occurred_at,
    )

    if dispatch is not None:
        message = db.get(Message, dispatch.message_id)
        if message is None:
            raise ChannelError("Provider delivery event references a dispatch with no message.")
        apply_provider_delivery_status(
            dispatch=dispatch,
            message=message,
            provider_status=provider_status,
            occurred_at=occurred_at,
            error=error,
            metadata=metadata,
        )
        reconcile_ai_followup_dispatch(db, dispatch=dispatch, message=message)
        reconcile_campaign_dispatch(db, dispatch=dispatch, message=message)
        event.processed_at = datetime.now(UTC)

    db.commit()
    db.refresh(event)
    if dispatch is not None:
        db.refresh(dispatch)

    return RecordedProviderStatus(
        event=event,
        dispatch=dispatch,
        duplicate=False,
    )


def record_dispatch_result(
    db: Session,
    *,
    connection: ChannelConnection,
    dispatch_id: UUID,
    result_status: str,
    provider_message_id: str | None,
    error: str | None,
    retry_after_seconds: int | None,
    metadata: dict,
) -> MessageDispatch:
    dispatch = db.scalar(
        select(MessageDispatch)
        .where(
            MessageDispatch.id == dispatch_id,
            MessageDispatch.workspace_id == connection.workspace_id,
            MessageDispatch.channel_connection_id == connection.id,
        )
        .with_for_update()
    )
    if dispatch is None:
        raise ChannelError("Dispatch not found for this channel connection.")

    if dispatch.status in {"cancelled", "read"}:
        raise ChannelConflictError(f"Dispatch is already terminal with status '{dispatch.status}'.")

    message = db.get(Message, dispatch.message_id)
    if message is None:
        raise ChannelError("Dispatch references a missing outbound message.")

    now = datetime.now(UTC)
    if provider_message_id:
        if (
            dispatch.provider_message_id is not None
            and dispatch.provider_message_id != provider_message_id
        ):
            raise ChannelConflictError(
                "Dispatch is already mapped to a different provider message id."
            )
        dispatch.provider_message_id = provider_message_id
        message.external_message_id = provider_message_id

    dispatch.metadata_json = {**(dispatch.metadata_json or {}), **metadata}
    dispatch.locked_at = None
    permanent_provider_failure = _record_connection_provider_health(
        connection,
        provider_status=result_status,
        error=error,
        metadata=metadata,
        occurred_at=now,
    )

    if result_status in {"sent", "delivered", "read"}:
        apply_provider_delivery_status(
            dispatch=dispatch,
            message=message,
            provider_status=result_status,
            occurred_at=now,
            error=None,
            metadata=metadata,
        )
    elif result_status == "failed":
        failure = (error or "Provider dispatch failed.")[:2000]
        dispatch.last_error = failure
        if (
            retry_after_seconds
            and _dispatch_has_retry_budget(dispatch)
            and not permanent_provider_failure
        ):
            dispatch.status = "queued"
            dispatch.next_attempt_at = now + timedelta(seconds=retry_after_seconds)
            message.delivery_status = "queued"
        else:
            if retry_after_seconds and not _dispatch_has_retry_budget(dispatch):
                dispatch.metadata_json = {
                    **(dispatch.metadata_json or {}),
                    "retry_exhausted": True,
                    "max_attempts": settings.channel_dispatch_max_attempts,
                }
            _mark_dispatch_failed(dispatch, message, error=failure)
    else:
        raise ChannelError("Unsupported dispatch result status.")

    _reconcile_pending_delivery_events(
        db,
        connection=connection,
        dispatch=dispatch,
        message=message,
    )
    reconcile_ai_followup_dispatch(db, dispatch=dispatch, message=message)
    reconcile_campaign_dispatch(db, dispatch=dispatch, message=message)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ChannelConflictError(
            "Provider message id is already mapped to another dispatch."
        ) from exc

    db.refresh(dispatch)
    db.refresh(message)
    return dispatch