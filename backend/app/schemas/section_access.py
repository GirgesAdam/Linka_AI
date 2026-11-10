from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class MemberSectionAccess(BaseModel):
    clinic_operations: StrictBool
    administration: StrictBool

    model_config = ConfigDict(extra="forbid")


class MemberSectionAccessUpdate(BaseModel):
    expected_version: Annotated[int, Field(ge=0, strict=True)]
    section_access: MemberSectionAccess

    model_config = ConfigDict(extra="forbid")


class MemberSectionAccessRead(BaseModel):
    membership_id: UUID
    section_access: MemberSectionAccess
    permissions_version: int
    is_legacy: bool

    model_config = ConfigDict(extra="forbid")
