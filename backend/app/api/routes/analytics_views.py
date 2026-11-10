from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies.section_access import require_section_access
from app.api.dependencies.security import WorkspaceAccess
from app.database.session import get_db
from app.schemas.analytics_saved_view import AnalyticsSavedViewCreate, AnalyticsSavedViewRead
from app.services.analytics_saved_views import (
    AnalyticsSavedViewConflictError,
    AnalyticsSavedViewError,
    create_analytics_saved_view,
    delete_analytics_saved_view,
    list_analytics_saved_views,
)

router = APIRouter()


@router.get("/views", response_model=list[AnalyticsSavedViewRead])
def get_saved_analytics_views(
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
    limit: int = 20,
) -> list[AnalyticsSavedViewRead]:
    return list_analytics_saved_views(db, workspace_id=access.workspace.id, created_by_user_id=access.user.id, limit=limit)


@router.post("/views", response_model=AnalyticsSavedViewRead, status_code=status.HTTP_201_CREATED)
def save_analytics_view(
    payload: AnalyticsSavedViewCreate,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> AnalyticsSavedViewRead:
    try:
        return create_analytics_saved_view(
            db,
            workspace_id=access.workspace.id,
            created_by_user_id=access.user.id,
            payload=payload,
        )
    except AnalyticsSavedViewConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AnalyticsSavedViewError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.delete("/views/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_analytics_view(
    view_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    deleted = delete_analytics_saved_view(
        db, workspace_id=access.workspace.id, created_by_user_id=access.user.id, view_id=view_id
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved analytics view not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
