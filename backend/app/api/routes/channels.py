from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.llm_runtime import LLMProviderError, provider_error_http_status
from app.agents.model_provider import LLMConfigurationError
from app.agents.structured_output import StructuredOutputError
from app.api.dependencies.page_permissions import (
    require_any_page_permission,
    require_page_permission,
)
from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.core.channel_adapter import generate_adapter_token
from app.database.session import get_db
from app.models.channel_connection import ChannelConnection
from app.schemas.channel import (
    ChannelConnectionCreate,
    ChannelConnectionCreated,
    ChannelConnectionRead,
    ChannelConnectionStatus,
    ChannelConnectionUpdate,
    ChannelTokenRotated,
    DispatchClaimItem,
    DispatchClaimRequest,
    DispatchResultRequest,
    InboundAcceptedResponse,
    InboundProcessResponse,
    MessageDispatchRead,
    NormalizedInboundMessage,
    ProviderStatusRequest,
    ProviderStatusResponse,
)
from app.services.channels import (
    ChannelConflictError,
    ChannelError,
    accept_normalized_inbound,
    claim_dispatches,
    get_connection_by_adapter_token,
    process_inbound_event,
    record_dispatch_result,
    record_provider_status,
)

router = APIRouter()


@dataclass(frozen=True)
class AdapterAccess:
    connection: ChannelConnection


def _not_found(entity: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{entity} not found.",
    )


def _conflict(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=detail,
    )


def _get_workspace_connection(
    db: Session,
    *,
    workspace_id: UUID,
    connection_id: UUID,
) -> ChannelConnection:
    connection = db.scalar(
        select(ChannelConnection).where(
            ChannelConnection.workspace_id == workspace_id,
            ChannelConnection.id == connection_id,
        )
    )
    if connection is None:
        raise _not_found("Channel connection")
    return connection


def get_adapter_access(
    x_channel_token: Annotated[str, Header(alias="X-Channel-Token")],
    db: Annotated[Session, Depends(get_db)],
) -> AdapterAccess:
    connection = get_connection_by_adapter_token(db, x_channel_token)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid channel adapter token.",
        )
    return AdapterAccess(connection=connection)


def _require_active(connection: ChannelConnection) -> None:
    if connection.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Channel connection is '{connection.status}', not active.",
        )


@router.post(
    "/connections",
    response_model=ChannelConnectionCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_page_permission("setup.whatsapp", legacy_access="admin"))],
)
def create_channel_connection(
    payload: ChannelConnectionCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    db: Annotated[Session, Depends(get_db)],
) -> ChannelConnectionCreated:
    adapter_token, adapter_token_hash = generate_adapter_token()
    connection = ChannelConnection(
        workspace_id=access.workspace.id,
        channel=payload.channel,
        provider=payload.provider,
        display_name=payload.display_name,
        status=payload.status,
        external_account_id=payload.external_account_id,
        adapter_token_hash=adapter_token_hash,
        created_by_user_id=access.user.id,
        config_json=payload.config,
    )
    db.add(connection)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict(
            "A matching channel account is already connected to this workspace."
        ) from exc
    db.refresh(connection)

    return ChannelConnectionCreated(
        **ChannelConnectionRead.model_validate(connection).model_dump(),
        adapter_token=adapter_token,
    )


@router.get("/connections", response_model=list[ChannelConnectionRead])
def list_channel_connections(
    access: Annotated[
        WorkspaceAccess,
        Depends(
            require_any_page_permission(
                ("setup.whatsapp", "admin"),
                ("analytics.cohort_detail", "admin"),
            )
        ),
    ],
    db: Annotated[Session, Depends(get_db)],
    connection_status: Annotated[
        ChannelConnectionStatus | None,
        Query(alias="status"),
    ] = None,
) -> list[ChannelConnection]:
    stmt = select(ChannelConnection).where(ChannelConnection.workspace_id == access.workspace.id)
    if connection_status:
        stmt = stmt.where(ChannelConnection.status == connection_status)
    return list(db.scalars(stmt.order_by(ChannelConnection.created_at)))


@router.get(
    "/connections/{connection_id}",
    response_model=ChannelConnectionRead,
)
def get_channel_connection(
    connection_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("setup.whatsapp", legacy_access="admin"))],
    db: Annotated[Session, Depends(get_db)],
) -> ChannelConnection:
    return _get_workspace_connection(
        db,
        workspace_id=access.workspace.id,
        connection_id=connection_id,
    )


