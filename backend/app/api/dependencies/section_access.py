from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.models.workspace import Workspace
from app.services.section_access import (
    SectionAccessKey,
    section_access_allows,
)


def require_section_access(
    section: SectionAccessKey,
) -> Callable[..., WorkspaceAccess]:
    """Require one of the two manager-controlled Member sections."""
    if section not in {"clinic_operations", "administration"}:
        raise ValueError(f"Unknown member access section: {section!r}")

    def dependency(
        access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    ) -> WorkspaceAccess:
        if not section_access_allows(
            access.membership,
            workspace_id=access.workspace.id,
            user_id=access.user.id,
            section=section,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this section.",
            )
        return access

    return dependency


def require_section_access_workspace(
    section: SectionAccessKey,
) -> Callable[..., Workspace]:
    """Project the authorized workspace for handlers that only need Workspace."""
    section_dependency = require_section_access(section)

    def dependency(
        access: Annotated[WorkspaceAccess, Depends(section_dependency)],
    ) -> Workspace:
        return access.workspace

    return dependency


def require_section_access_user_id(
    section: SectionAccessKey,
) -> Callable[..., UUID]:
    """Return the actor ID after checking the requested section."""
    section_dependency = require_section_access(section)

    def dependency(
        access: Annotated[WorkspaceAccess, Depends(section_dependency)],
    ) -> UUID:
        return access.user.id

    return dependency
