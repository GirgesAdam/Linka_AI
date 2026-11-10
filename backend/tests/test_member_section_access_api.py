from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.api.routes import auth as auth_routes
from app.api.routes.auth import router as auth_router
from app.database.base import Base
from app.database.session import get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember


def _database():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Workspace.__table__,
            WorkspaceMember.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        workspace = Workspace(name="Section Access", slug=f"section-{uuid4().hex}")
        other_workspace = Workspace(name="Other Workspace", slug=f"other-{uuid4().hex}")
        actor = User(email=f"admin-{uuid4()}@example.test", auth_user_id=uuid4())
        target = User(email=f"member-{uuid4()}@example.test", auth_user_id=uuid4())
        invite_user = User(email=f"invite-{uuid4()}@example.test", auth_user_id=uuid4())
        inactive_user = User(email=f"inactive-{uuid4()}@example.test", auth_user_id=uuid4())
        other_user = User(email=f"other-{uuid4()}@example.test", auth_user_id=uuid4())
        admin_user = User(email=f"other-admin-{uuid4()}@example.test", auth_user_id=uuid4())
        db.add_all([workspace, other_workspace, actor, target, invite_user, inactive_user, other_user, admin_user])
        db.flush()

        admin_membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=actor.id,
            role="admin",
            is_active=True,
            section_access=None,
        )
        target_membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=target.id,
            role="member",
            is_active=True,
            section_access=None,
            permissions_version=0,
        )
        inactive_membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=inactive_user.id,
            role="member",
            is_active=False,
            section_access=None,
        )
        other_workspace_membership = WorkspaceMember(
            workspace_id=other_workspace.id,
            user_id=other_user.id,
            role="member",
            is_active=True,
            section_access=None,
        )
        admin_target_membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=admin_user.id,
            role="admin",
            is_active=True,
            section_access=None,
        )
        db.add_all([
            admin_membership,
            target_membership,
            inactive_membership,
            other_workspace_membership,
            admin_target_membership,
        ])
        db.commit()
        ids = {
            "workspace_id": workspace.id,
            "actor_id": actor.id,
            "target_id": target_membership.id,
            "target_user_id": target.id,
            "invite_user_id": invite_user.id,
            "invite_email": invite_user.email,
            "inactive_id": inactive_membership.id,
            "cross_workspace_id": other_workspace_membership.id,
            "admin_target_id": admin_target_membership.id,
        }
    return engine, factory, ids


def _client(factory, *, workspace_id, actor_id, role="admin", membership_role=None):
    app = FastAPI()
    app.include_router(auth_router, prefix="/auth")
    access = WorkspaceAccess(
        user=SimpleNamespace(id=actor_id),
        workspace=SimpleNamespace(id=workspace_id, is_active=True),
        membership=SimpleNamespace(role=membership_role or role),
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


def test_section_access_api_is_admin_only_and_reads_legacy_state_safely() -> None:
    engine, factory, ids = _database()
    member_client, _ = _client(
        factory,
        workspace_id=ids["workspace_id"],
        actor_id=ids["target_user_id"],
        role="member",
    )
    admin_client, _ = _client(
        factory,
        workspace_id=ids["workspace_id"],
        actor_id=ids["actor_id"],
    )
    try:
        with member_client:
            denied = member_client.get(
                f"/auth/workspace/members/{ids['target_id']}/section-access"
            )
        with admin_client:
            legacy = admin_client.get(
                f"/auth/workspace/members/{ids['target_id']}/section-access"
            )
            cross_workspace = admin_client.get(
                f"/auth/workspace/members/{ids['cross_workspace_id']}/section-access"
            )
            inactive = admin_client.get(
                f"/auth/workspace/members/{ids['inactive_id']}/section-access"
            )
            admin_target = admin_client.get(
                f"/auth/workspace/members/{ids['admin_target_id']}/section-access"
            )
        assert denied.status_code == 403
        assert legacy.status_code == 200
        assert legacy.json() == {
            "membership_id": str(ids["target_id"]),
            "section_access": {"clinic_operations": False, "administration": False},
            "permissions_version": 0,
            "is_legacy": True,
        }
        assert cross_workspace.status_code == 404
        assert inactive.status_code == 404
        assert admin_target.status_code == 404
    finally:
        engine.dispose()


def test_section_access_update_uses_two_flags_and_compare_and_swap() -> None:
    engine, factory, ids = _database()
    client, _ = _client(factory, workspace_id=ids["workspace_id"], actor_id=ids["actor_id"])
    url = f"/auth/workspace/members/{ids['target_id']}/section-access"
    payload = {
        "expected_version": 0,
        "section_access": {"clinic_operations": True, "administration": False},
    }
    try:
        with client:
            updated = client.put(url, json=payload)
            stale = client.put(url, json=payload)
            malformed = client.put(
                url,
                json={
                    "expected_version": 1,
                    "section_access": {
                        "clinic_operations": 1,
                        "administration": False,
                    },
                },
            )
        assert updated.status_code == 200
        assert updated.json()["section_access"] == {
            "clinic_operations": True,
            "administration": False,
        }
        assert updated.json()["permissions_version"] == 1
        assert updated.json()["is_legacy"] is False
        assert stale.status_code == 409
        assert malformed.status_code == 422

        with factory() as db:
            membership = db.scalar(
                select(WorkspaceMember).where(WorkspaceMember.id == ids["target_id"])
            )
            assert membership is not None
            assert membership.section_access == {
                "clinic_operations": True,
                "administration": False,
            }
            assert membership.permissions_version == 1
    finally:
        engine.dispose()


def test_new_member_invitation_defaults_closed_and_role_changes_reset_flags(monkeypatch) -> None:
    monkeypatch.setattr(auth_routes, "record_activity_event", lambda *_args, **_kwargs: None)
    engine, factory, ids = _database()
    client, _ = _client(factory, workspace_id=ids["workspace_id"], actor_id=ids["actor_id"])
    try:
        with client:
            invited = client.post(
                "/auth/workspace/invitations",
                json={"email": ids["invite_email"], "role": "member"},
            )
        assert invited.status_code == 201
        invited_membership_id = invited.json()["membership"]["membership_id"]
        with factory() as db:
            invited_membership = db.scalar(
                select(WorkspaceMember).where(WorkspaceMember.id == UUID(invited_membership_id))
            )
            target_membership = db.scalar(
                select(WorkspaceMember).where(WorkspaceMember.id == ids["target_id"])
            )
            assert invited_membership is not None
            assert invited_membership.section_access == {
                "clinic_operations": False,
                "administration": False,
            }
            assert target_membership is not None
            target_membership.section_access = {
                "clinic_operations": True,
                "administration": True,
            }
            target_membership.permissions_version = 4
            db.commit()

        with client:
            promoted = client.patch(
                f"/auth/workspace/members/{ids['target_id']}",
                json={"role": "admin"},
            )
            demoted = client.patch(
                f"/auth/workspace/members/{ids['target_id']}",
                json={"role": "member"},
            )
        assert promoted.status_code == 200
        assert demoted.status_code == 200
        with factory() as db:
            target_membership = db.scalar(
                select(WorkspaceMember).where(WorkspaceMember.id == ids["target_id"])
            )
            assert target_membership is not None
            assert target_membership.role == "member"
            assert target_membership.section_access == {
                "clinic_operations": False,
                "administration": False,
            }
            assert target_membership.permissions_version == 6
    finally:
        engine.dispose()
