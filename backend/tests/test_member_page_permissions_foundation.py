"""Focused tests for the binary Member page-permission contract."""

from types import SimpleNamespace
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api.dependencies.page_permissions import require_page_permission
from app.api.dependencies.security import (
    WorkspaceAccess,
    get_workspace_access,
    get_workspace_admin,
)
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
    return {key: ("none" if key == "team" else level) for key in PAGE_PERMISSION_KEYS}


workspace_id = uuid4()
user_id = uuid4()


def membership(
    *,
    role: str = "member",
    active: bool = True,
    page_permissions: dict[str, str] | None = None,
) -> WorkspaceMember:
    return WorkspaceMember(
        workspace_id=workspace_id,
        user_id=user_id,
        role=role,
        is_active=active,
        page_permissions=page_permissions,
    )


def workspace_access(
    *,
    role: str = "member",
    active: bool = True,
    page_permissions: dict[str, str] | None = None,
) -> WorkspaceAccess:
    return WorkspaceAccess(
        user=SimpleNamespace(id=user_id),
        workspace=SimpleNamespace(id=workspace_id, is_active=True),
        membership=membership(
            role=role,
            active=active,
            page_permissions=page_permissions,
        ),
    )


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


def test_admin_always_has_full_access_after_membership_checks() -> None:
    for stored in (None, {}, {"patients.detail": "none"}, {"bad.key": "invalid"}):
        member = membership(role="admin", page_permissions=stored)
        assert all(allows(member, key, legacy="admin") for key in PAGE_PERMISSION_KEYS)


def test_null_matrix_preserves_legacy_reader_policy() -> None:
    member = membership(page_permissions=None)
    assert all(allows(member, key, "reader") for key in PAGE_PERMISSION_KEYS if key != "team")
    assert not allows(member, "team", "reader")
    assert not allows(member, "patients.detail", "admin")


def test_explicit_complete_matrix_grants_manage_and_denies_none() -> None:
    granted = matrix()
    granted["patients.detail"] = "manage"
    member = membership(page_permissions=granted)
    assert allows(member, "patients.detail")
    assert not allows(member, "patients.list")
    assert not allows(membership(page_permissions=matrix()), "patients.detail")


def test_incomplete_explicit_stored_matrix_fails_closed_even_for_present_grant() -> None:
    member = membership(page_permissions={"patients.detail": "manage"})
    assert not allows(member, "patients.detail")
    assert not allows(member, "patients.list")


@pytest.mark.parametrize(
    "bad",
    [
        {**matrix(), "patients.detail": "view"},
        {**matrix(), "future.page": "manage"},
        {**matrix(), "team": "manage"},
        {"patients.detail": 1},
        {"patients.detail": None},
        [],
    ],
)
def test_malformed_explicit_matrix_fails_closed(bad) -> None:
    member = membership(page_permissions=bad)
    assert not allows(member, "patients.detail")


def test_team_is_always_admin_only() -> None:
    assert not allows(membership(page_permissions=matrix("manage")), "team")
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
        MemberPagePermissionsUpdate(
            expected_version=0,
            page_permissions={**complete, "dashboard": "view"},
        )
    with pytest.raises(ValidationError):
        MemberPagePermissionsUpdate(
            expected_version=0,
            page_permissions={**complete, "team": "manage"},
        )


def test_permission_read_schema_exposes_version_matrix_and_legacy_state() -> None:
    complete = matrix()
    parsed = MemberPagePermissionsRead(
        membership_id=uuid4(),
        page_permissions=complete,
        permissions_version=4,
        is_legacy=False,
    )
    assert parsed.permissions_version == 4
    assert parsed.page_permissions == complete
    assert parsed.is_legacy is False


def test_dependency_rejects_unknown_key_at_route_declaration() -> None:
    with pytest.raises(ValueError, match="Unknown page"):
        require_page_permission("patients.unknown", legacy_access="reader")


def test_explicit_member_manage_does_not_bypass_admin_role_guard() -> None:
    access = workspace_access(page_permissions=matrix("manage"))
    assert page_permission_allows(
        access.membership,
        workspace_id=access.workspace.id,
        user_id=access.user.id,
        page_key="patients.detail",
        legacy_access="admin",
    )
    with pytest.raises(HTTPException) as exc:
        get_workspace_admin(access)
    assert exc.value.status_code == 403


def test_fastapi_direct_route_denies_before_database_or_business_side_effects() -> None:
    access = workspace_access(page_permissions={"patients.detail": "manage"})
    app = FastAPI()
    page_guard = require_page_permission("patients.detail", legacy_access="admin")
    calls = {"database": 0, "business_guard": 0, "handler": 0}

    def database_dependency() -> object:
        calls["database"] += 1
        return object()

    def unchanged_ownership_guard() -> bool:
        calls["business_guard"] += 1
        raise HTTPException(status_code=409, detail="Existing ownership rules still apply.")

    @app.post("/patients/{patient_id}")
    def direct_patient_write(
        _access: Annotated[WorkspaceAccess, Depends(page_guard)],
        _db: Annotated[object, Depends(database_dependency)],
        _ownership: Annotated[bool, Depends(unchanged_ownership_guard)],
    ) -> dict[str, bool]:
        calls["handler"] += 1
        return {"allowed": True}

    app.dependency_overrides[get_workspace_access] = lambda: access
    with TestClient(app) as client:
        denied = client.post("/patients/patient-1")
        assert denied.status_code == 403
        assert calls == {"database": 0, "business_guard": 0, "handler": 0}

        complete = matrix()
        complete["patients.detail"] = "manage"
        access.membership.page_permissions = complete
        business_denied = client.post("/patients/patient-1")
        assert business_denied.status_code == 409
        assert calls == {"database": 1, "business_guard": 1, "handler": 0}


def test_fastapi_team_page_guard_is_admin_only() -> None:
    access = workspace_access(page_permissions=matrix("manage"))
    app = FastAPI()
    team_guard = require_page_permission("team", legacy_access="admin")

    @app.post("/team/permissions")
    def update_team_permissions(
        _access: Annotated[WorkspaceAccess, Depends(team_guard)],
    ) -> dict[str, bool]:
        return {"allowed": True}

    app.dependency_overrides[get_workspace_access] = lambda: access
    with TestClient(app) as client:
        assert client.post("/team/permissions").status_code == 403
        access.membership.role = "admin"
        assert client.post("/team/permissions").status_code == 200


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
    assert "RETURNING workspace_members.permissions_version" in sql


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
        build_page_permissions_compare_and_swap(
            **{**args, "page_permissions": {"dashboard": "view"}}
        )


def test_stale_compare_and_swap_returns_conflict() -> None:
    assert require_successful_permission_update(8, expected_version=7) == 8
    with pytest.raises(HTTPException) as exc:
        require_successful_permission_update(None, expected_version=7)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException):
        require_successful_permission_update(9, expected_version=7)
