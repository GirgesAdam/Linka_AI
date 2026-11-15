"""Immutable, workspace-scoped operational cash and recorded treasury ledger."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, UUIDPrimaryKeyMixin

VAULT_KINDS = ("reception", "main_cash", "main_visa", "main_instapay")


class VaultAccount(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "vault_accounts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_vault_accounts_workspace_id_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "branch_id"],
            ["branches.workspace_id", "branches.id"], ondelete="RESTRICT",
        ),
        CheckConstraint(
            "kind IN ('reception','main_cash','main_visa','main_instapay')",
            name="vault_account_kind",
        ),
        CheckConstraint(
            "(kind = 'reception' AND branch_id IS NOT NULL) OR "
            "(kind <> 'reception' AND branch_id IS NULL)",
            name="vault_account_branch",
        ),
        CheckConstraint("currency = 'EGP'", name="vault_account_currency"),
        Index(
            "uq_vault_accounts_workspace_reception",
            "workspace_id", "branch_id", "currency",
            unique=True, postgresql_where=text("kind = 'reception'"),
        ),
        Index(
            "uq_vault_accounts_workspace_main_kind",
            "workspace_id", "kind", "currency",
            unique=True, postgresql_where=text("kind <> 'reception'"),
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    branch_id: Mapped[UUID | None] = mapped_column(nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EGP")
    opened_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class VaultTransfer(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "vault_transfers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_vault_transfers_workspace_id_id"),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_vault_transfer_retry"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "branch_id"], ["branches.workspace_id", "branches.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount_minor > 0", name="vault_transfer_positive"),
        CheckConstraint("currency = 'EGP'", name="vault_transfer_currency"),
    )
    workspace_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    branch_id: Mapped[UUID] = mapped_column(nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EGP")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class VaultLedgerEntry(UUIDPrimaryKeyMixin, Base):
    """Signed minor units, append-only. Transfers have two equal opposite rows."""
    __tablename__ = "vault_ledger_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "account_id"], ["vault_accounts.workspace_id", "vault_accounts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "transfer_id"], ["vault_transfers.workspace_id", "vault_transfers.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "payment_transaction_id"],
            ["payment_transactions.workspace_id", "payment_transactions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "expense_id"], ["expenses.workspace_id", "expenses.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount_minor <> 0", name="vault_entry_nonzero"),
        CheckConstraint(
            "kind IN ('opening','payment','refund','expense','transfer_out','transfer_in','correction')",
            name="vault_entry_kind",
        ),
        UniqueConstraint("workspace_id", "payment_transaction_id", name="uq_vault_entry_payment"),
        UniqueConstraint("workspace_id", "expense_id", name="uq_vault_entry_expense"),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_vault_entry_idempotency"),
        UniqueConstraint("workspace_id", "transfer_id", "kind", name="uq_vault_entry_transfer_side"),
        Index("ix_vault_entries_workspace_account_created", "workspace_id", "account_id", "created_at"),
        Index("uq_vault_opening_per_account", "workspace_id", "account_id",
              unique=True, postgresql_where=text("kind = 'opening'")),
    )
    workspace_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    account_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payment_transaction_id: Mapped[UUID | None] = mapped_column(nullable=True)
    expense_id: Mapped[UUID | None] = mapped_column(nullable=True)
    transfer_id: Mapped[UUID | None] = mapped_column(nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class VaultPendingPayment(UUIDPrimaryKeyMixin, Base):
    """No guessed branch or unearned cash credit for unresolved payment source."""
    __tablename__ = "vault_pending_payments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "payment_transaction_id", name="uq_vault_pending_payment"),
        CheckConstraint(
            "review_outcome IS NULL OR review_outcome IN ('included_in_opening', 'requires_accounting_decision')",
            name="vault_pending_review_outcome_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "reviewed_account_id"],
            ["vault_accounts.workspace_id", "vault_accounts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "payment_transaction_id"],
            ["payment_transactions.workspace_id", "payment_transactions.id"], ondelete="RESTRICT",
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    payment_transaction_id: Mapped[UUID] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    review_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    reviewed_account_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()"), nullable=False,
    )


class VaultBranchGrant(UUIDPrimaryKeyMixin, Base):
    """Explicit admin-managed authorization; never infer a member's branch."""
    __tablename__ = "vault_branch_grants"
    __table_args__ = (
        UniqueConstraint("workspace_id", "branch_id", "user_id", name="uq_vault_branch_grant"),
        ForeignKeyConstraint(
            ["workspace_id", "branch_id"], ["branches.workspace_id", "branches.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspace_members.workspace_id", "workspace_members.user_id"],
            ondelete="CASCADE",
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    branch_id: Mapped[UUID] = mapped_column(nullable=False)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    granted_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
