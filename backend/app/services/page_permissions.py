"""Binary page-permission catalog and atomic Member permission updates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.sql.dml import Update

from app.models.workspace_member import (
    WORKSPACE_ROLE_ADMIN,
    WORKSPACE_ROLE_MEMBER,
    WorkspaceMember,
)

PagePermissionLevel = Literal["none", "manage"]
LegacyPageAccess = Literal["reader", "admin"]

PAGE_PERMISSION_KEYS: Final[tuple[str, ...]] = (
    "dashboard",
    "inbox.list",
    "inbox.detail",
    "appointments.calendar",
    "appointments.detail",
    "appointments.reschedule",
    "patients.list",
    "patients.detail",
    "tasks",
    "doctors",
    "services",
    "inventory",
    "analytics.overview",
    "analytics.campaigns",
    "analytics.campaign_detail",
    "analytics.cohort_detail",
    "analytics.outstanding_balances",
    "finance",
    "automations",
    "activity",
    "team",
    "setup",
    "setup.whatsapp",
    "demo.home",
    "demo.agent",
)
PAGE_PERMISSION_KEY_SET: Final = frozenset(PAGE_PERMISSION_KEYS)
PAGE_PERMISSION_LEVELS: Final = frozenset({"none", "manage"})


def validate_page_key(page_key: str) -> None:
    if page_key not in PAGE_PERMISSION_KEY_SET:
        raise ValueError(f"Unknown page permission key: {page_key!r}")


def validate_legacy_access(legacy_access: str) -> None:
    if legacy_access not in {"reader", "admin"}:
        raise ValueError(f"Unknown legacy access requirement: {legacy_access!r}")


def validate_complete_page_permissions(matrix: Mapping[str, str]) -> dict[str, str]:
    """Validate a complete explicit matrix; no partial writes or hidden levels."""
    if not isinstance(matrix, Mapping):
        raise ValueError("Page permissions must be an object.")
    unknown = set(matrix) - PAGE_PERMISSION_KEY_SET
    missing = PAGE_PERMISSION_KEY_SET - set(matrix)
    if unknown or missing:
        raise ValueError(
            f"Invalid page matrix: unknown={sorted(unknown)}, missing={sorted(missing)}"
        )
    for key, level in matrix.items():
        if not isinstance(level, str) or level not in PAGE_PERMISSION_LEVELS:
            raise ValueError(f"Invalid page permission level for {key!r}.")
        if key == "team" and level != "none":
            raise ValueError("Team management is Admin-only.")
    return dict(matrix)


def page_permission_allows(
    membership: WorkspaceMember,
    *,
    workspace_id: UUID,
    user_id: UUID,
    page_key: str,
    legacy_access: LegacyPageAccess,
) -> bool:
    """Admin always passes; NULL retains legacy role behavior; explicit manage grants."""
    validate_page_key(page_key)
    validate_legacy_access(legacy_access)
    if (
        not membership.is_active
        or membership.workspace_id != workspace_id
        or membership.user_id != user_id
    ):
        return False
    if membership.role == WORKSPACE_ROLE_ADMIN:
        return True
    if membership.role != WORKSPACE_ROLE_MEMBER or page_key == "team":
        return False

    matrix = membership.page_permissions
    if matrix is None:
        return legacy_access == "reader"
    if not isinstance(matrix, dict):
        return False
    try:
        validated_matrix = validate_complete_page_permissions(matrix)
    except (TypeError, ValueError):
        return False
    return validated_matrix[page_key] == "manage"


def build_page_permissions_compare_and_swap(
    *,
    workspace_id: UUID,
    membership_id: UUID,
    actor_user_id: UUID,
    expected_version: int,
    page_permissions: Mapping[str, str],
) -> Update:
    """Build an atomic update restricted to another active Member in this workspace."""
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("Expected permission version must be a non-negative integer.")
    validated = validate_complete_page_permissions(page_permissions)
    return (
        update(WorkspaceMember)
        .where(
            WorkspaceMember.id == membership_id,
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id != actor_user_id,
            WorkspaceMember.role == WORKSPACE_ROLE_MEMBER,
            WorkspaceMember.is_active.is_(True),
            WorkspaceMember.permissions_version == expected_version,
        )
        .values(
            page_permissions=validated,
            permissions_version=WorkspaceMember.permissions_version + 1,
        )
        .returning(WorkspaceMember.permissions_version)
    )


def require_successful_permission_update(
    updated_version: int | None, *, expected_version: int
) -> int:
    if updated_version is None or updated_version != expected_version + 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Membership or permission version changed. Reload before saving.",
        )
    return updated_version
