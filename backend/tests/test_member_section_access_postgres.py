import os
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.section_access import (
    build_section_access_compare_and_swap,
    empty_member_section_access,
    normalize_member_section_access,
    section_access_allows,
)


def test_postgres_two_section_defaults_cas_legacy_and_workspace_isolation() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    assert url.host in {"localhost", "127.0.0.1", "::1"}
    assert url.database == "ci_db"
    engine = create_engine(url)
    workspace_ids = []
    user_ids = []
    try:
        with Session(engine) as db, db.begin():
            workspace = Workspace(name="Section Access PG", slug=f"section-pg-{uuid4().hex}")
            other_workspace = Workspace(name="Section Access Other", slug=f"section-other-{uuid4().hex}")
            actor = User(email=f"section-actor-{uuid4().hex}@example.test", auth_user_id=uuid4())
            member_user = User(email=f"section-member-{uuid4().hex}@example.test", auth_user_id=uuid4())
            legacy_user = User(email=f"section-legacy-{uuid4().hex}@example.test", auth_user_id=uuid4())
            cross_user = User(email=f"section-cross-{uuid4().hex}@example.test", auth_user_id=uuid4())
            db.add_all([workspace, other_workspace, actor, member_user, legacy_user, cross_user])
            db.flush()
            workspace_ids = [workspace.id, other_workspace.id]
            user_ids = [actor.id, member_user.id, legacy_user.id, cross_user.id]

            admin = WorkspaceMember(workspace_id=workspace.id, user_id=actor.id, role="admin")
            new_member = WorkspaceMember(
                workspace_id=workspace.id,
                user_id=member_user.id,
                role="member",
                section_access=empty_member_section_access(),
            )
            legacy_member = WorkspaceMember(
                workspace_id=workspace.id,
                user_id=legacy_user.id,
                role="member",
                section_access=None,
            )
            cross_member = WorkspaceMember(
                workspace_id=other_workspace.id,
                user_id=cross_user.id,
                role="member",
                section_access=empty_member_section_access(),
            )
            db.add_all([admin, new_member, legacy_member, cross_member])
            db.flush()

            assert new_member.section_access == {
                "clinic_operations": False,
                "administration": False,
            }
            assert not section_access_allows(
                new_member,
                workspace_id=workspace.id,
                user_id=member_user.id,
                section="clinic_operations",
            )
            assert not section_access_allows(
                new_member,
                workspace_id=workspace.id,
                user_id=member_user.id,
                section="administration",
            )
            assert section_access_allows(
                admin,
                workspace_id=workspace.id,
                user_id=actor.id,
                section="clinic_operations",
            )
            assert section_access_allows(
                admin,
                workspace_id=workspace.id,
                user_id=actor.id,
                section="administration",
            )
            assert normalize_member_section_access(legacy_member.section_access) == (
                {"clinic_operations": False, "administration": False},
                True,
            )

            statement = build_section_access_compare_and_swap(
                workspace_id=workspace.id,
                membership_id=new_member.id,
                actor_user_id=actor.id,
                expected_version=0,
                section_access={"clinic_operations": True, "administration": False},
            )
            assert db.execute(statement).scalar_one_or_none() == 1
            db.flush()
            db.refresh(new_member)
            assert section_access_allows(
                new_member,
                workspace_id=workspace.id,
                user_id=member_user.id,
                section="clinic_operations",
            )
            assert not section_access_allows(
                new_member,
                workspace_id=workspace.id,
                user_id=member_user.id,
                section="administration",
            )
            assert db.execute(
                build_section_access_compare_and_swap(
                    workspace_id=workspace.id,
                    membership_id=new_member.id,
                    actor_user_id=actor.id,
                    expected_version=0,
                    section_access={"clinic_operations": False, "administration": True},
                )
            ).scalar_one_or_none() is None
            assert db.execute(
                build_section_access_compare_and_swap(
                    workspace_id=workspace.id,
                    membership_id=new_member.id,
                    actor_user_id=member_user.id,
                    expected_version=1,
                    section_access={"clinic_operations": False, "administration": True},
                )
            ).scalar_one_or_none() is None
            assert not section_access_allows(
                cross_member,
                workspace_id=workspace.id,
                user_id=cross_user.id,
                section="clinic_operations",
            )
    finally:
        if workspace_ids:
            with Session(engine) as db, db.begin():
                db.query(WorkspaceMember).filter(
                    WorkspaceMember.workspace_id.in_(workspace_ids)
                ).delete(synchronize_session=False)
                db.query(Workspace).filter(Workspace.id.in_(workspace_ids)).delete(
                    synchronize_session=False
                )
                db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        engine.dispose()
