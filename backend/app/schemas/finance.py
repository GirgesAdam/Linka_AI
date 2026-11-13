from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ExpenseCategory = Literal[
    "rent",
    "payroll",
    "supplies",
    "marketing",
    "utilities",
    "maintenance",
    "software",
    "taxes",
    "other",
]
ExpenseType = Literal["fixed", "variable"]
FinanceTrendMode = Literal["month", "year"]


class ExpenseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category: ExpenseCategory = "other"
    expense_type: ExpenseType = "variable"
    amount_minor: int = Field(gt=0)
    currency: str = Field(default="EGP", min_length=3, max_length=3)
    incurred_on: date
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class ExpenseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    category: ExpenseCategory | None = None
    expense_type: ExpenseType | None = None
    amount_minor: int | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    incurred_on: date | None = None
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def reject_null_for_required_columns(self) -> ExpenseUpdate:
        for field_name in ("title", "category", "expense_type", "amount_minor", "currency", "incurred_on"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null.")
        return self


class ExpenseRead(BaseModel):
    is_vault_linked: bool = False
    id: UUID
    workspace_id: UUID
    created_by_user_id: UUID | None
    title: str
    category: ExpenseCategory
    expense_type: ExpenseType
    amount_minor: int
    currency: str
    incurred_on: date
    note: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProfitabilityCurrencyRead(BaseModel):
    currency: str
    gross_payments_minor: int
    refunds_minor: int
    net_revenue_minor: int
    expenses_minor: int
    profit_minor: int


class ProfitabilityRead(BaseModel):
    start_date: date
    end_date: date
    currencies: list[ProfitabilityCurrencyRead]


class FinanceTrendPoint(BaseModel):
    label: str
    start_date: date
    end_date: date
    gross_payments_minor: int
    refunds_minor: int
    net_revenue_minor: int
    expenses_minor: int
    profit_minor: int


class FinanceTrendRead(BaseModel):
    start_date: date
    end_date: date
    currency: str
    mode: FinanceTrendMode
    points: list[FinanceTrendPoint]


class PaymentMethodBreakdownRow(BaseModel):
    payment_method: str
    currency: str
    amount_minor: int
    transaction_count: int


class PaymentMethodBreakdownRead(BaseModel):
    start_date: date
    end_date: date
    rows: list[PaymentMethodBreakdownRow]


class OutstandingBalanceRow(BaseModel):
    patient_id: UUID
    patient_name: str
    phone: str | None
    currency: str
    balance_minor: int
    appointment_count: int


class OutstandingBalancesRead(BaseModel):
    rows: list[OutstandingBalanceRow]
    total_patients: int
    totals_by_currency: dict[str, int]
