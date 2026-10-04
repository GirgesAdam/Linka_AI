from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.api.dependencies.security import (
    WorkspaceAccess,
    get_workspace_admin,
    get_workspace_reader,
)
from app.database.session import get_db
from app.models.channel_connection import ChannelConnection
from app.models.conversation import Conversation
from app.models.handoff_event import HandoffEvent
from app.models.handoff_request import HandoffRequest
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.models.patient import Patient
from app.models.user import User
from app.models.workspace_member import WORKSPACE_ROLE_ADMIN
from app.schemas.inbox import (
    ConversationOwnerType,
    ConversationReadReceipt,
    ConversationStatus,
    HandoffAssignRequest,
    HandoffCategory,
    HandoffPriority,
    HandoffQueueItem,
    HandoffRead,
    HandoffStatus,
    InboxAssigneeRead,
    InboxConversationListItem,
    InboxConversationRead,
    InboxMessageRead,
    InboxPatientRead,
    InboxRevisionRead,
    InboxSummaryRead,
    ResolveHandoffRequest,
    StaffReplyRequest,
    StaffReplyResponse,
    TakeoverRequest,
)
from app.services.channels import queue_message_dispatch
from app.services.conversation_ownership import mark_conversation_read
from app.services.handoff_intelligence import build_handoff_context
from app.services.handoffs import (
    HandoffStateError,
    add_staff_reply,
    assign_handoff,
    claim_handoff,
    create_handoff,
    ensure_active_workspace_user,
    get_active_handoff,
    resolve_handoff,
)

router = APIRouter()


def _not_found(resource: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{resource} not found in this workspace.",
    )


def _conflict(exc: HandoffStateError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=str(exc),
    )


def _get_handoff(
    db: Session,
    *,
    workspace_id: UUID,
    handoff_id: UUID,
    for_update: bool = False,
) -> HandoffRequest:
    stmt = select(HandoffRequest).where(
        HandoffRequest.workspace_id == workspace_id,
        HandoffRequest.id == handoff_id,
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    handoff = db.scalar(stmt)
    if handoff is None:
        raise _not_found("Handoff")
    return handoff


def _get_conversation(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    for_update: bool = False,
) -> Conversation:
    stmt = select(Conversation).where(
        Conversation.workspace_id == workspace_id,
        Conversation.id == conversation_id,
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    conversation = db.scalar(stmt)
    if conversation is None:
        raise _not_found("Conversation")
    return conversation



def _lock_handoff_and_conversation(
    db: Session,
    *,
    workspace_id: UUID,
    handoff_id: UUID,
) -> tuple[HandoffRequest, Conversation]:
    # Read the foreign key first without a lock, then take ownership-sensitive
    # locks in the same order used by escalation and staff reply: conversation,
    # then handoff. This avoids the handoff->conversation / conversation->handoff
    # deadlock pattern under concurrent claim/escalation.
    handoff_ref = _get_handoff(
        db,
        workspace_id=workspace_id,
        handoff_id=handoff_id,
    )
    conversation = _get_conversation(
        db,
        workspace_id=workspace_id,
        conversation_id=handoff_ref.conversation_id,
        for_update=True,
    )
    handoff = _get_handoff(
        db,
        workspace_id=workspace_id,
        handoff_id=handoff_id,
        for_update=True,
    )
    return handoff, conversation


def _queue_item(
    db: Session,
    handoff: HandoffRequest,
) -> HandoffQueueItem:
    patient = db.scalar(
        select(Patient).where(
            Patient.workspace_id == handoff.workspace_id,
            Patient.id == handoff.patient_id,
        )
    )
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.workspace_id == handoff.workspace_id,
            Conversation.id == handoff.conversation_id,
        )
    )
    assigned_user = None
    if handoff.assigned_user_id:
        assigned_user = db.scalar(select(User).where(User.id == handoff.assigned_user_id))

    if patient is None or conversation is None:
        raise RuntimeError("Handoff references missing workspace data.")

    patient_name = f"{patient.first_name} {patient.last_name or ''}".strip()
    data = HandoffRead.model_validate(handoff).model_dump()
    return HandoffQueueItem(
        **data,
        patient_name=patient_name,
        patient_phone=patient.phone,
        channel=conversation.channel,
        conversation_last_message_at=conversation.last_message_at,
        conversation_owner_type=conversation.owner_type,
        conversation_unread_count=conversation.unread_count,
        assigned_user_name=assigned_user.full_name if assigned_user else None,
        assigned_user_email=assigned_user.email if assigned_user else None,
    )


