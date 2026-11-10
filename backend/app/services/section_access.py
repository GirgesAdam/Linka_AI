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

SectionAccessKey = Literal["clinic_operations", "administration"]

SECTION_ACCESS_KEYS: Final[tuple[str, ...]] = (
    "clinic_operations",
    "administration",
)
SECTION_ACCESS_KEY_SET: Final = frozenset(SECTION_ACCESS_KEYS)

# Kept only to interpret rows written by the previous 25-page-permission release.
LEGACY_PAGE_PERMISSION_KEYS: Final[tuple[str, ...]] = (
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
LEGACY_PAGE_PERMISSION_KEY_SET: Final = frozenset(LEGACY_PAGE_PERMISSION_KEYS)
LEGACY_CLINIC_OPERATIONS_KEYS: Final[tuple[str, ...]] = (
    "doctors",
    "services",
    "inventory",
)
LEGACY_ADMINISTRATION_KEYS: Final[tuple[str, ...]] = (
    "analytics.overview",
    "analytics.campaigns",
    "analytics.campaign_detail",
    "analytics.cohort_detail",
    "analytics.outstanding_balances",
    "finance",
    "automations",
    "activity",
    "setup",
    "setup.whatsapp",
)


def empty_member_section_access() -> dict[str, bool]:
    """New Members start with both manager-controlled sections closed."""
    return {"clinic_operations": False, "administration": False}


def validate_section_access(value: Mapping[str, object]) -> dict[str, bool]:
    if not isinstance(value, Mapping):
        raise ValueError("Section access must be an object.")
    unknown = set(value) - SECTION_ACCESS_KEY_SET
    missing = SECTION_ACCESS_KEY_SET - set(value)
    if unknown or missing:
        raise ValueError(
            f"Invalid section access: unknown={sorted(map(str, unknown))}, "
            f"missing={sorted(missing)}"
        )
    if any(type(value[key]) is not bool for key in SECTION_ACCESS_KEYS):
        raise ValueError("Section access values must be booleans.")
    return {key: value[key] for key in SECTION_ACCESS_KEYS}


def validate_legacy_page_permissions(value: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("Legacy page permissions must be an object.")
    unknown = set(value) - LEGACY_PAGE_PERMISSION_KEY_SET
    missing = LEGACY_PAGE_PERMISSION_KEY_SET - set(value)
    if unknown or missing:
        raise ValueError(
            f"Invalid legacy page matrix: unknown={sorted(map(str, unknown))}, "
            f"missing={sorted(missing)}"
        )
    for key, level in value.items():
        if not isinstance(level, str) or level not in {"none", "manage"}:
            raise ValueError(f"Invalid legacy page permission level for {key!r}.")
        if key == "team" and level != "none":
            raise ValueError("Team management remains Admin-only.")
    return {key: str(value[key]) for key in LEGACY_PAGE_PERMISSION_KEYS}


def normalize_member_section_access(
    stored_value: object,
) -> tuple[dict[str, bool], bool]:
    """Return section flags and whether the stored value is legacy/malformed.

    Legacy matrices are converted conservatively: a section opens only when every
    old page in that section was explicitly set to manage. NULL and malformed
    values keep both restricted sections closed. No database row is rewritten.
    """
    if not isinstance(stored_value, Mapping):
        return empty_member_section_access(), True

    if set(stored_value) == SECTION_ACCESS_KEY_SET:
        try:
            return validate_section_access(stored_value), False
        except ValueError:
            return empty_member_section_access(), True

    if set(stored_value) == LEGACY_PAGE_PERMISSION_KEY_SET:
        try:
            legacy = validate_legacy_page_permissions(stored_value)
        except (TypeError, ValueError):
            return empty_member_section_access(), True
        return (
            {
                "clinic_operations": all(
                    legacy[key] == "manage" for key in LEGACY_CLINIC_OPERATIONS_KEYS
                ),
                "administration": all(
                    legacy[key] == "manage" for key in LEGACY_ADMINISTRATION_KEYS
                ),
            },
            True,
        )

    return empty_member_section_access(), True


def member_section_access_for_role(
    membership: WorkspaceMember,
) -> dict[str, bool]:
    if membership.role == WORKSPACE_ROLE_ADMIN:
        return {"clinic_operations": True, "administration": True}
    return normalize_member_section_access(membership.section_access)[0]


def section_access_allows(
    membership: WorkspaceMember,
    *,
    workspace_id: UUID,
    user_id: UUID,
    section: SectionAccessKey,
) -> bool:
    if section not in SECTION_ACCESS_KEY_SET:
        raise ValueError(f"Unknown member access section: {section!r}")
    if (
        not membership.is_active
        or membership.workspace_id != workspace_id
        or membership.user_id != user_id
    ):
        return False
    if membership.role == WORKSPACE_ROLE_ADMIN:
        return True
    if membership.role != WORKSPACE_ROLE_MEMBER:
        return False
    access, _is_legacy = normalize_member_section_access(membership.section_access)
    return access[section]


def build_section_access_compare_and_swap(
    *,
    workspace_id: UUID,
    membership_id: UUID,
    actor_user_id: UUID,
    expected_version: int,
    section_access: Mapping[str, object],
) -> Update:
    """Atomically update another active Member in this workspace."""
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("Expected permission version must be a non-negative integer.")
    validated = validate_section_access(section_access)
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
            section_access=validated,
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
