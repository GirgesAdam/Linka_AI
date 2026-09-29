from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess, get_workspace_admin
from app.database.session import get_db
from app.schemas.activity import ActivityActorType, ActivityEventRead
from app.schemas.operations import WorkspaceOperationalReadiness
from app.services.activity import ACTIVITY_ALLOWED_DAYS, list_activity_events
from app.services.operational_readiness import build_workspace_operational_readiness

router = APIRouter()


@router.get("/readiness", response_model=WorkspaceOperationalReadiness)
def operational_readiness(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> WorkspaceOperationalReadiness:
    return build_workspace_operational_readiness(
        db,
        workspace_id=access.workspace.id,
    )


@router.get("/activity", response_model=list[ActivityEventRead])
def activity_log(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
    days: int = 7,
    actor_type: Annotated[ActivityActorType | None, Query()] = None,
    entity_type: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ActivityEventRead]:
    if days not in ACTIVITY_ALLOWED_DAYS:
        raise HTTPException(status_code=422, detail="days must be 7, 30 or 90.")

    rows = list_activity_events(
        db,
        workspace_id=access.workspace.id,
        days=days,
        actor_type=actor_type,
        entity_type=entity_type,
        limit=limit,
    )
    result: list[ActivityEventRead] = []
    for event, user in rows:
        if event.actor_type == "ai":
            actor_label = "Linka"
        elif event.actor_type == "system":
            actor_label = "System"
        else:
            actor_label = (
                (user.full_name or user.email) if user is not None else "Former team member"
            )
        result.append(
            ActivityEventRead(
                id=event.id,
                action=event.action,
                actor_type=event.actor_type,
                actor_user_id=event.actor_user_id,
                actor_label=actor_label,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                summary=event.summary,
                metadata=event.metadata_json or {},
                created_at=event.created_at,
            )
        )
    return result
