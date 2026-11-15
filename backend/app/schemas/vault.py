"""Vault API contracts: amounts are signed integer Egyptian piasters."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

VaultKind = Literal["reception", "main_cash", "main_visa", "main_instapay"]


class VaultActivationRequest(BaseModel):
    kind: VaultKind
    branch_id: UUID | None = None
    opening_balance_minor: int = Field(ge=0, le=1_000_000_000_000)
    currency: Literal["EGP"] = "EGP"


class VaultActivationRead(BaseModel):
    account_id: UUID
    kind: VaultKind
    branch_id: UUID | None
    currency: str


class VaultBranchGrantRequest(BaseModel):
    branch_id: UUID
    user_id: UUID
    enabled: bool


class VaultBranchGrantRead(BaseModel):
    branch_id: UUID
    user_id: UUID


class VaultExpenseRequest(BaseModel):
    amount_minor: int = Field(gt=0, le=1_000_000_000_000)
    description: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=128)


class VaultExpenseRead(BaseModel):
    expense_id: UUID
    amount_minor: int
    currency: str


class VaultTransferRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)


class VaultTransferRead(BaseModel):
    transfer_id: UUID
    amount_minor: int
    currency: str
    branch_id: UUID


class ReceptionVaultRead(BaseModel):
    branch_id: UUID
    branch_name: str
    activated: bool
    currency: str
    balance_minor: int
    opening_carried_minor: int
    cash_today_minor: int
    refunds_today_minor: int
    expenses_today_minor: int
    transfers_today_minor: int


class TreasuryMovementRead(BaseModel):
    id: UUID
    kind: str
    amount_minor: int
    account_kind: str
    branch_id: UUID | None
    created_at: datetime


class MainTreasuryRead(BaseModel):
    currency: str
    activated: bool
    active_channels: list[str]
    cash_minor: int
    visa_minor: int
    instapay_minor: int
    total_recorded_minor: int
    transfers_today_minor: int
    unattributed_payment_count: int
    recent_movements: list[TreasuryMovementRead]
