"""Opt-in page permission dependency; deliberately unused by business routes in PR-01."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.services.page_permissions import (
    LegacyPageAccess,
    PagePermissionAction,
    page_permission_allows,
    validate_legacy_access,
    validate_page_action,
    validate_page_key,
)


def require_page_permission(
    page_key: str,
    action: PagePermissionAction,
    *,
    legacy_access: LegacyPageAccess,
) -> Callable[..., WorkspaceAccess]:
    """Declare the existing endpoint reader/admin policy explicitly.

    This dependency replaces the legacy role gate for the route it protects;
    do not also attach get_workspace_admin when explicit Member manage is allowed.
    """
    validate_page_key(page_key)
    validate_page_action(action)
    validate_legacy_access(legacy_access)

    def dependency(
        access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    ) -> WorkspaceAccess:
        if not page_permission_allows(
            access.membership,
            workspace_id=access.workspace.id,
            user_id=access.user.id,
            page_key=page_key,
            action=action,
            legacy_access=legacy_access,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this page.",
            )
        return access

    return dependency
