"""API schemas for Admin-managed binary Member page permissions."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.services.page_permissions import (
    PagePermissionLevel,
    validate_complete_page_permissions,
)


class MemberPagePermissionsUpdate(BaseModel):
    expected_version: Annotated[int, Field(ge=0, strict=True)]
    page_permissions: dict[str, PagePermissionLevel]

    @field_validator("page_permissions")
    @classmethod
    def require_complete_matrix(
        cls, value: dict[str, PagePermissionLevel]
    ) -> dict[str, PagePermissionLevel]:
        validate_complete_page_permissions(value)
        return value


class MemberPagePermissionsRead(BaseModel):
    membership_id: UUID
    page_permissions: dict[str, PagePermissionLevel] | None
    permissions_version: int
    is_legacy: bool