def _revision_value(*parts: object) -> str:
    return "|".join("" if part is None else str(part) for part in parts)


@router.get("/summary", response_model=InboxSummaryRead)
def inbox_summary(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> InboxSummaryRead:
    unread_conversations = db.scalar(
        select(func.count())
        .select_from(Conversation)
        .where(
            Conversation.workspace_id == access.workspace.id,
            Conversation.status == "open",
            Conversation.unread_count > 0,
        )
    )
    return InboxSummaryRead(unread_conversations=int(unread_conversations or 0))


@router.get("/revision", response_model=InboxRevisionRead)
def inbox_revision(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    conversation_id: UUID | None = None,
) -> InboxRevisionRead:
    workspace_id = access.workspace.id

    if conversation_id is None:
        conversation_count = (
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.workspace_id == workspace_id)
            .scalar_subquery()
        )
        conversation_updated = (
            select(func.max(Conversation.updated_at))
            .where(Conversation.workspace_id == workspace_id)
            .scalar_subquery()
        )
        handoff_updated = (
            select(func.max(HandoffRequest.updated_at))
            .where(HandoffRequest.workspace_id == workspace_id)
            .scalar_subquery()
        )
        connection_updated = (
            select(func.max(ChannelConnection.updated_at))
            .where(ChannelConnection.workspace_id == workspace_id)
            .scalar_subquery()
        )
        row = db.execute(
            select(
                conversation_count,
                conversation_updated,
                handoff_updated,
                connection_updated,
            )
        ).one()
        return InboxRevisionRead(revision=_revision_value(*row))

    conversation_updated = (
        select(Conversation.updated_at)
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.id == conversation_id,
        )
        .scalar_subquery()
    )
    message_count = (
        select(func.count())
        .select_from(Message)
        .where(
            Message.workspace_id == workspace_id,
            Message.conversation_id == conversation_id,
        )
        .scalar_subquery()
    )
    message_updated = (
        select(func.max(Message.updated_at))
        .where(
            Message.workspace_id == workspace_id,
            Message.conversation_id == conversation_id,
        )
        .scalar_subquery()
    )
    handoff_updated = (
        select(func.max(HandoffRequest.updated_at))
        .where(
            HandoffRequest.workspace_id == workspace_id,
            HandoffRequest.conversation_id == conversation_id,
        )
        .scalar_subquery()
    )
    handoff_event_created = (
        select(func.max(HandoffEvent.created_at))
        .where(
            HandoffEvent.workspace_id == workspace_id,
            HandoffEvent.conversation_id == conversation_id,
        )
        .scalar_subquery()
    )
    row = db.execute(
        select(
            conversation_updated,
            message_count,
            message_updated,
            handoff_updated,
            handoff_event_created,
        )
    ).one()
    if row[0] is None:
        raise _not_found("Conversation")
    return InboxRevisionRead(revision=_revision_value(*row))


