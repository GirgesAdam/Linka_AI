"""Admin-only pending vault reconciliation API contracts."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class PendingAllocationRead(BaseModel):
    allocation_id: UUID
    appointment_id: UUID
    amount_minor: int
    branch_id: UUID | None
    branch_name: str | None
    patient_matches_source: bool


class VaultPendingPaymentRead(BaseModel):
    pending_id: UUID
    transaction_id: UUID
    reason: str
    amount_minor: int
    currency: str
    payment_method: str
    transaction_type: str
    source: str
    external_reference: str | None
    origin_appointment_id: UUID | None
    appointment_id: UUID | None
    created_at: datetime
    allocations: list[PendingAllocationRead]
    resolution_eligible: bool
    verified_branch_id: UUID | None
    electronic_action: str | None = None
    review_outcome: str | None = None
    review_note: str | None = None
    reviewed_at: datetime | None = None


class VaultResolveRequest(BaseModel):
    branch_id: UUID
    explanation: str = Field(min_length=8, max_length=500)


class VaultResolveRead(BaseModel):
    entry_id: UUID
    payment_transaction_id: UUID
    branch_id: UUID
    amount_minor: int
    currency: str = "EGP"

class VaultElectronicReviewRequest(BaseModel):
    outcome: str = Field(pattern="^(included_in_opening|requires_accounting_decision)$")
    explanation: str = Field(min_length=8, max_length=500)


class VaultElectronicRetryRequest(BaseModel):
    explanation: str = Field(min_length=8, max_length=500)


class VaultElectronicReviewRead(BaseModel):
    pending_id: UUID
    outcome: str
    ledger_posted: bool = False

class VaultElectronicPostRead(BaseModel):
    entry_id: UUID
    payment_transaction_id: UUID
    amount_minor: int
    currency: str = "EGP"
