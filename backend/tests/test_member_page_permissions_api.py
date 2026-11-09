"""Direct API regressions for Admin-managed Member page permissions."""

from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.api.routes.auth import router as auth_router
from app.database.base import Base
from app.database.session import get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.page_permissions import (
    PAGE_PERMISSION_KEYS,
    empty_member_page_permissions,
)


def _matrix(granted_key: str | None = None) -> dict[str, str]:
    return {
        key: ("manage" if key == granted_key and key != "team" else "none")
        for key in PAGE_PERMISSION_KEYS
    }


def _database():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[User.__table__, Workspace.__table__, WorkspaceMember.__table__],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as db:
        workspace = Workspace(name="Permissions API", slug=f"permissions-{uuid4().hex}")
        other_workspace = Workspace(name="Other Workspace", slug=f"other-{uuid4().hex}")
        actor = User(email=f"admin-{uuid4()}@example.test", auth_user_id=uuid4())
        target = User(email=f"member-{uuid4()}@example.test", auth_user_id=uuid4())
        inactive_target = User(email=f"inactive-{uuid4()}@example.test", auth_user_id=uuid4())
        admin_target = User(email=f"other-admin-{uuid4()}@example.test", auth_user_id=uuid4())
        cross_workspace_target = User(
            email=f"cross-workspace-{uuid4()}@example.test",
            auth_user_id=uuid4(),
        )
        db.add_all(
            [
                workspace,
                other_workspace,
                actor,
                target,
                inactive_target,
                admin_target,
                cross_workspace_target,
            ]
        )
        db.flush()

        admin_membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=actor.id,
            role="admin",
            is_active=True,
            page_permissions=None,
            permissions_version=0,
        )
        target_membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=target.id,
            role="member",
            is_active=True,
            page_permissions=None,
            permissions_version=0,
        )
        inactive_membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=inactive_target.id,
            role="member",
            is_active=False,
            page_permissions=None,
            permissions_version=0,
        )
        admin_target_membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=admin_target.id,
            role="admin",
            is_active=True,
            page_permissions=None,
            permissions_version=0,
        )
        cross_workspace_membership = WorkspaceMember(
            workspace_id=other_workspace.id,
            user_id=cross_workspace_target.id,
            role="member",
            is_active=True,
            page_permissions=None,
            permissions_version=0,
        )
        db.add_all(
            [
                admin_membership,
                target_membership,
                inactive_membership,
                admin_target_membership,
                cross_workspace_membership,
            ]
        )
        db.commit()

        ids = {
            "workspace_id": workspace.id,
            "actor_id": actor.id,
            "target_id": target_membership.id,
            "target_user_id": target.id,
            "inactive_id": inactive_membership.id,
            "admin_target_id": admin_target_membership.id,
            "cross_workspace_id": cross_workspace_membership.id,
        }

    return engine, factory, ids


def _client(factory, *, workspace_id, actor_id, role="admin", user_id=None):
    app = FastAPI()
    app.include_router(auth_router, prefix="/auth")
    access = WorkspaceAccess(
        user=SimpleNamespace(id=user_id or actor_id),
        workspace=SimpleNamespace(id=workspace_id, is_active=True),
        membership=SimpleNamespace(role=role),
    )
    app.dependency_overrides[get_workspace_access] = lambda: access

    def test_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = test_get_db
    return TestClient(app), access


def test_permission_api_is_admin_only_and_denies_before_sql_side_effects() -> None:
    engine, factory, ids = _database()
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    client, _access = _client(
        factory,
        workspace_id=ids["workspace_id"],
        actor_id=ids["actor_id"],
        role="member",
    )
    payload = {"expected_version": 0, "page_permissions": _matrix("patients.detail")}
    try:
        with client:
            read = client.get(f"/auth/workspace/members/{ids['target_id']}/page-permissions")
            write = client.put(
                f"/auth/workspace/members/{ids['target_id']}/page-permissions",
                json=payload,
            )
        assert read.status_code == 403
        assert write.status_code == 403
        assert statements == []
    finally:
        engine.dispose()


def test_permission_api_reads_legacy_state_and_scopes_targets() -> None:
    engine, factory, ids = _database()
    client, _access = _client(
        factory,
        workspace_id=ids["workspace_id"],
        actor_id=ids["actor_id"],
    )
    try:
        payload = {"expected_version": 0, "page_permissions": _matrix("patients.detail")}
        with client:
            response = client.get(
                f"/auth/workspace/members/{ids['target_id']}/page-permissions"
            )
            cross_workspace = client.get(
                f"/auth/workspace/members/{ids['cross_workspace_id']}/page-permissions"
            )
            cross_workspace_write = client.put(
                f"/auth/workspace/members/{ids['cross_workspace_id']}/page-permissions",
                json=payload,
            )
            inactive = client.get(
                f"/auth/workspace/members/{ids['inactive_id']}/page-permissions"
            )
            inactive_write = client.put(
                f"/auth/workspace/members/{ids['inactive_id']}/page-permissions",
                json=payload,
            )
            admin_target = client.get(
                f"/auth/workspace/members/{ids['admin_target_id']}/page-permissions"
            )
            admin_target_write = client.put(
                f"/auth/workspace/members/{ids['admin_target_id']}/page-permissions",
                json=payload,
            )

        assert response.status_code == 200
        assert response.json() == {
            "membership_id": str(ids["target_id"]),
            "page_permissions": None,
            "permissions_version": 0,
            "is_legacy": True,
        }
        assert cross_workspace.status_code == 404
        assert cross_workspace_write.status_code == 404
        assert inactive.status_code == 404
        assert inactive_write.status_code == 404
        assert admin_target.status_code == 404
        assert admin_target_write.status_code == 404
    finally:
        engine.dispose()


