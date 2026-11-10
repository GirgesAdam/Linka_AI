from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.llm_runtime import LLMProviderError, provider_error_http_status
from app.agents.model_provider import LLMConfigurationError
from app.agents.structured_output import StructuredOutputError
from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.database.session import get_db
from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.agent import AgentChatRequest, AgentChatResponse
from app.services.agent_chat import AgentChatError
from app.services.agent_v2.live_chat import run_agent_chat
from app.services.conversation_flows import FlowStateConflictError
from app.services.workspace_runtime_policy import workspace_runtime_policy

router = APIRouter()


def _enforce_demo_agent_budget(db: Session, *, workspace) -> None:
    policy = workspace_runtime_policy(workspace)
    if not policy.is_demo:
        return
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    turns = db.scalar(
        select(func.count(Message.id))
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.workspace_id == workspace.id,
            Message.sender_type == "patient",
            Message.direction == "inbound",
            Message.created_at >= cutoff,
            Conversation.channel == "web",
        )
    ) or 0
    if policy.agent_hourly_turn_limit is not None and turns >= policy.agent_hourly_turn_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Public demo agent budget reached. Try again later.",
        )


@router.post("/chat", response_model=AgentChatResponse)
def chat_with_tia_agent(
    payload: AgentChatRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    db: Annotated[Session, Depends(get_db)],
) -> AgentChatResponse:
    _enforce_demo_agent_budget(db, workspace=access.workspace)
    try:
        return run_agent_chat(
            db=db,
            workspace=access.workspace,
            payload=payload,
        )
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
    except FlowStateConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Conversation state changed concurrently. Retry this turn.",
        ) from exc
    except StructuredOutputError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI semantic routing returned an invalid structured result.",
        ) from exc
    except AgentChatError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI agent execution failed.",
        ) from exc
