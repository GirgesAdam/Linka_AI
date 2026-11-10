from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.section_access import MemberSectionAccess

WorkspaceRole = Literal["admin", "member"]
InvitableWorkspaceRole = Literal["admin", "member"]


def normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) > 320 or "@" not in normalized:
        raise ValueError("A valid email address is required.")
    local, _, domain = normalized.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("A valid email address is required.")
    return normalized


class CurrentUserRead(BaseModel):
    id: UUID
    auth_user_id: UUID
    email: str
    full_name: str | None

    model_config = ConfigDict(from_attributes=True)


class WorkspaceAccessRead(BaseModel):
    workspace_id: UUID
    workspace_name: str
    workspace_slug: str
    role: WorkspaceRole
    is_demo: bool = False
    # Normalized DB-backed section access, never trusted from JWT claims.
    section_access: MemberSectionAccess
    permissions_version: int = 0


class MeRead(BaseModel):
    user: CurrentUserRead
    workspaces: list[WorkspaceAccessRead]


class WorkspaceMemberRead(BaseModel):
    membership_id: UUID
    user_id: UUID
    auth_user_id: UUID | None
    email: str
    full_name: str | None
    role: WorkspaceRole
    is_active: bool


class WorkspaceInvitationCreate(BaseModel):
    email: str
    role: InvitableWorkspaceRole = "member"

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class WorkspaceInvitationRead(BaseModel):
    membership: WorkspaceMemberRead
    invitation_sent: bool


class WorkspaceMemberRoleUpdate(BaseModel):
    role: InvitableWorkspaceRole
