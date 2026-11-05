"""Backend-only foundation checks: no business routes are bound in this PR."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.api.dependencies.page_permissions import require_page_permission
from app.api.dependencies.security import (
    WorkspaceAccess,
    get_workspace_admin,
    get_workspace_reader,
)
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.schemas.page_permissions import MemberPagePermissionsUpdate
from app.services.page_permissions import (
    PAGE_PERMISSION_KEY_SET,
    PAGE_PERMISSION_KEYS,
    build_page_permissions_compare_and_swap,
    page_permission_allows,
    require_successful_permission_update,
    validate_complete_page_permissions,
)


def _full_matrix(level: str = "none") -> dict[str, str]:
    return dict.fromkeys(PAGE_PERMISSION_KEYS, level)


def _access(
    *, role: str = "member", page_permissions: dict[str, str] | None = None,
    active: bool = True,
) -> WorkspaceAccess:
    workspace_id = uuid4()
    user_id = uuid4()
    return WorkspaceAccess(
        workspace=SimpleNamespace(id=workspace_id, is_active=True),
        user=SimpleNamespace(id=user_id, is_active=True),
        membership=WorkspaceMember(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            is_active=active,
            page_permissions=page_permissions,
        ),
    )


def _allowed(access: WorkspaceAccess, key: str, action: str) -> bool:
    return page_permission_allows(
        access.membership,
        workspace_id=access.workspace.id,
        user_id=access.user.id,
        page_key=key,
        action=action,
    )


def test_catalog_is_exactly_25_independent_page_keys() -> None:
    expected = {
        "dashboard", "inbox.list", "inbox.detail", "appointments.calendar",
        "appointments.detail", "appointments.reschedule", "patients.list",
        "patients.detail", "tasks", "doctors", "services", "inventory",
        "analytics.overview", "analytics.campaigns", "analytics.campaign_detail",
        "analytics.cohort_detail", "analytics.outstanding_balances", "finance",
        "automations", "activity", "team", "setup", "setup.whatsapp",
        "demo.home", "demo.agent",
    }
    assert len(PAGE_PERMISSION_KEYS) == 25
    assert set(PAGE_PERMISSION_KEYS) == expected == PAGE_PERMISSION_KEY_SET
    assert len(set(PAGE_PERMISSION_KEYS)) == len(PAGE_PERMISSION_KEYS)


def test_admin_has_full_access_regardless_of_stored_map() -> None:
    for stored in (None, {}, {"patients.detail": "none"}, {"unexpected": "invalid"}):
        access = _access(role="admin", page_permissions=stored)
        for key in PAGE_PERMISSION_KEYS:
            assert _allowed(access, key, "view")
            assert _allowed(access, key, "manage")
        assert get_workspace_admin(access) is access


def test_member_null_uses_unchanged_legacy_role_policy() -> None:
    access = _access()
    assert access.membership.page_permissions is None
    assert get_workspace_reader(access) is access
    for key in PAGE_PERMISSION_KEYS:
        assert _allowed(access, key, "view")
        assert _allowed(access, key, "manage") is (key != "team")
    # The page gate is additive, not a replacement for existing admin-only checks.
    with pytest.raises(HTTPException) as exc:
        get_workspace_admin(access)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    ("level", "view", "manage"),
    [("none", False, False), ("view", True, False), ("manage", True, True)],
)
def test_explicit_member_levels(level: str, view: bool, manage: bool) -> None:
    access = _access(page_permissions={"patients.detail": level})
    assert _allowed(access, "patients.detail", "view") is view
    assert _allowed(access, "patients.detail", "manage") is manage
    assert not _allowed(access, "patients.list", "view")


def test_team_management_remains_admin_only() -> None:
    assert not _allowed(_access(), "team", "manage")
    assert not _allowed(_access(page_permissions={"team": "manage"}), "team", "manage")
    assert _allowed(_access(role="admin", page_permissions={"team": "none"}), "team", "manage")


def test_other_page_permission_does_not_grant_access() -> None:
    access = _access(page_permissions={"patients.detail": "manage", "finance": "none"})
    assert _allowed(access, "patients.detail", "manage")
    assert not _allowed(access, "finance", "view")
    assert not _allowed(access, "appointments.detail", "view")


def test_unknown_keys_and_actions_fail_during_dependency_configuration() -> None:
    with pytest.raises(ValueError, match="Unknown page"):
        require_page_permission("patients.made_up", "view")
    with pytest.raises(ValueError, match="Unknown page permission action"):
        require_page_permission("patients.detail", "delete")
    with pytest.raises(ValueError):
        _allowed(_access(), "patients.made_up", "view")


def test_matrix_validation_rejects_unknown_missing_and_invalid_values() -> None:
    good = _full_matrix("none")
    good["patients.detail"] = "manage"
    assert validate_complete_page_permissions(good) == good
    with pytest.raises(ValueError, match="missing"):
        validate_complete_page_permissions({"patients.detail": "view"})
    with pytest.raises(ValueError, match="unknown"):
        validate_complete_page_permissions({**good, "bad.key": "view"})
    with pytest.raises(ValueError, match="Invalid page permission level"):
        validate_complete_page_permissions({**good, "inventory": "superadmin"})
    with pytest.raises(ValueError, match="Only Admins"):
        validate_complete_page_permissions({**good, "team": "manage"})
    with pytest.raises(ValueError, match="must be an object"):
        validate_complete_page_permissions(None)


def test_schema_validates_complete_matrix_and_version() -> None:
    matrix = _full_matrix()
    saved = MemberPagePermissionsUpdate(expected_version=0, page_permissions=matrix)
    assert saved.expected_version == 0
    assert saved.page_permissions == matrix
    for invalid in ({"bad.key": "view"}, {"patients.detail": "view"}):
        with pytest.raises(ValidationError):
            MemberPagePermissionsUpdate(expected_version=0, page_permissions=invalid)
    with pytest.raises(ValidationError):
        MemberPagePermissionsUpdate(
            expected_version=0, page_permissions={**matrix, "inventory": "write"}
        )
    for invalid in (-1, True, "3"):
        with pytest.raises(ValidationError):
            MemberPagePermissionsUpdate(expected_version=invalid, page_permissions=matrix)


def test_invalid_explicit_stored_matrix_fails_closed() -> None:
    for stored in (
        {"patients.detail": "superadmin"},
        {"patients.detail": "manage", "team": "manage"},
        {"patients.detail": "manage", "future.page": "view"},
        {"patients.detail": 2},
        [],
    ):
        access = _access(page_permissions=stored)
        assert not _allowed(access, "patients.detail", "view")
        assert not _allowed(access, "patients.detail", "manage")


def test_inactive_membership_and_cross_workspace_access_denied() -> None:
    inactive = _access(role="admin", active=False)
    assert not _allowed(inactive, "dashboard", "view")
    access = _access(page_permissions={"patients.detail": "manage"})
    access.membership.workspace_id = uuid4()
    assert not _allowed(access, "patients.detail", "manage")
    access.membership.workspace_id = access.workspace.id
    access.membership.user_id = uuid4()
    assert not _allowed(access, "patients.detail", "view")


def test_fastapi_dependency_blocks_direct_url_and_manage_bypass() -> None:
    app = FastAPI()
    view_gate = require_page_permission("patients.detail", "view")
    manage_gate = require_page_permission("patients.detail", "manage")

    @app.get("/protected")
    def read(_access: Annotated[WorkspaceAccess, Depends(view_gate)]) -> dict[str, bool]:
        return {"allowed": True}

    @app.post("/protected")
    def write(_access: Annotated[WorkspaceAccess, Depends(manage_gate)]) -> dict[str, bool]:
        return {"allowed": True}

    access = _access(page_permissions={"patients.detail": "view"})
    app.dependency_overrides[get_workspace_reader] = lambda: access
    with TestClient(app) as client:
        assert client.get("/protected").status_code == 200
        assert client.post("/protected").status_code == 403
        access.membership.page_permissions = {"patients.detail": "manage"}
        assert client.post("/protected").status_code == 200
        access.membership.page_permissions = {"patients.detail": "none"}
        assert client.get("/protected").status_code == 403
        access.membership.is_active = False
        assert client.get("/protected").status_code == 403


def test_atomic_cas_builder_scopes_workspace_actor_role_and_version() -> None:
    actor_id, workspace_id, membership_id = uuid4(), uuid4(), uuid4()
    matrix = _full_matrix("view")
    stmt = build_page_permissions_compare_and_swap(
        workspace_id=workspace_id, membership_id=membership_id,
        actor_user_id=actor_id, expected_version=12, page_permissions=matrix,
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "UPDATE workspace_members" in sql
    assert "workspace_members.workspace_id =" in sql
    assert "workspace_members.id =" in sql
    assert "workspace_members.user_id !=" in sql
    assert "workspace_members.is_active IS true" in sql
    assert "workspace_members.role =" in sql
    assert "workspace_members.permissions_version =" in sql
    assert "workspace_members.permissions_version + " in sql
    assert "RETURNING workspace_members.permissions_version" in sql
    assert require_successful_permission_update(13, expected_version=12) == 13
    with pytest.raises(HTTPException) as exc:
        require_successful_permission_update(None, expected_version=12)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException):
        require_successful_permission_update(14, expected_version=12)
    for invalid in (-1, True, "4"):
        with pytest.raises(ValueError):
            build_page_permissions_compare_and_swap(
                workspace_id=workspace_id, membership_id=membership_id,
                actor_user_id=actor_id, expected_version=invalid,
                page_permissions=matrix,
            )


def _isolated_ci_engine():
    url = make_url(os.environ["DATABASE_URL"])
    if url.host not in {"localhost", "127.0.0.1", "::1"} or url.database != "ci_db":
        pytest.fail("Permission storage tests require disposable local PostgreSQL ci_db.")
    return create_engine(url, connect_args={"connect_timeout": 3})


def test_migration_columns_and_legacy_defaults_on_isolated_postgresql() -> None:
    engine = _isolated_ci_engine()
    try:
        columns = {col["name"]: col for col in inspect(engine).get_columns("workspace_members")}
        assert str(columns["page_permissions"]["type"]).upper() == "JSONB"
        assert columns["page_permissions"]["nullable"] is True
        assert str(columns["permissions_version"]["type"]).upper() == "INTEGER"
        assert columns["permissions_version"]["nullable"] is False
        assert "0" in str(columns["permissions_version"]["default"])

        conn = engine.connect()
        transaction = conn.begin()
        try:
            db = Session(bind=conn, join_transaction_mode="create_savepoint")
            workspace = Workspace(name="Permission CI", slug=f"permission-{uuid4().hex[:12]}")
            user = User(email=f"permission-{uuid4()}@example.test", auth_user_id=uuid4())
            db.add_all([workspace, user])
            db.flush()
            membership_id = uuid4()
            value = db.execute(
                text(
                    "INSERT INTO workspace_members "
                    "(id, workspace_id, user_id, role, is_active) "
                    "VALUES (:id, :wid, :uid, 'member', true) "
                    "RETURNING page_permissions, permissions_version"
                ),
                {"id": membership_id, "wid": workspace.id, "uid": user.id},
            ).one()
            assert value.page_permissions is None
            assert value.permissions_version == 0
            membership = db.get(WorkspaceMember, membership_id)
            assert membership is not None
            assert membership.page_permissions is None
            assert membership.permissions_version == 0

            admin_id = uuid4()
            new = db.execute(
                build_page_permissions_compare_and_swap(
                    workspace_id=workspace.id, membership_id=membership_id,
                    actor_user_id=admin_id, expected_version=0,
                    page_permissions=_full_matrix("view"),
                )
            ).scalar_one_or_none()
            assert require_successful_permission_update(new, expected_version=0) == 1
            assert db.execute(
                build_page_permissions_compare_and_swap(
                    workspace_id=workspace.id, membership_id=membership_id,
                    actor_user_id=admin_id, expected_version=0,
                    page_permissions=_full_matrix("manage"),
                )
            ).scalar_one_or_none() is None
            assert db.execute(
                build_page_permissions_compare_and_swap(
                    workspace_id=workspace.id, membership_id=membership_id,
                    actor_user_id=user.id, expected_version=1,
                    page_permissions=_full_matrix("manage"),
                )
            ).scalar_one_or_none() is None
            assert db.execute(
                build_page_permissions_compare_and_swap(
                    workspace_id=uuid4(), membership_id=membership_id,
                    actor_user_id=admin_id, expected_version=1,
                    page_permissions=_full_matrix("manage"),
                )
            ).scalar_one_or_none() is None
            db.expire_all()
            saved = db.get(WorkspaceMember, membership_id)
            assert saved is not None
            assert saved.permissions_version == 1
            assert saved.page_permissions == _full_matrix("view")
            db.close()
        finally:
            if transaction.is_active:
                transaction.rollback()
            conn.close()
    finally:
        engine.dispose()
