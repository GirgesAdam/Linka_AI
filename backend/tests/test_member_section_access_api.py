from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.api.routes import auth as auth_routes
from app.api.routes import clinic as clinic_routes
from app.api.routes.auth import router as auth_router
from app.api.routes.clinic import router as clinic_router
from app.database.base import Base
from app.database.session import get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.schemas.agent_knowledge import (
    AgentKnowledgeSnapshot,
    KnowledgeEditApplyResponse,
    KnowledgeEditProposal,
)


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


def test_knowledge_snapshot_api_requires_administration_section(monkeypatch):
    workspace_id = uuid4()
    actor_user_id = uuid4()
    snapshot = AgentKnowledgeSnapshot(
        workspace_id=workspace_id,
        workspace_name="Knowledge Test Workspace",
        workspace_timezone="Africa/Cairo",
        branches=[],
        services=[],
        doctors=[],
        booking_settings=None,
        patients=[],
        appointments=[],
        patient_count=0,
        appointment_count=0,
    )
    builder_calls = []

    def build_snapshot(db, workspace):
        builder_calls.append(workspace.id)
        return snapshot

    monkeypatch.setattr(clinic_routes, "build_agent_knowledge_snapshot", build_snapshot)

    def client_for(
        *,
        role="member",
        section_access=None,
        active=True,
        membership_workspace_id=None,
        membership_user_id=None,
    ):
        membership = SimpleNamespace(
            workspace_id=membership_workspace_id or workspace_id,
            user_id=membership_user_id or actor_user_id,
            role=role,
            is_active=active,
            section_access=section_access,
        )
        access = WorkspaceAccess(
            user=SimpleNamespace(id=actor_user_id),
            workspace=SimpleNamespace(id=workspace_id),
            membership=membership,
        )
        app = FastAPI()
        app.include_router(clinic_router, prefix="/clinic")
        app.dependency_overrides[get_workspace_access] = lambda: access
        app.dependency_overrides[get_db] = lambda: object()
        return TestClient(app)

    cases = [
        ("admin", {"role": "admin", "section_access": None}, 200),
        (
            "member_with_administration",
            {
                "role": "member",
                "section_access": {"clinic_operations": False, "administration": True},
            },
            200,
        ),
        (
            "member_with_both_sections_closed",
            {
                "role": "member",
                "section_access": {"clinic_operations": False, "administration": False},
            },
            403,
        ),
        (
            "member_with_clinic_operations_only",
            {
                "role": "member",
                "section_access": {"clinic_operations": True, "administration": False},
            },
            403,
        ),
        (
            "inactive_member",
            {
                "role": "member",
                "section_access": {"clinic_operations": False, "administration": True},
                "active": False,
            },
            403,
        ),
        (
            "cross_workspace_membership",
            {
                "role": "member",
                "section_access": {"clinic_operations": False, "administration": True},
                "membership_workspace_id": uuid4(),
            },
            403,
        ),
        (
            "cross_user_membership",
            {
                "role": "member",
                "section_access": {"clinic_operations": False, "administration": True},
                "membership_user_id": uuid4(),
            },
            403,
        ),
    ]

    for label, options, expected_status in cases:
        client = client_for(**options)
        with client:
            response = client.get("/clinic/knowledge")
        assert response.status_code == expected_status, label
        if expected_status == 200:
            assert response.json()["workspace_id"] == str(workspace_id)
        else:
            assert "patient" not in response.text.lower(), label

    assert builder_calls == [workspace_id, workspace_id]


