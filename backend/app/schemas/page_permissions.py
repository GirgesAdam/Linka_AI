"""Validation contract for a future admin-only member permission editor."""

from typing import Annotated

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