@router.patch(
    "/connections/{connection_id}",
    response_model=ChannelConnectionRead,
)
def update_channel_connection(
    connection_id: UUID,
    payload: ChannelConnectionUpdate,
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("setup.whatsapp", legacy_access="admin"))],
    db: Annotated[Session, Depends(get_db)],
) -> ChannelConnection:
    connection = _get_workspace_connection(
        db,
        workspace_id=access.workspace.id,
        connection_id=connection_id,
    )
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "config":
            connection.config_json = value or {}
        else:
            setattr(connection, key, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict(
            "The updated external channel account conflicts with another connection."
        ) from exc
    db.refresh(connection)
    return connection


@router.post(
    "/connections/{connection_id}/rotate-token",
    response_model=ChannelTokenRotated,
    dependencies=[Depends(require_page_permission("setup.whatsapp", legacy_access="admin"))],
)
def rotate_channel_token(
    connection_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    db: Annotated[Session, Depends(get_db)],
) -> ChannelTokenRotated:
    connection = _get_workspace_connection(
        db,
        workspace_id=access.workspace.id,
        connection_id=connection_id,
    )
    adapter_token, adapter_token_hash = generate_adapter_token()
    connection.adapter_token_hash = adapter_token_hash
    db.commit()
    return ChannelTokenRotated(
        connection_id=connection.id,
        adapter_token=adapter_token,
    )


@router.post(
    "/adapter/inbound",
    response_model=InboundAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def accept_channel_inbound(
    payload: NormalizedInboundMessage,
    adapter: Annotated[AdapterAccess, Depends(get_adapter_access)],
    db: Annotated[Session, Depends(get_db)],
) -> InboundAcceptedResponse:
    _require_active(adapter.connection)
    try:
        accepted = accept_normalized_inbound(
            db,
            connection=adapter.connection,
            payload=payload,
        )
    except ChannelConflictError as exc:
        raise _conflict(str(exc)) from exc
    except ChannelError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return InboundAcceptedResponse(
        event_id=accepted.event.id,
        message_id=accepted.message.id,
        patient_id=accepted.patient.id,
        conversation_id=accepted.conversation.id,
        duplicate=accepted.duplicate,
        processing_required=accepted.event.status != "processed",
        status=accepted.event.status,
    )


@router.post(
    "/adapter/inbound/{event_id}/process",
    response_model=InboundProcessResponse,
)
def process_channel_inbound(
    event_id: UUID,
    adapter: Annotated[AdapterAccess, Depends(get_adapter_access)],
    db: Annotated[Session, Depends(get_db)],
) -> InboundProcessResponse:
    _require_active(adapter.connection)
    try:
        processed = process_inbound_event(
            db,
            connection=adapter.connection,
            event_id=event_id,
        )
    except ChannelConflictError as exc:
        raise _conflict(str(exc)) from exc
    except ChannelError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except LLMConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except LLMProviderError as exc:
        raise HTTPException(
            status_code=provider_error_http_status(exc),
            detail="Gemini provider request failed.",
        ) from exc
    except StructuredOutputError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI semantic routing returned an invalid structured result.",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI agent execution failed for inbound channel event.",
        ) from exc

    response = processed.agent_response
    return InboundProcessResponse(
        event_id=processed.event.id,
        status=processed.event.status,
        conversation_id=response.conversation_id,
        inbound_message_id=response.inbound_message_id,
        outbound_message_id=response.outbound_message_id,
        dispatch_id=processed.dispatch.id if processed.dispatch else None,
        handoff_required=response.handoff_required,
        agent_paused=response.agent_paused,
        reply=response.reply,
        model=response.model,
    )


@router.post(
    "/adapter/outbox/claim",
    response_model=list[DispatchClaimItem],
)
def claim_channel_outbox(
    payload: DispatchClaimRequest,
    adapter: Annotated[AdapterAccess, Depends(get_adapter_access)],
    db: Annotated[Session, Depends(get_db)],
) -> list[DispatchClaimItem]:
    _require_active(adapter.connection)
    return claim_dispatches(
        db,
        connection=adapter.connection,
        limit=payload.limit,
    )


@router.post(
    "/adapter/outbox/{dispatch_id}/result",
    response_model=MessageDispatchRead,
)
def report_channel_dispatch_result(
    dispatch_id: UUID,
    payload: DispatchResultRequest,
    adapter: Annotated[AdapterAccess, Depends(get_adapter_access)],
    db: Annotated[Session, Depends(get_db)],
) -> MessageDispatchRead:
    try:
        dispatch = record_dispatch_result(
            db,
            connection=adapter.connection,
            dispatch_id=dispatch_id,
            result_status=payload.status,
            provider_message_id=payload.provider_message_id,
            error=payload.error,
            retry_after_seconds=payload.retry_after_seconds,
            metadata=payload.metadata,
        )
    except ChannelConflictError as exc:
        raise _conflict(str(exc)) from exc
    except ChannelError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return MessageDispatchRead.model_validate(dispatch)


@router.post(
    "/adapter/outbox/provider-status",
    response_model=ProviderStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def report_provider_delivery_status(
    payload: ProviderStatusRequest,
    adapter: Annotated[AdapterAccess, Depends(get_adapter_access)],
    db: Annotated[Session, Depends(get_db)],
) -> ProviderStatusResponse:
    try:
        recorded = record_provider_status(
            db,
            connection=adapter.connection,
            external_event_id=payload.external_event_id,
            provider_message_id=payload.provider_message_id,
            provider_status=payload.status,
            occurred_at=payload.occurred_at,
            error=payload.error,
            metadata=payload.metadata,
        )
    except ChannelConflictError as exc:
        raise _conflict(str(exc)) from exc
    except ChannelError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    dispatch = recorded.dispatch
    return ProviderStatusResponse(
        event_id=recorded.event.id,
        duplicate=recorded.duplicate,
        matched_dispatch=dispatch is not None,
        dispatch_id=dispatch.id if dispatch else None,
        dispatch_status=dispatch.status if dispatch else None,
    )
