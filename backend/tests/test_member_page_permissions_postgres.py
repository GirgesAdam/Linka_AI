from __future__ import annotations

import os
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.page_permissions import (
    PAGE_PERMISSION_KEYS,
    build_page_permissions_compare_and_swap,
    empty_member_page_permissions,
    page_permission_allows,
)


def test_postgres_member_permission_defaults_legacy_override_cas_and_isolation() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    assert url.host in {"localhost", "127.0.0.1", "::1"}
    assert url.database == "ci_db"
    engine = create_engine(url)
    workspace_id = None
    other_workspace_id = None
    user_ids: list[object] = []
    try:
        with Session(engine) as db, db.begin():
            workspace = Workspace(name="Permissions PG", slug=f"permissions-pg-{uuid4().hex}")
            other_workspace = Workspace(name="Permissions PG Other", slug=f"permissions-pg-{uuid4().hex}")
            actor = User(email=f"permissions-actor-{uuid4().hex}@example.test", auth_user_id=uuid4())
            member_user = User(email=f"permissions-member-{uuid4().hex}@example.test", auth_user_id=uuid4())
            legacy_user = User(email=f"permissions-legacy-{uuid4().hex}@example.test", auth_user_id=uuid4())
            cross_user = User(email=f"permissions-cross-{uuid4().hex}@example.test", auth_user_id=uuid4())
            db.add_all([workspace, other_workspace, actor, member_user, legacy_user, cross_user])
            db.flush()
            workspace_id, other_workspace_id = workspace.id, other_workspace.id
            user_ids = [actor.id, member_user.id, legacy_user.id, cross_user.id]

            admin = WorkspaceMember(workspace_id=workspace.id, user_id=actor.id, role="admin")
            new_member = WorkspaceMember(
                workspace_id=workspace.id,
                user_id=member_user.id,
                role="member",
                page_permissions=empty_member_page_permissions(),
            )
            legacy_member = WorkspaceMember(
                workspace_id=workspace.id,
                user_id=legacy_user.id,
                role="member",
                page_permissions=None,
            )
            cross_member = WorkspaceMember(
                workspace_id=other_workspace.id,
                user_id=cross_user.id,
                role="member",
                page_permissions=empty_member_page_permissions(),
            )
            db.add_all([admin, new_member, legacy_member, cross_member])
            db.flush()

            assert set(new_member.page_permissions or {}) == set(PAGE_PERMISSION_KEYS)
            assert set((new_member.page_permissions or {}).values()) == {"none"}
            assert all(
                page_permission_allows(
                    new_member,
                    workspace_id=workspace.id,
                    user_id=member_user.id,
                    page_key=key,
                    legacy_access="reader",
                ) is False
                for key in PAGE_PERMISSION_KEYS
            )
            assert all(
                page_permission_allows(
                    admin,
                    workspace_id=workspace.id,
                    user_id=actor.id,
                    page_key=key,
                    legacy_access="admin",
                )
                for key in PAGE_PERMISSION_KEYS
            )
            assert legacy_member.page_permissions is None
            for key in ("inventory", "services", "automations", "setup"):
                assert not page_permission_allows(
                    legacy_member,
                    workspace_id=workspace.id,
                    user_id=legacy_user.id,
                    page_key=key,
                    legacy_access="admin",
                )

            explicit = empty_member_page_permissions()
            explicit["inventory"] = "manage"
            statement = build_page_permissions_compare_and_swap(
                workspace_id=workspace.id,
                membership_id=new_member.id,
                actor_user_id=actor.id,
                expected_version=0,
                page_permissions=explicit,
            )
            updated_version = db.execute(statement).scalar_one_or_none()
            assert updated_version == 1
            db.flush()
            db.refresh(new_member)
            assert page_permission_allows(
                new_member,
                workspace_id=workspace.id,
                user_id=member_user.id,
                page_key="inventory",
                legacy_access="admin",
            )
            assert not page_permission_allows(
                new_member,
                workspace_id=workspace.id,
                user_id=member_user.id,
                page_key="services",
                legacy_access="admin",
            )
            assert db.execute(
                build_page_permissions_compare_and_swap(
                    workspace_id=workspace.id,
                    membership_id=new_member.id,
                    actor_user_id=member_user.id,
                    expected_version=1,
                    page_permissions=explicit,
                )
            ).scalar_one_or_none() is None
            assert not page_permission_allows(
                cross_member,
                workspace_id=workspace.id,
                user_id=cross_user.id,
                page_key="inventory",
                legacy_access="reader",
            )
    finally:
        if workspace_id is not None and other_workspace_id is not None:
            with Session(engine) as db, db.begin():
                db.query(WorkspaceMember).filter(
                    WorkspaceMember.workspace_id.in_([workspace_id, other_workspace_id])
                ).delete(synchronize_session=False)
                db.query(Workspace).filter(
                    Workspace.id.in_([workspace_id, other_workspace_id])
                ).delete(synchronize_session=False)
                db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        engine.dispose()
