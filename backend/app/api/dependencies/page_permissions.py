"""Backend enforcement for the binary page-permission contract."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.models.workspace import Workspace
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


def require_page_permission_workspace(
    page_key: str,
    *,
    legacy_access: LegacyPageAccess,
) -> Callable[..., Workspace]:
    """Project a permitted workspace for endpoints using workspace-only dependencies."""

    permission = require_page_permission(page_key, legacy_access=legacy_access)

    def dependency(
        access: Annotated[WorkspaceAccess, Depends(permission)],
    ) -> Workspace:
        return access.workspace

    return dependency


def require_page_permission_user_id(
    page_key: str,
    *,
    legacy_access: LegacyPageAccess,
) -> Callable[..., UUID]:
    """Record the authenticated user ID without requiring Admin for a managed page."""

    permission = require_page_permission(page_key, legacy_access=legacy_access)

    def dependency(
        access: Annotated[WorkspaceAccess, Depends(permission)],
    ) -> UUID:
        return access.user.id

    return dependency

def require_any_page_permission(
    *requirements: tuple[str, LegacyPageAccess],
) -> Callable[..., WorkspaceAccess]:
    """Allow a shared endpoint when any listed page is explicitly managed.

    Legacy role behavior is evaluated independently for each page. This remains
    page-level authorization; it does not introduce action-specific permissions.
    """
    if not requirements:
        raise ValueError("At least one page permission requirement is required.")
    for page_key, legacy_access in requirements:
        validate_page_key(page_key)
        validate_legacy_access(legacy_access)

    def dependency(
        access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    ) -> WorkspaceAccess:
        if not any(
            page_permission_allows(
                access.membership,
                workspace_id=access.workspace.id,
                user_id=access.user.id,
                page_key=page_key,
                legacy_access=legacy_access,
            )
            for page_key, legacy_access in requirements
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this page.",
            )
        return access

    return dependency
