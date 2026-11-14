"""Reception cash and recorded main-treasury operations.

Every balance is derived from immutable signed ledger entries. Account row locks
serialize debits and full-balance transfers with payment posting triggers.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.expense import Expense
from app.models.vault import (
    VaultAccount,
    VaultBranchGrant,
    VaultLedgerEntry,
    VaultPendingPayment,
    VaultTransfer,
)
from app.models.workspace_member import WORKSPACE_ROLE_ADMIN, WorkspaceMember
from app.schemas.finance import ExpenseCreate
from app.services.activity import record_activity_event
from app.services.finance import create_expense


class VaultError(ValueError):
    pass


def _local_bounds(timezone_name: str, workspace_fallback: str = "UTC") -> tuple[date, datetime, datetime]:
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        try:
            tz = ZoneInfo(workspace_fallback)
        except (ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo("UTC")
    local_day = datetime.now(UTC).astimezone(tz).date()
    beginning = datetime.combine(local_day, time.min, tzinfo=tz).astimezone(UTC)
    following = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    return local_day, beginning, following


def _branch(db: Session, workspace_id: UUID, branch_id: UUID) -> Branch:
    branch = db.scalar(select(Branch).where(
        Branch.workspace_id == workspace_id, Branch.id == branch_id, Branch.is_active.is_(True),
    ))
    if branch is None:
        raise VaultError("Branch not found in this workspace.")
    return branch


def allowed_branch(
    db: Session, *, workspace_id: UUID, branch_id: UUID,
    actor_user_id: UUID, membership: WorkspaceMember,
) -> Branch:
    branch = _branch(db, workspace_id, branch_id)
    if membership.workspace_id != workspace_id or membership.user_id != actor_user_id or not membership.is_active:
        raise VaultError("Access denied.")
    if membership.role == WORKSPACE_ROLE_ADMIN:
        return branch
    # An ordinary member must be explicitly granted access. Staff profile and
    # primary branch are not proof of financial custody.
    grant = db.scalar(select(VaultBranchGrant.id).where(
        VaultBranchGrant.workspace_id == workspace_id,
        VaultBranchGrant.branch_id == branch_id,
        VaultBranchGrant.user_id == actor_user_id,
    ))
    if grant is None:
        raise VaultError("Reception vault access not granted for this branch.")
    return branch


def _account(
    db: Session, workspace_id: UUID, kind: str, branch_id: UUID | None = None,
) -> VaultAccount:
    stmt = select(VaultAccount).where(
        VaultAccount.workspace_id == workspace_id,
        VaultAccount.kind == kind,
        VaultAccount.currency == "EGP",
    )
    if branch_id is None:
        stmt = stmt.where(VaultAccount.branch_id.is_(None))
    else:
        stmt = stmt.where(VaultAccount.branch_id == branch_id)
    account = db.scalar(stmt)
    if account is None:
        raise VaultError("Vault has not been activated by the administrator.")
    return account


def _balance(db: Session, *, workspace_id: UUID, account_id: UUID) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(VaultLedgerEntry.amount_minor), 0)).where(
        VaultLedgerEntry.workspace_id == workspace_id,
        VaultLedgerEntry.account_id == account_id,
    )) or 0)


def _carried_reception_balance(balance_minor: int, daily: dict[str, int]) -> int:
    # Opening money is an attested existing float, not new cash received today.
    # The first activation day must still reconcile with the dashboard card.
    operational_delta = sum(amount for kind, amount in daily.items() if kind != "opening")
    return balance_minor - operational_delta


def _lock_accounts(db: Session, workspace_id: UUID, account_ids: list[UUID]) -> None:
    # Same order in every operation, including transfers across two accounts.
    locked = list(db.scalars(
        select(VaultAccount.id).where(
            VaultAccount.workspace_id == workspace_id,
            VaultAccount.id.in_(account_ids),
        ).order_by(VaultAccount.id).with_for_update()
    ))
    if len(locked) != len(set(account_ids)):
        raise VaultError("Vault account identity mismatch.")


def activate_account(
    db: Session, *, workspace_id: UUID, actor_user_id: UUID,
    kind: str, branch_id: UUID | None, opening_balance_minor: int, currency: str,
) -> VaultAccount:
    if kind not in {"reception", "main_cash", "main_visa", "main_instapay"} or currency != "EGP":
        raise VaultError("Invalid vault kind or currency.")
    if opening_balance_minor < 0 or opening_balance_minor > 10_000_000_000_00:
        raise VaultError("Invalid opening amount.")
    if (kind == "reception") != (branch_id is not None):
        raise VaultError("Reception accounts need a branch; main channels must not have one.")
    if branch_id is not None:
        _branch(db, workspace_id, branch_id)

    # Serializes concurrent activation and payment-trigger decisions.
    # The workspace lock is also acquired before any account insert.
    from app.models.workspace import Workspace
    workspace = db.scalar(
        select(Workspace).where(Workspace.id == workspace_id).with_for_update()
    )
    if workspace is None:
        raise VaultError("Workspace not found.")
    if workspace.is_demo:
        raise VaultError("Vault activation is unavailable in demo mode.")
    existing = db.scalar(select(VaultAccount).where(
        VaultAccount.workspace_id == workspace_id,
        VaultAccount.kind == kind,
        VaultAccount.branch_id == branch_id if branch_id is not None
        else VaultAccount.branch_id.is_(None),
    ))
    if existing is not None:
        raise VaultError("This vault account has already been activated.")
    account = VaultAccount(
        workspace_id=workspace_id, kind=kind, branch_id=branch_id,
        currency="EGP", opened_by_user_id=actor_user_id,
    )
    db.add(account)
    db.flush()
    if opening_balance_minor:
        db.add(VaultLedgerEntry(
            workspace_id=workspace_id, account_id=account.id, kind="opening",
            amount_minor=opening_balance_minor, actor_user_id=actor_user_id,
            note="Owner-attested opening physical/recorded balance",
        ))
    record_activity_event(
        db, workspace_id=workspace_id, actor_type="staff",
        actor_user_id=actor_user_id, action="vault.activated",
        entity_type="vault_account", entity_id=account.id,
        summary="Vault activation recorded.",
        metadata={"kind":kind,"branch_id":str(branch_id) if branch_id else None,
                  "opening_minor":opening_balance_minor,"currency":"EGP"},
    )
    db.flush()
    return account


def set_branch_grant(
    db: Session, *, workspace_id: UUID, branch_id: UUID, user_id: UUID,
    actor_user_id: UUID, enabled: bool,
) -> None:
    _branch(db, workspace_id, branch_id)
    membership = db.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == user_id,
        WorkspaceMember.is_active.is_(True),
    ))
    if membership is None:
        raise VaultError("User is not an active member of this workspace.")
    existing = db.scalar(select(VaultBranchGrant).where(
        VaultBranchGrant.workspace_id == workspace_id,
        VaultBranchGrant.branch_id == branch_id, VaultBranchGrant.user_id == user_id,
    ))
    if enabled and existing is None:
        db.add(VaultBranchGrant(
            workspace_id=workspace_id, branch_id=branch_id, user_id=user_id,
            granted_by_user_id=actor_user_id,
        ))
    elif not enabled and existing is not None:
        db.delete(existing)
    record_activity_event(
        db, workspace_id=workspace_id, actor_type="staff", actor_user_id=actor_user_id,
        action="vault.branch_access_changed", entity_type="branch", entity_id=branch_id,
        summary="Reception financial access changed.",
        metadata={"target_user_id":str(user_id),"enabled":enabled},
    )


def transfer_full_balance(
    db: Session, *, workspace_id: UUID, branch_id: UUID, actor_user_id: UUID,
    idempotency_key: str,
) -> VaultTransfer:
    if not idempotency_key or len(idempotency_key) > 128:
        raise VaultError("A valid idempotency key is required.")
    previous = db.scalar(select(VaultTransfer).where(
        VaultTransfer.workspace_id == workspace_id,
        VaultTransfer.idempotency_key == idempotency_key,
    ))
    if previous is not None:
        if previous.branch_id != branch_id:
            raise VaultError("Idempotency key already used for another branch.")
        return previous
    reception = _account(db, workspace_id, "reception", branch_id)
    treasury = _account(db, workspace_id, "main_cash")
    _lock_accounts(db, workspace_id, [reception.id, treasury.id])
    previous = db.scalar(select(VaultTransfer).where(
        VaultTransfer.workspace_id == workspace_id,
        VaultTransfer.idempotency_key == idempotency_key,
    ))
    if previous is not None:
        if previous.branch_id != branch_id:
            raise VaultError("Idempotency key already used for another branch.")
        return previous
    amount = _balance(db, workspace_id=workspace_id, account_id=reception.id)
    if amount <= 0:
        raise VaultError("No available cash to transfer.")
    transfer = VaultTransfer(
        workspace_id=workspace_id, branch_id=branch_id,
        currency="EGP", amount_minor=amount, idempotency_key=idempotency_key,
        actor_user_id=actor_user_id,
    )
    db.add(transfer)
    db.flush()
    db.add_all([
        VaultLedgerEntry(
            workspace_id=workspace_id, account_id=reception.id,
            kind="transfer_out", amount_minor=-amount, transfer_id=transfer.id,
            actor_user_id=actor_user_id,
        ),
        VaultLedgerEntry(
            workspace_id=workspace_id, account_id=treasury.id,
            kind="transfer_in", amount_minor=amount, transfer_id=transfer.id,
            actor_user_id=actor_user_id,
        ),
    ])
    record_activity_event(
        db, workspace_id=workspace_id, actor_type="staff",
        actor_user_id=actor_user_id, action="vault.cash_transferred",
        entity_type="vault_transfer", entity_id=transfer.id,
        summary="Reception cash handed over to main treasury.",
        metadata={"branch_id":str(branch_id),"amount_minor":amount,"currency":"EGP"},
    )
    db.flush()
    return transfer


def record_cash_expense(
    db: Session, *, workspace_id: UUID, branch: Branch,
    actor_user_id: UUID, amount_minor: int, description: str,
    idempotency_key: str, workspace_timezone: str,
) -> Expense:
    description = description.strip()
    if not description or len(description) > 200 or amount_minor <= 0:
        raise VaultError("A positive amount and expense description are required.")
    if not idempotency_key or len(idempotency_key) > 128:
        raise VaultError("A valid idempotency key is required.")
    # Expense retry receipt belongs to the vault ledger, not the finance expense.
    previous = db.scalar(select(VaultLedgerEntry).where(
        VaultLedgerEntry.workspace_id == workspace_id,
        VaultLedgerEntry.kind == "expense",
        VaultLedgerEntry.idempotency_key == idempotency_key,
    ))
    if previous is not None:
        expense = db.scalar(select(Expense).where(
            Expense.id == previous.expense_id, Expense.workspace_id == workspace_id,
        ))
        expected_account = _account(db, workspace_id, "reception", branch.id)
        if (previous.account_id != expected_account.id or expense is None or expense.amount_minor != amount_minor or expense.title != description):
            raise VaultError("Idempotency key was reused with different expense or branch details.")
        return expense
    account = _account(db, workspace_id, "reception", branch.id)
    _lock_accounts(db, workspace_id, [account.id])
    previous = db.scalar(select(VaultLedgerEntry).where(
        VaultLedgerEntry.workspace_id == workspace_id,
        VaultLedgerEntry.kind == "expense",
        VaultLedgerEntry.idempotency_key == idempotency_key,
    ))
    if previous is not None:
        expense = db.scalar(select(Expense).where(
            Expense.id == previous.expense_id, Expense.workspace_id == workspace_id,
        ))
        expected_account = _account(db, workspace_id, "reception", branch.id)
        if (previous.account_id != expected_account.id or expense is None or expense.amount_minor != amount_minor or expense.title != description):
            raise VaultError("Idempotency key was reused with different expense or branch details.")
        return expense
    if _balance(db, workspace_id=workspace_id, account_id=account.id) < amount_minor:
        raise VaultError("Insufficient reception cash for this expense.")
    today, _start, _end = _local_bounds(branch.timezone or workspace_timezone, workspace_timezone)
    expense = create_expense(
        db, workspace_id=workspace_id, created_by_user_id=actor_user_id,
        payload=ExpenseCreate(
            title=description, category="other", expense_type="variable",
            amount_minor=amount_minor, currency="EGP", incurred_on=today,
        ),
    )
    db.add(VaultLedgerEntry(
        workspace_id=workspace_id, account_id=account.id, kind="expense",
        amount_minor=-amount_minor, expense_id=expense.id,
        actor_user_id=actor_user_id, idempotency_key=idempotency_key,
        note="Cash paid at reception for an existing Finance expense",
    ))
    db.flush()
    return expense


def reception_summaries(
    db: Session, *, workspace_id: UUID, actor_user_id: UUID,
    membership: WorkspaceMember, workspace_timezone: str,
) -> list[dict]:
    stmt = select(Branch).where(
        Branch.workspace_id == workspace_id, Branch.is_active.is_(True),
    )
    if membership.role != WORKSPACE_ROLE_ADMIN:
        stmt = stmt.join(VaultBranchGrant, (
            (VaultBranchGrant.workspace_id == Branch.workspace_id)
            & (VaultBranchGrant.branch_id == Branch.id)
            & (VaultBranchGrant.user_id == actor_user_id)
        ))
    branches = list(db.scalars(stmt.order_by(Branch.name)))
    output = []
    for branch in branches:
        account = db.scalar(select(VaultAccount).where(
            VaultAccount.workspace_id == workspace_id,
            VaultAccount.kind == "reception", VaultAccount.branch_id == branch.id,
        ))
        if account is None:
            output.append({"branch_id":branch.id,"branch_name":branch.name,
                           "activated":False,"currency":"EGP","balance_minor":0,
                           "opening_carried_minor":0,"cash_today_minor":0,
                           "expenses_today_minor":0,"transfers_today_minor":0,
                           "refunds_today_minor":0})
            continue
        _today, start, end = _local_bounds(branch.timezone or workspace_timezone, workspace_timezone)
        entries = db.execute(select(VaultLedgerEntry.kind, func.sum(VaultLedgerEntry.amount_minor)).where(
            VaultLedgerEntry.workspace_id == workspace_id,
            VaultLedgerEntry.account_id == account.id,
            VaultLedgerEntry.created_at >= start,
            VaultLedgerEntry.created_at < end,
        ).group_by(VaultLedgerEntry.kind)).all()
        daily = {str(k):int(n) for k,n in entries}
        balance = _balance(db, workspace_id=workspace_id, account_id=account.id)
        output.append({
            "branch_id":branch.id, "branch_name":branch.name,
            "activated":True,"currency":"EGP","balance_minor":balance,
            "opening_carried_minor":_carried_reception_balance(balance, daily),
            "cash_today_minor":daily.get("payment",0),
            "refunds_today_minor":-daily.get("refund",0),
            "expenses_today_minor":-daily.get("expense",0),
            "transfers_today_minor":-daily.get("transfer_out",0),
        })
    return output


def main_treasury_summary(
    db: Session, *, workspace_id: UUID, workspace_timezone: str,
) -> dict:
    _today, start, end = _local_bounds(workspace_timezone)
    accounts = list(db.scalars(select(VaultAccount).where(
        VaultAccount.workspace_id == workspace_id,
        VaultAccount.kind.in_(("main_cash","main_visa","main_instapay")),
        VaultAccount.currency == "EGP",
    )))
    channels = {a.kind: _balance(db, workspace_id=workspace_id, account_id=a.id)
                for a in accounts}
    transfers_today = int(db.scalar(
        select(func.coalesce(func.sum(VaultLedgerEntry.amount_minor),0)).where(
            VaultLedgerEntry.workspace_id == workspace_id,
            VaultLedgerEntry.account_id.in_([a.id for a in accounts if a.kind == "main_cash"]),
            VaultLedgerEntry.kind == "transfer_in",
            VaultLedgerEntry.created_at >= start,
            VaultLedgerEntry.created_at < end,
        )
    ) or 0)
    recent = db.execute(
        select(VaultLedgerEntry, VaultTransfer.branch_id).outerjoin(
            VaultTransfer,
            (VaultTransfer.workspace_id == VaultLedgerEntry.workspace_id)
            & (VaultTransfer.id == VaultLedgerEntry.transfer_id),
        ).where(
            VaultLedgerEntry.workspace_id == workspace_id,
            VaultLedgerEntry.account_id.in_([a.id for a in accounts]),
        ).order_by(VaultLedgerEntry.created_at.desc()).limit(30)
    ).all() if accounts else []
    pending = db.scalar(select(func.count(VaultPendingPayment.id)).where(
        VaultPendingPayment.workspace_id == workspace_id,
    )) or 0
    return {
        "currency":"EGP","activated":len(accounts)==3,
        "active_channels":[a.kind for a in accounts],
        "cash_minor":channels.get("main_cash",0),
        "visa_minor":channels.get("main_visa",0),
        "instapay_minor":channels.get("main_instapay",0),
        "total_recorded_minor":sum(channels.values()),
        "transfers_today_minor":transfers_today,
        "unattributed_payment_count":int(pending),
        "recent_movements":[
            {
                "id":entry.id,"kind":entry.kind,"amount_minor":entry.amount_minor,
                "account_kind":next((a.kind for a in accounts if a.id==entry.account_id),"unknown"),
                "branch_id":branch_id,"created_at":entry.created_at,
            }
            for entry,branch_id in recent
        ],
    }



def list_branch_grants(db: Session, *, workspace_id: UUID) -> list[dict]:
    rows = db.execute(select(VaultBranchGrant.branch_id, VaultBranchGrant.user_id).where(
        VaultBranchGrant.workspace_id == workspace_id,
    )).all()
    return [{"branch_id":branch_id, "user_id":user_id} for branch_id,user_id in rows]


def treasury_movements(
    db: Session, *, workspace_id: UUID, timezone_name: str,
    start_date: date | None, end_date: date | None,
    branch_id: UUID | None, kind: str | None, method: str | None,
    limit: int = 200,
) -> list[dict]:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise VaultError("Invalid date filter.")
    if method not in {None,"cash","visa","instapay"}:
        raise VaultError("Unsupported payment method.")
    if kind is not None and kind not in {
        "opening","payment","refund","expense","transfer_out","transfer_in","correction",
    }:
        raise VaultError("Unsupported movement type.")
    if branch_id is not None:
        _branch(db, workspace_id, branch_id)
    accounts = select(VaultAccount.id).where(
        VaultAccount.workspace_id == workspace_id,
        VaultAccount.kind.in_(("main_cash","main_visa","main_instapay")),
    )
    if method is not None:
        accounts = accounts.where(VaultAccount.kind == "main_" + method)
    stmt = (
        select(VaultLedgerEntry, VaultTransfer.branch_id, VaultAccount.kind)
        .join(VaultAccount, (
            (VaultAccount.workspace_id == VaultLedgerEntry.workspace_id)
            & (VaultAccount.id == VaultLedgerEntry.account_id)
        ))
        .outerjoin(VaultTransfer, (
            (VaultTransfer.workspace_id == VaultLedgerEntry.workspace_id)
            & (VaultTransfer.id == VaultLedgerEntry.transfer_id)
        ))
        .where(
            VaultLedgerEntry.workspace_id == workspace_id,
            VaultLedgerEntry.account_id.in_(accounts),
        )
    )
    if kind is not None:
        stmt = stmt.where(VaultLedgerEntry.kind == kind)
    if branch_id is not None:
        stmt = stmt.where(VaultTransfer.branch_id == branch_id)
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        timezone = ZoneInfo("UTC")
    if start_date is not None:
        stmt = stmt.where(
            VaultLedgerEntry.created_at >= datetime.combine(start_date, time.min, tzinfo=timezone).astimezone(UTC)
        )
    if end_date is not None:
        stmt = stmt.where(
            VaultLedgerEntry.created_at <
            datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone).astimezone(UTC)
        )
    rows = db.execute(
        stmt.order_by(VaultLedgerEntry.created_at.desc(), VaultLedgerEntry.id.desc())
        .limit(min(max(limit,1),500))
    ).all()
    return [
        {"id":entry.id,"kind":entry.kind,"amount_minor":entry.amount_minor,
         "account_kind":account_kind,"branch_id":source_branch,"created_at":entry.created_at}
        for entry,source_branch,account_kind in rows
    ]
