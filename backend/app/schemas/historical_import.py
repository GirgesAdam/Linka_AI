from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

HistoricalImportMode = Literal["append", "replace_previous_imports"]
HistoricalEntityType = Literal["patient", "appointment", "payment", "payment_allocation", "package"]


class HistoricalImportDocument(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    format: Literal["csv", "xlsx"]
    content_base64: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return value.strip()


class HistoricalImportPreviewRequest(BaseModel):
    documents: list[HistoricalImportDocument] = Field(min_length=1, max_length=10)
    mode: HistoricalImportMode = "append"


class HistoricalImportIssueGroup(BaseModel):
    code: str
    message: str
    entity_type: HistoricalEntityType
    occurrence_count: int
    example_rows: list[int] = Field(default_factory=list)


class HistoricalImportBatchRead(BaseModel):
    batch_id: UUID
    mode: HistoricalImportMode
    status: Literal["preview_ready", "importing", "imported", "failed"]
    schema_version: str
    source_name: str
    summary: dict
    error_message: str | None = None


class HistoricalImportPreviewResponse(BaseModel):
    batch: HistoricalImportBatchRead
    ready_counts: dict[str, int]
    rejected_counts: dict[str, int]
    issue_groups: list[HistoricalImportIssueGroup]
    can_import: bool
    projected_counts: dict[str, int] = Field(default_factory=dict)
    total_amount_imported_minor: int = 0


class HistoricalImportApplyResponse(BaseModel):
    batch: HistoricalImportBatchRead
    import_started: bool = True


class HistoricalImportListResponse(BaseModel):
    batches: list[HistoricalImportBatchRead]