def test_permission_api_updates_complete_matrix_and_rejects_stale_cas() -> None:
    engine, factory, ids = _database()
    client, _access = _client(
        factory,
        workspace_id=ids["workspace_id"],
        actor_id=ids["actor_id"],
    )
    permissions = _matrix("patients.detail")
    payload = {"expected_version": 0, "page_permissions": permissions}
    try:
        with client:
            updated = client.put(
                f"/auth/workspace/members/{ids['target_id']}/page-permissions",
                json=payload,
            )
            stale = client.put(
                f"/auth/workspace/members/{ids['target_id']}/page-permissions",
                json=payload,
            )
            incomplete = client.put(
                f"/auth/workspace/members/{ids['target_id']}/page-permissions",
                json={
                    "expected_version": 1,
                    "page_permissions": {"patients.detail": "manage"},
                },
            )
            team_grant = _matrix()
            team_grant["team"] = "manage"
            invalid_team = client.put(
                f"/auth/workspace/members/{ids['target_id']}/page-permissions",
                json={"expected_version": 1, "page_permissions": team_grant},
            )

        assert updated.status_code == 200
        assert updated.json()["permissions_version"] == 1
        assert updated.json()["page_permissions"] == permissions
        assert updated.json()["is_legacy"] is False
        assert stale.status_code == 409
        assert incomplete.status_code == 422
        assert invalid_team.status_code == 422

        with factory() as db:
            saved = db.get(WorkspaceMember, ids["target_id"])
            assert saved is not None
            assert saved.permissions_version == 1
            assert saved.page_permissions == permissions
    finally:
        engine.dispose()


def test_permission_api_self_target_check_is_retained() -> None:
    engine, factory, ids = _database()
    client, _access = _client(
        factory,
        workspace_id=ids["workspace_id"],
        actor_id=ids["actor_id"],
        role="admin",
        user_id=ids["target_user_id"],
    )
    try:
        payload = {"expected_version": 0, "page_permissions": _matrix("patients.detail")}
        with client:
            response = client.get(
                f"/auth/workspace/members/{ids['target_id']}/page-permissions"
            )
            write = client.put(
                f"/auth/workspace/members/{ids['target_id']}/page-permissions",
                json=payload,
            )
        assert response.status_code == 404
        assert write.status_code == 404
    finally:
        engine.dispose()


def test_new_member_invitation_starts_with_complete_all_none_matrix(monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes.auth.record_activity_event", lambda *args, **kwargs: None)
    engine, factory, ids = _database()
    new_user_id = uuid4()
    with factory() as db:
        user = User(
            email=f"new-member-{new_user_id}@example.test",
            auth_user_id=uuid4(),
        )
        db.add(user)
        db.commit()
        email = user.email

    client, _access = _client(
        factory, workspace_id=ids["workspace_id"], actor_id=ids["actor_id"]
    )
    try:
        with client:
            response = client.post(
                "/auth/workspace/invitations", json={"email": email, "role": "member"}
            )
        assert response.status_code == 201, response.text
        membership_id = response.json()["membership"]["membership_id"]
        with factory() as db:
            created = db.get(WorkspaceMember, UUID(membership_id))
            assert created is not None
            assert created.page_permissions == empty_member_page_permissions()
            assert set(created.page_permissions) == set(PAGE_PERMISSION_KEYS)
            assert set(created.page_permissions.values()) == {"none"}
    finally:
        engine.dispose()


def test_admin_role_changed_to_member_receives_all_none_without_rewriting_legacy_members(monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes.auth.record_activity_event", lambda *args, **kwargs: None)
    engine, factory, ids = _database()
    client, _access = _client(
        factory, workspace_id=ids["workspace_id"], actor_id=ids["actor_id"]
    )
    try:
        with client:
            response = client.patch(
                f"/auth/workspace/members/{ids['admin_target_id']}",
                json={"role": "member"},
            )
        assert response.status_code == 200, response.text
        with factory() as db:
            demoted = db.get(WorkspaceMember, ids["admin_target_id"])
            legacy_member = db.get(WorkspaceMember, ids["target_id"])
            assert demoted is not None
            assert demoted.page_permissions == empty_member_page_permissions()
            assert legacy_member is not None
            assert legacy_member.page_permissions is None
    finally:
        engine.dispose()
