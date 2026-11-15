from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

EXPENSE_CATEGORIES = (
    "rent",
    "payroll",
    "supplies",
    "marketing",
    "utilities",
    "maintenance",
    "software",
    "taxes",
    "other",
)
EXPENSE_TYPES = ("fixed", "variable")


class Expense(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Workspace-scoped operating expense used by core profitability reporting."""

    __tablename__ = "expenses"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_expenses_workspace_id_id"),
        CheckConstraint("amount_minor > 0", name="expense_amount_positive"),
        CheckConstraint(
            "category IN ('rent', 'payroll', 'supplies', 'marketing', 'utilities', 'maintenance', 'software', 'taxes', 'other')",
            name="expense_category_valid",
        ),
        CheckConstraint(
            "expense_type IN ('fixed', 'variable')",
            name="expense_type_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_expenses_workspace",
        ),
        Index(
            "ix_expenses_workspace_incurred_on",
            "workspace_id",
            "incurred_on",
        ),
        Index(
            "ix_expenses_workspace_currency_incurred_on",
            "workspace_id",
            "currency",
            "incurred_on",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(24), nullable=False, default="other")
    expense_type: Mapped[str] = mapped_column(String(16), nullable=False, default="variable")
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    incurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
