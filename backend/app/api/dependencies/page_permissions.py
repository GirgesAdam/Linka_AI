"""Opt-in page permission dependency; deliberately unused by business routes in PR-01."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.api.dependencies.security import WorkspaceAccess, get_workspace_reader
from app.services.page_permissions import (
    PagePermissionAction,
    page_permission_allows,
    validate_page_action,
    validate_page_key,
)


def require_page_permission(
    page_key: str, action: PagePermissionAction
) -> Callable[..., WorkspaceAccess]:
    validate_page_key(page_key)
    validate_page_action(action)

    def dependency(
        access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    ) -> WorkspaceAccess:
        if not page_permission_allows(
            access.membership,
            workspace_id=access.workspace.id,
            user_id=access.user.id,
            page_key=page_key,
            action=action,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this page.",
            )
        return access

    return dependency