def test_knowledge_ai_propose_and_apply_require_administration_section(monkeypatch):
    workspace_id = uuid4()
    actor_user_id = uuid4()
    propose_calls = []
    apply_calls = []

    def propose_edit(_db, workspace, message):
        propose_calls.append((workspace.id, message))
        return KnowledgeEditProposal(
            base_fingerprint="a" * 64,
            assistant_message="Prepared a safe preview.",
            preview_lines=["Service details would be updated."],
            actions=[],
            requires_confirmation=False,
        )

    def apply_edit(_db, workspace, *, base_fingerprint, actions, actor_user_id):
        apply_calls.append((workspace.id, base_fingerprint, len(actions), actor_user_id))
        return KnowledgeEditApplyResponse(
            assistant_message="The approved change was applied.",
            applied_actions=1,
        )

    monkeypatch.setattr(clinic_routes, "propose_agent_knowledge_edit", propose_edit)
    monkeypatch.setattr(clinic_routes, "apply_agent_knowledge_edit", apply_edit)

    def client_for(
        *,
        role="member",
        section_access=None,
        active=True,
        membership_workspace_id=None,
        membership_user_id=None,
    ):
        membership = SimpleNamespace(
            workspace_id=membership_workspace_id or workspace_id,
            user_id=membership_user_id or actor_user_id,
            role=role,
            is_active=active,
            section_access=section_access,
        )
        access = WorkspaceAccess(
            user=SimpleNamespace(id=actor_user_id),
            workspace=SimpleNamespace(id=workspace_id),
            membership=membership,
        )
        app = FastAPI()
        app.include_router(clinic_router, prefix="/clinic")
        app.dependency_overrides[get_workspace_access] = lambda: access
        app.dependency_overrides[get_db] = lambda: object()
        return TestClient(app)

    propose_payload = {"message": "Update the clinic opening hours."}
    apply_payload = {
        "base_fingerprint": "a" * 64,
        "actions": [{"kind": "update_service"}],
    }
    cases = [
        ("admin", {"role": "admin"}, 200),
        (
            "member_with_administration",
            {
                "role": "member",
                "section_access": {"clinic_operations": False, "administration": True},
            },
            200,
        ),
        (
            "member_with_both_sections_closed",
            {
                "role": "member",
                "section_access": {"clinic_operations": False, "administration": False},
            },
            403,
        ),
        (
            "member_with_clinic_operations_only",
            {
                "role": "member",
                "section_access": {"clinic_operations": True, "administration": False},
            },
            403,
        ),
        (
            "inactive_member_with_administration",
            {
                "role": "member",
                "section_access": {"clinic_operations": False, "administration": True},
                "active": False,
            },
            403,
        ),
        (
            "cross_workspace_member_with_administration",
            {
                "role": "member",
                "section_access": {"clinic_operations": False, "administration": True},
                "membership_workspace_id": uuid4(),
            },
            403,
        ),
    ]

    for label, options, expected_status in cases:
        client = client_for(**options)
        with client:
            propose_response = client.post(
                "/clinic/knowledge/ai/propose",
                json=propose_payload,
            )
            apply_response = client.post(
                "/clinic/knowledge/ai/apply",
                json=apply_payload,
            )
        assert propose_response.status_code == expected_status, label
        assert apply_response.status_code == expected_status, label
        if expected_status == 200:
            assert propose_response.json()["base_fingerprint"] == "a" * 64
            assert apply_response.json()["applied_actions"] == 1

    assert propose_calls == [
        (workspace_id, propose_payload["message"]),
        (workspace_id, propose_payload["message"]),
    ]
    assert apply_calls == [
        (workspace_id, "a" * 64, 1, actor_user_id),
        (workspace_id, "a" * 64, 1, actor_user_id),
    ]


def test_knowledge_ai_propose_and_apply_require_authentication():
    app = FastAPI()
    app.include_router(clinic_router, prefix="/clinic")
    app.dependency_overrides[get_db] = lambda: None
    client = TestClient(app)
    headers = {"X-Workspace-ID": str(uuid4())}

    with client:
        propose_response = client.post(
            "/clinic/knowledge/ai/propose",
            json={"message": "Update the clinic opening hours."},
            headers=headers,
        )
        apply_response = client.post(
            "/clinic/knowledge/ai/apply",
            json={
                "base_fingerprint": "a" * 64,
                "actions": [{"kind": "update_service"}],
            },
            headers=headers,
        )

    assert propose_response.status_code == 401
    assert apply_response.status_code == 401


def test_knowledge_snapshot_api_requires_authentication():
    app = FastAPI()
    app.include_router(clinic_router, prefix="/clinic")
    app.dependency_overrides[get_db] = lambda: None
    client = TestClient(app)

    response = client.get(
        "/clinic/knowledge",
        headers={"X-Workspace-ID": str(uuid4())},
    )

    assert response.status_code == 401
