"""Focused tests for the binary Member page-permission contract."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api.dependencies.page_permissions import require_page_permission
from app.models.workspace_member import WorkspaceMember
from app.schemas.page_permissions import MemberPagePermissionsRead, MemberPagePermissionsUpdate
from app.services.page_permissions import (
    PAGE_PERMISSION_KEYS,
    PAGE_PERMISSION_LEVELS,
    build_page_permissions_compare_and_swap,
    page_permission_allows,
    require_successful_permission_update,
    validate_complete_page_permissions,
)


def matrix(level: str = "none") -> dict[str, str]:
    return dict.fromkeys(PAGE_PERMISSION_KEYS, level)


def membership(
    *, role: str = "member", active: bool = True,
    page_permissions: dict[str, str] | None = None,
) -> WorkspaceMember:
    return WorkspaceMember(
        workspace_id=workspace_id,
        user_id=user_id,
        role=role,
        is_active=active,
        page_permissions=page_permissions,
    )


workspace_id = uuid4()
user_id = uuid4()


def allows(member: WorkspaceMember, key: str, legacy: str = "reader") -> bool:
    return page_permission_allows(
        member,
        workspace_id=workspace_id,
        user_id=user_id,
        page_key=key,
        legacy_access=legacy,
    )


def test_catalog_has_exactly_25_unique_keys_and_two_levels() -> None:
    assert len(PAGE_PERMISSION_KEYS) == 25
    assert len(set(PAGE_PERMISSION_KEYS)) == 25
    assert PAGE_PERMISSION_LEVELS == {"none", "manage"}


def test_admin_always_has_full_access_regardless_of_matrix() -> None:
    for stored in (None, {}, {"patients.detail": "none"}, {"bad.key": "invalid"}):
        member = membership(role="admin", page_permissions=stored)
        assert all(allows(member, key, legacy="admin") for key in PAGE_PERMISSION_KEYS)


def test_null_matrix_preserves_legacy_reader_policy() -> None:
    member = membership(page_permissions=None)
    assert all(allows(member, key, "reader") for key in PAGE_PERMISSION_KEYS if key != "team")
    assert not allows(member, "team", "reader")
    assert not allows(member, "patients.detail", "admin")


def test_explicit_manage_grants_page_and_none_denies_page() -> None:
    granted = matrix()
    granted["patients.detail"] = "manage"
    member = membership(page_permissions=granted)
    assert allows(member, "patients.detail")
    assert not allows(member, "patients.list")
    assert not allows(membership(page_permissions=matrix()), "patients.detail")


def test_missing_key_in_stored_explicit_matrix_fails_closed() -> None:
    member = membership(page_permissions={"patients.detail": "manage"})
    assert allows(member, "patients.detail")
    assert not allows(member, "patients.list")


@pytest.mark.parametrize("bad", [
    {"patients.detail": "view"},
    {"patients.detail": "admin"},
    {"patients.detail": 1},
    {"patients.detail": None},
    {"future.page": "manage"},
    {"team": "manage"},
    [],
])
def test_malformed_explicit_matrix_fails_closed(bad) -> None:
    member = membership(page_permissions=bad)
    assert not allows(member, "patients.detail")


def test_team_is_always_admin_only() -> None:
    assert not allows(membership(page_permissions=matrix("none")), "team")
    assert not allows(membership(page_permissions={"team": "manage"}), "team")
    assert allows(membership(role="admin", page_permissions=matrix()), "team")


def test_membership_activity_workspace_and_user_are_enforced() -> None:
    inactive = membership(role="admin", active=False)
    assert not allows(inactive, "dashboard")

    member = membership(page_permissions=matrix("manage"))
    member.workspace_id = uuid4()
    assert not allows(member, "dashboard")
    member.workspace_id = workspace_id
    member.user_id = uuid4()
    assert not allows(member, "dashboard")


def test_complete_matrix_accepts_only_none_and_manage() -> None:
    complete = matrix()
    complete["inventory"] = "manage"
    assert validate_complete_page_permissions(complete) == complete
    assert set(validate_complete_page_permissions(complete)) == set(PAGE_PERMISSION_KEYS)
    with pytest.raises(ValueError, match="unknown"):
        validate_complete_page_permissions({**complete, "bad.key": "none"})
    with pytest.raises(ValueError, match="missing"):
        validate_complete_page_permissions({"inventory": "manage"})
    with pytest.raises(ValueError, match="Invalid page permission level"):
        validate_complete_page_permissions({**complete, "inventory": "view"})
    with pytest.raises(ValueError, match="Admin-only"):
        validate_complete_page_permissions({**complete, "team": "manage"})


def test_update_schema_requires_complete_binary_matrix_and_version() -> None:
    complete = matrix()
    parsed = MemberPagePermissionsUpdate(expected_version=0, page_permissions=complete)
    assert parsed.page_permissions == complete
    for value in (-1, True, "1"):
        with pytest.raises(ValidationError):
            MemberPagePermissionsUpdate(expected_version=value, page_permissions=complete)
    with pytest.raises(ValidationError):
        MemberPagePermissionsUpdate(expected_version=0, page_permissions={**complete, "dashboard": "view"})


def test_permission_read_schema_exposes_version_and_complete_matrix() -> None:
    complete = matrix()
    parsed = MemberPagePermissionsRead(
        membership_id="membership-id",
        page_permissions=complete,
        permissions_version=4,
    )
    assert parsed.permissions_version == 4
    assert parsed.page_permissions == complete


def test_dependency_rejects_unknown_key_at_route_declaration() -> None:
    with pytest.raises(ValueError, match="Unknown page"):
        require_page_permission("patients.unknown", legacy_access="reader")


def test_compare_and_swap_is_workspace_member_actor_and_version_scoped() -> None:
    statement = build_page_permissions_compare_and_swap(
        workspace_id=workspace_id,
        membership_id=uuid4(),
        actor_user_id=user_id,
        expected_version=7,
        page_permissions=matrix("manage"),
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "workspace_members.workspace_id" in sql
    assert "workspace_members.user_id !=" in sql
    assert "workspace_members.role =" in sql
    assert "workspace_members.is_active IS true" in sql
    assert "workspace_members.permissions_version =" in sql
    assert "permissions_version=(workspace_members.permissions_version + " in sql


def test_compare_and_swap_rejects_invalid_version_and_matrix() -> None:
    args = {
        "workspace_id": workspace_id,
        "membership_id": uuid4(),
        "actor_user_id": user_id,
        "expected_version": 0,
        "page_permissions": matrix(),
    }
    with pytest.raises(ValueError):
        build_page_permissions_compare_and_swap(**{**args, "expected_version": True})
    with pytest.raises(ValueError):
        build_page_permissions_compare_and_swap(**{**args, "page_permissions": {"dashboard": "view"}})


def test_stale_compare_and_swap_returns_conflict() -> None:
    assert require_successful_permission_update(8, expected_version=7) == 8
    with pytest.raises(HTTPException) as exc:
        require_successful_permission_update(None, expected_version=7)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException):
        require_successful_permission_update(9, expected_version=7)
