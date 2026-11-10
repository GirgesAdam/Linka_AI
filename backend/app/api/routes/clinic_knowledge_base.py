from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies.section_access import require_section_access
from app.api.dependencies.security import WorkspaceAccess
from app.database.session import get_db
from app.schemas.clinic_knowledge_base import ClinicKnowledgeText
from app.services.activity import record_activity_event
from app.services.clinic_knowledge_base import read_knowledge_text, replace_knowledge_text

router = APIRouter()


@router.get("/knowledge-text", response_model=ClinicKnowledgeText)
def read_single_knowledge_text(
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicKnowledgeText:
    """Return the clinic's one editable explanatory knowledge source."""
    return ClinicKnowledgeText(content=read_knowledge_text(db, workspace_id=access.workspace.id))


@router.put("/knowledge-text", response_model=ClinicKnowledgeText)
def replace_single_knowledge_text(
    payload: ClinicKnowledgeText,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicKnowledgeText:
    """Replace all user-managed explanatory knowledge with the single clinic text."""
    content = replace_knowledge_text(db, workspace_id=access.workspace.id, content=payload.content)
    record_activity_event(
        db,
        workspace_id=access.workspace.id,
        actor_type="staff",
        actor_user_id=access.user.id,
        action="clinic.knowledge_replaced",
        entity_type="clinic_knowledge",
        entity_id=None,
        summary="Clinic knowledge text replaced.",
        metadata={"characters": len(content)},
        flush=False,
    )
    db.commit()
    return ClinicKnowledgeText(content=content)
