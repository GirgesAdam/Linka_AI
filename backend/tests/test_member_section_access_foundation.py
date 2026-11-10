from types import SimpleNamespace
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies.section_access import require_section_access
from app.api.dependencies.security import get_workspace_access
from app.models.workspace_member import WorkspaceMember
from app.schemas.section_access import MemberSectionAccess
from app.services.section_access import (
    LEGACY_ADMINISTRATION_KEYS,
    LEGACY_CLINIC_OPERATIONS_KEYS,
    LEGACY_PAGE_PERMISSION_KEYS,
    empty_member_section_access,
    normalize_member_section_access,
    section_access_allows,
)


def _legacy_matrix(**overrides: str) -> dict[str, str]:
    values = {key: "none" for key in LEGACY_PAGE_PERMISSION_KEYS}
    values.update(overrides)
    return values


def _membership(
    *,
    role: str = "member",
    section_access: dict | None = None,
    active: bool = True,
    workspace_id=None,
    user_id=None,
) -> WorkspaceMember:
    return WorkspaceMember(
        workspace_id=workspace_id or uuid4(),
        user_id=user_id or uuid4(),
        role=role,
        is_active=active,
        section_access=section_access,
    )


def _guard_client(access, section: str) -> TestClient:
    app = FastAPI()
    guard = require_section_access(section)

    @app.get("/guarded")
    def guarded(member: Annotated[object, Depends(guard)]):
        return {"user_id": str(member.user.id)}

    app.dependency_overrides[get_workspace_access] = lambda: access
    return TestClient(app)


def test_new_member_defaults_close_only_the_two_manager_controlled_sections() -> None:
    section_access = empty_member_section_access()
    assert section_access == {"clinic_operations": False, "administration": False}

    member = _membership(section_access=section_access)
    assert not section_access_allows(
        member,
        workspace_id=member.workspace_id,
        user_id=member.user_id,
        section="clinic_operations",
    )
    assert not section_access_allows(
        member,
        workspace_id=member.workspace_id,
        user_id=member.user_id,
        section="administration",
    )


def test_admin_always_has_both_sections_without_stored_flags() -> None:
    admin = _membership(role="admin", section_access=None)
    for section in ("clinic_operations", "administration"):
        assert section_access_allows(
            admin,
            workspace_id=admin.workspace_id,
            user_id=admin.user_id,
            section=section,
        )


def test_legacy_null_and_malformed_values_fail_closed_without_rewriting() -> None:
    for raw in (None, {}, {"clinic_operations": True}, {"clinic_operations": 1, "administration": True}):
        normalized, is_legacy = normalize_member_section_access(raw)
        assert normalized == {"clinic_operations": False, "administration": False}
        assert is_legacy is True


def test_legacy_page_matrix_maps_conservatively_to_whole_sections() -> None:
    legacy = _legacy_matrix()
    for key in LEGACY_CLINIC_OPERATIONS_KEYS:
        legacy[key] = "manage"
    normalized, is_legacy = normalize_member_section_access(legacy)
    assert normalized == {"clinic_operations": True, "administration": False}
    assert is_legacy is True

    for key in LEGACY_ADMINISTRATION_KEYS:
        legacy[key] = "manage"
    normalized, is_legacy = normalize_member_section_access(legacy)
    assert normalized == {"clinic_operations": True, "administration": True}
    assert is_legacy is True

    legacy["inventory"] = "none"
    normalized, _ = normalize_member_section_access(legacy)
    assert normalized["clinic_operations"] is False


def test_legacy_team_grant_and_unknown_keys_never_grant_access() -> None:
    team_grant = _legacy_matrix(team="manage")
    normalized, is_legacy = normalize_member_section_access(team_grant)
    assert normalized == {"clinic_operations": False, "administration": False}
    assert is_legacy is True

    unknown = _legacy_matrix()
    unknown["unexpected"] = "manage"
    normalized, _ = normalize_member_section_access(unknown)
    assert normalized == {"clinic_operations": False, "administration": False}


def test_new_section_schema_requires_exact_boolean_flags() -> None:
    assert MemberSectionAccess(clinic_operations=True, administration=False).model_dump() == {
        "clinic_operations": True,
        "administration": False,
    }
    with pytest.raises(ValidationError):
        MemberSectionAccess.model_validate(
            {"clinic_operations": 1, "administration": False}
        )
    with pytest.raises(ValidationError):
        MemberSectionAccess.model_validate(
            {"clinic_operations": True, "administration": False, "team": True}
        )


def test_section_access_enforces_membership_workspace_user_and_active_state() -> None:
    member = _membership(section_access={"clinic_operations": True, "administration": False})
    assert section_access_allows(
        member,
        workspace_id=member.workspace_id,
        user_id=member.user_id,
        section="clinic_operations",
    )
    assert not section_access_allows(
        member,
        workspace_id=uuid4(),
        user_id=member.user_id,
        section="clinic_operations",
    )
    assert not section_access_allows(
        member,
        workspace_id=member.workspace_id,
        user_id=uuid4(),
        section="clinic_operations",
    )
    member.is_active = False
    assert not section_access_allows(
        member,
        workspace_id=member.workspace_id,
        user_id=member.user_id,
        section="clinic_operations",
    )


def test_section_dependency_denies_closed_section_and_allows_granted_section_and_admin() -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    member = _membership(
        section_access={"clinic_operations": True, "administration": False},
        workspace_id=workspace_id,
        user_id=user_id,
    )
    access = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        workspace=SimpleNamespace(id=workspace_id),
        membership=member,
    )

    with _guard_client(access, "clinic_operations") as client:
        assert client.get("/guarded").status_code == 200
    with _guard_client(access, "administration") as client:
        assert client.get("/guarded").status_code == 403

    admin = _membership(role="admin", section_access=None, workspace_id=workspace_id, user_id=user_id)
    admin_access = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        workspace=SimpleNamespace(id=workspace_id),
        membership=admin,
    )
    for section in ("clinic_operations", "administration"):
        with _guard_client(admin_access, section) as client:
            assert client.get("/guarded").status_code == 200