@router.get("/conversations", response_model=list[InboxConversationListItem])
def list_inbox_conversations(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    owner_type: ConversationOwnerType | None = None,
    conversation_status: Annotated[ConversationStatus | None, Query(alias="status")] = None,
    assigned_to_me: bool = False,
    unread_only: bool = False,
    q: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[InboxConversationListItem]:
    stmt = (
        select(Conversation, Patient, User)
        .join(
            Patient,
            (Patient.workspace_id == Conversation.workspace_id)
            & (Patient.id == Conversation.patient_id),
        )
        .outerjoin(User, User.id == Conversation.assigned_user_id)
        .where(Conversation.workspace_id == access.workspace.id)
    )
    if owner_type:
        stmt = stmt.where(Conversation.owner_type == owner_type)
    if conversation_status:
        stmt = stmt.where(Conversation.status == conversation_status)
    if assigned_to_me:
        stmt = stmt.where(Conversation.assigned_user_id == access.user.id)
    if unread_only:
        stmt = stmt.where(Conversation.unread_count > 0)
    if q:
        search = q.strip()
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            conditions = [
                Patient.first_name.ilike(pattern, escape="\\"),
                Patient.last_name.ilike(pattern, escape="\\"),
                func.concat_ws(" ", Patient.first_name, Patient.last_name).ilike(
                    pattern, escape="\\"
                ),
                Patient.phone.ilike(pattern, escape="\\"),
            ]
            phone_digits = "".join(character for character in search if character.isdigit())
            if phone_digits:
                phone_pattern = f"%{phone_digits}%"
                conditions.append(
                    Patient.phone_normalized.ilike(phone_pattern, escape="\\")
                )
            stmt = stmt.where(or_(*conditions))

    stmt = (
        stmt.order_by(
            Conversation.last_message_at.desc().nullslast(),
            Conversation.started_at.desc(),
            Conversation.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    rows = list(db.execute(stmt).all())
    if not rows:
        return []

    conversation_ids = [conversation.id for conversation, _, _ in rows]
    active_handoffs = {
        handoff.conversation_id: handoff
        for handoff in db.scalars(
            select(HandoffRequest).where(
                HandoffRequest.workspace_id == access.workspace.id,
                HandoffRequest.conversation_id.in_(conversation_ids),
                HandoffRequest.status.in_(("pending", "claimed")),
            )
        )
    }

    ranked_messages = (
        select(
            Message.id.label("message_id"),
            func.row_number()
            .over(
                partition_by=Message.conversation_id,
                order_by=(Message.created_at.desc(), Message.id.desc()),
            )
            .label("row_number"),
        )
        .where(
            Message.workspace_id == access.workspace.id,
            Message.conversation_id.in_(conversation_ids),
        )
        .subquery()
    )
    latest_messages = {
        message.conversation_id: message
        for message in db.scalars(
            select(Message)
            .join(ranked_messages, Message.id == ranked_messages.c.message_id)
            .where(ranked_messages.c.row_number == 1)
        )
    }

    items: list[InboxConversationListItem] = []
    for conversation, patient, assigned_user in rows:
        handoff = active_handoffs.get(conversation.id)
        message = latest_messages.get(conversation.id)
        items.append(
            InboxConversationListItem(
                id=conversation.id,
                workspace_id=conversation.workspace_id,
                patient_id=conversation.patient_id,
                channel=conversation.channel,
                status=conversation.status,
                owner_type=conversation.owner_type,
                unread_count=conversation.unread_count,
                assigned_user_id=conversation.assigned_user_id,
                assigned_user=(
                    InboxAssigneeRead(
                        id=assigned_user.id,
                        full_name=assigned_user.full_name,
                        email=assigned_user.email,
                    )
                    if assigned_user
                    else None
                ),
                subject=conversation.subject,
                started_at=conversation.started_at,
                last_message_at=conversation.last_message_at,
                patient=InboxPatientRead(
                    id=patient.id,
                    first_name=patient.first_name,
                    last_name=patient.last_name,
                    phone=patient.phone,
                ),
                active_handoff=HandoffRead.model_validate(handoff) if handoff else None,
                last_message=InboxMessageRead.model_validate(message) if message else None,
            )
        )
    return items


@router.get("/handoffs", response_model=list[HandoffQueueItem])
def list_handoffs(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    handoff_status: Annotated[HandoffStatus | None, Query(alias="status")] = None,
    category: HandoffCategory | None = None,
    priority: HandoffPriority | None = None,
    assigned_to_me: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[HandoffQueueItem]:
    priority_order = case(
        (HandoffRequest.priority == "urgent", 0),
        (HandoffRequest.priority == "high", 1),
        (HandoffRequest.priority == "normal", 2),
        else_=3,
    )
    stmt = select(HandoffRequest).where(HandoffRequest.workspace_id == access.workspace.id)
    if handoff_status:
        stmt = stmt.where(HandoffRequest.status == handoff_status)
    else:
        stmt = stmt.where(HandoffRequest.status.in_(("pending", "claimed")))
    if category:
        stmt = stmt.where(HandoffRequest.category == category)
    if priority:
        stmt = stmt.where(HandoffRequest.priority == priority)
    if assigned_to_me:
        stmt = stmt.where(HandoffRequest.assigned_user_id == access.user.id)

    stmt = stmt.order_by(priority_order, HandoffRequest.created_at).limit(limit).offset(offset)
    return [_queue_item(db, item) for item in db.scalars(stmt)]


@router.get(
    "/conversations/{conversation_id}",
    response_model=InboxConversationRead,
)
def get_inbox_conversation(
    conversation_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> InboxConversationRead:
    conversation = _get_conversation(
        db,
        workspace_id=access.workspace.id,
        conversation_id=conversation_id,
    )
    patient = db.scalar(
        select(Patient).where(
            Patient.workspace_id == access.workspace.id,
            Patient.id == conversation.patient_id,
        )
    )
    if patient is None:
        raise _not_found("Patient")

    assigned_user = (
        db.scalar(select(User).where(User.id == conversation.assigned_user_id))
        if conversation.assigned_user_id
        else None
    )
    handoff = get_active_handoff(
        db,
        workspace_id=access.workspace.id,
        conversation_id=conversation.id,
    )
    handoff_history = list(
        db.scalars(
            select(HandoffRequest)
            .where(
                HandoffRequest.workspace_id == access.workspace.id,
                HandoffRequest.conversation_id == conversation.id,
            )
            .order_by(HandoffRequest.created_at.desc())
            .limit(100)
        )
    )
    messages = list(
        db.scalars(
            select(Message)
            .where(
                Message.workspace_id == access.workspace.id,
                Message.conversation_id == conversation.id,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(500)
        )
    )
    messages.reverse()
    events = list(
        db.scalars(
            select(HandoffEvent)
            .where(
                HandoffEvent.workspace_id == access.workspace.id,
                HandoffEvent.conversation_id == conversation.id,
            )
            .order_by(HandoffEvent.created_at)
            .limit(500)
        )
    )

    return InboxConversationRead(
        id=conversation.id,
        workspace_id=conversation.workspace_id,
        patient_id=conversation.patient_id,
        channel=conversation.channel,
        channel_connection_id=conversation.channel_connection_id,
        status=conversation.status,
        assigned_user_id=conversation.assigned_user_id,
        owner_type=conversation.owner_type,
        unread_count=conversation.unread_count,
        ownership_changed_at=conversation.ownership_changed_at,
        subject=conversation.subject,
        started_at=conversation.started_at,
        last_message_at=conversation.last_message_at,
        closed_at=conversation.closed_at,
        patient=InboxPatientRead(
            id=patient.id,
            first_name=patient.first_name,
            last_name=patient.last_name,
            phone=patient.phone,
        ),
        assigned_user=(
            InboxAssigneeRead(
                id=assigned_user.id,
                full_name=assigned_user.full_name,
                email=assigned_user.email,
            )
            if assigned_user
            else None
        ),
        active_handoff=HandoffRead.model_validate(handoff) if handoff else None,
        handoff_history=[HandoffRead.model_validate(item) for item in handoff_history],
        messages=[InboxMessageRead.model_validate(message) for message in messages],
        handoff_events=events,
    )


@router.post(
    "/conversations/{conversation_id}/read",
    response_model=ConversationReadReceipt,
)
def mark_inbox_conversation_read(
    conversation_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> ConversationReadReceipt:
    conversation = _get_conversation(
        db,
        workspace_id=access.workspace.id,
        conversation_id=conversation_id,
        for_update=True,
    )
    mark_conversation_read(conversation)
    db.commit()
    return ConversationReadReceipt(
        conversation_id=conversation.id,
        unread_count=conversation.unread_count,
    )


@router.post(
    "/handoffs/{handoff_id}/claim",
    response_model=HandoffRead,
)
def claim_inbox_handoff(
    handoff_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> HandoffRequest:
    handoff, conversation = _lock_handoff_and_conversation(
        db,
        workspace_id=access.workspace.id,
        handoff_id=handoff_id,
    )
    try:
        return claim_handoff(
            db,
            handoff=handoff,
            conversation=conversation,
            user=access.user,
        )
    except HandoffStateError as exc:
        db.rollback()
        raise _conflict(exc) from exc


@router.post(
    "/handoffs/{handoff_id}/assign",
    response_model=HandoffRead,
)
def assign_inbox_handoff(
    handoff_id: UUID,
    payload: HandoffAssignRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> HandoffRequest:
    handoff, conversation = _lock_handoff_and_conversation(
        db,
        workspace_id=access.workspace.id,
        handoff_id=handoff_id,
    )
    try:
        target_user = ensure_active_workspace_user(
            db,
            workspace_id=access.workspace.id,
            user_id=payload.user_id,
        )
        return assign_handoff(
            db,
            handoff=handoff,
            conversation=conversation,
            target_user=target_user,
            actor_user=access.user,
        )
    except HandoffStateError as exc:
        db.rollback()
        raise _conflict(exc) from exc


@router.post(
    "/conversations/{conversation_id}/takeover",
    response_model=HandoffRead,
)
def take_over_conversation(
    conversation_id: UUID,
    payload: TakeoverRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> HandoffRequest:
    conversation = _get_conversation(
        db,
        workspace_id=access.workspace.id,
        conversation_id=conversation_id,
    )
    patient = db.scalar(
        select(Patient).where(
            Patient.workspace_id == access.workspace.id,
            Patient.id == conversation.patient_id,
        )
    )
    if patient is None:
        raise _not_found("Patient")

    try:
        handoff = create_handoff(
            db,
            workspace_id=access.workspace.id,
            conversation=conversation,
            patient=patient,
            reason=payload.reason or "Manual staff takeover.",
            category=payload.category,
            priority=payload.priority,
            source="staff",
            created_by_user_id=access.user.id,
            handoff_context=build_handoff_context(
                trigger="manual_takeover",
                semantic_reason=payload.reason or "Manual staff takeover.",
            ),
            commit=False,
        )
        return claim_handoff(
            db,
            handoff=handoff,
            conversation=conversation,
            user=access.user,
        )
    except HandoffStateError as exc:
        db.rollback()
        raise _conflict(exc) from exc


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=StaffReplyResponse,
    status_code=status.HTTP_201_CREATED,
)
def send_staff_reply(
    conversation_id: UUID,
    payload: StaffReplyRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128),
    ] = None,
) -> StaffReplyResponse:
    conversation = _get_conversation(
        db,
        workspace_id=access.workspace.id,
        conversation_id=conversation_id,
        for_update=True,
    )
    handoff = get_active_handoff(
        db,
        workspace_id=access.workspace.id,
        conversation_id=conversation.id,
        for_update=True,
    )
    if handoff is None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This conversation does not have an active handoff.",
        )

    if handoff.assigned_user_id != access.user.id:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Claim this handoff before replying to the customer.",
        )

    request_key = idempotency_key.strip() if idempotency_key else None
    if request_key:
        existing_message = db.scalar(
            select(Message)
            .where(
                Message.workspace_id == access.workspace.id,
                Message.conversation_id == conversation.id,
                Message.sender_type == "staff",
                Message.direction == "outbound",
                Message.sent_by_user_id == access.user.id,
                Message.metadata_json.contains({"staff_reply_request_id": request_key}),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        if existing_message is not None:
            if existing_message.content != payload.content:
                db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Idempotency key was already used for a different staff reply.",
                )
            existing_dispatch = db.scalar(
                select(MessageDispatch).where(
                    MessageDispatch.workspace_id == access.workspace.id,
                    MessageDispatch.message_id == existing_message.id,
                )
            )
            response = StaffReplyResponse(
                message=InboxMessageRead.model_validate(existing_message),
                dispatch_required=existing_dispatch is not None,
                dispatch_id=existing_dispatch.id if existing_dispatch else None,
            )
            db.rollback()
            return response

    try:
        message = add_staff_reply(
            db,
            handoff=handoff,
            conversation=conversation,
            user=access.user,
            content=payload.content,
            commit=False,
        )
    except HandoffStateError as exc:
        db.rollback()
        raise _conflict(exc) from exc

    if request_key:
        message.metadata_json = {
            **(message.metadata_json or {}),
            "staff_reply_request_id": request_key,
        }

    dispatch = queue_message_dispatch(
        db,
        message=message,
        conversation=conversation,
        commit=False,
    )
    # Persist the staff message, handoff audit event, and provider outbox row as
    # one unit. A crash can no longer leave a visible reply with no dispatch.
    db.commit()
    db.refresh(message)
    if dispatch is not None:
        db.refresh(dispatch)

    return StaffReplyResponse(
        message=InboxMessageRead.model_validate(message),
        dispatch_required=dispatch is not None,
        dispatch_id=dispatch.id if dispatch else None,
    )


@router.post(
    "/handoffs/{handoff_id}/resolve",
    response_model=HandoffRead,
)
def resolve_inbox_handoff(
    handoff_id: UUID,
    payload: ResolveHandoffRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> HandoffRequest:
    handoff, conversation = _lock_handoff_and_conversation(
        db,
        workspace_id=access.workspace.id,
        handoff_id=handoff_id,
    )

    is_admin = access.membership.role == WORKSPACE_ROLE_ADMIN
    if not is_admin and handoff.assigned_user_id != access.user.id:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Claim this handoff before resolving it.",
        )

    try:
        return resolve_handoff(
            db,
            handoff=handoff,
            conversation=conversation,
            actor_user=access.user,
            resolution_note=payload.resolution_note,
            conversation_status_after=payload.conversation_status_after,
        )
    except HandoffStateError as exc:
        db.rollback()
        raise _conflict(exc) from exc
