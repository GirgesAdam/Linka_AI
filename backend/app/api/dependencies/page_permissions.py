"""Backend enforcement for the binary page-permission contract."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.services.page_permissions import (
    LegacyPageAccess,
    page_permission_allows,
    validate_legacy_access,
    validate_page_key,
)


def require_page_permission(
    page_key: str,
    *,
    legacy_access: LegacyPageAccess,
) -> Callable[..., WorkspaceAccess]:
    """Require explicit MANAGE or preserve the endpoint's legacy NULL policy."""
    validate_page_key(page_key)
    validate_legacy_access(legacy_access)

    def dependency(
        access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    ) -> WorkspaceAccess:
        if not page_permission_allows(
            access.membership,
            workspace_id=access.workspace.id,
            user_id=access.user.id,
            page_key=page_key,
            legacy_access=legacy_access,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this page.",
            )
        return access

    return dependency
