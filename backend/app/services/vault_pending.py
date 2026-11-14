"""Evidence-based resolution of unresolved vault payment facts.

Only a fully allocated, single-branch cash *payment* can be manually posted.
The original appointment hints and free-text attestations cannot override allocations.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.models.vault import VaultAccount, VaultLedgerEntry, VaultPendingPayment
from app.services.activity import record_activity_event
from app.services.vault import VaultError


def _allocation_evidence(db: Session, *, workspace_id: UUID, transaction: PaymentTransaction) -> tuple[list[dict], UUID | None]:
    rows = db.execute(
        select(
            PaymentAllocation.id,
            PaymentAllocation.appointment_id,
            PaymentAllocation.amount_minor,
            Appointment.branch_id,
            Branch.name,
            Appointment.patient_id,
        )
        .join(
            Appointment,
            and_(
                Appointment.workspace_id == PaymentAllocation.workspace_id,
                Appointment.id == PaymentAllocation.appointment_id,
            ),
        )
        .outerjoin(
            Branch,
            and_(Branch.workspace_id == Appointment.workspace_id, Branch.id == Appointment.branch_id),
        )
        .where(
            PaymentAllocation.workspace_id == workspace_id,
            PaymentAllocation.transaction_id == transaction.id,
        )
        .order_by(PaymentAllocation.id)
    ).all()
    evidence = [
        {
            "allocation_id":row.id,
            "appointment_id":row.appointment_id,
            "amount_minor":int(row.amount_minor),
            "branch_id":row.branch_id,
            "branch_name":row.name,
            "patient_matches_source":row.patient_id == transaction.patient_id,
        }
        for row in rows
    ]
    branches = {row.branch_id for row in rows}
    total = sum(row.amount_minor for row in rows)
    candidate = (
        next(iter(branches)) if len(branches) == 1 and None not in branches
        and total == transaction.amount_minor
        and all(row.patient_id == transaction.patient_id for row in rows)
        else None
    )
    return evidence, candidate


def _eligible_account(
    db: Session, *, workspace_id: UUID, transaction: PaymentTransaction, branch_id: UUID,
    lock: bool = False,
) -> VaultAccount | None:
    stmt = select(VaultAccount).where(
        VaultAccount.workspace_id == workspace_id,
        VaultAccount.branch_id == branch_id,
        VaultAccount.kind == "reception",
        VaultAccount.currency == transaction.currency,
        VaultAccount.opened_at <= transaction.created_at,
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def _posting_allowed(transaction: PaymentTransaction) -> bool:
    return (
        transaction.transaction_type == "payment"
        and transaction.payment_method == "cash"
        and transaction.currency == "EGP"
        and transaction.source != "legacy_backfill"
        and transaction.amount_minor > 0
    )


def _predates_channel_activation(
    pending: VaultPendingPayment,
    payment: PaymentTransaction,
    account: VaultAccount,
) -> bool:
    # Match the 0099 database trigger's guarded clock-skew policy. A new
    # staff checkout may be created milliseconds before the DB account clock,
    # but an old or externally backdated source remains unposted for review.
    if pending.created_at < account.opened_at:
        return True
    return (
        payment.created_at < account.opened_at
        and (
            payment.source != "staff"
            or pending.created_at - payment.created_at > timedelta(seconds=5)
        )
    )


def list_pending_payments(
    db: Session, *, workspace_id: UUID, limit: int = 100,
) -> list[dict]:
    pending = db.execute(
        select(VaultPendingPayment, PaymentTransaction)
        .join(
            PaymentTransaction,
            and_(
                PaymentTransaction.workspace_id == VaultPendingPayment.workspace_id,
                PaymentTransaction.id == VaultPendingPayment.payment_transaction_id,
            ),
        )
        .where(VaultPendingPayment.workspace_id == workspace_id)
        .order_by(VaultPendingPayment.created_at.desc(), VaultPendingPayment.id.desc())
        .limit(min(max(limit, 1), 200))
    ).all()
    result = []
    for row, payment in pending:
        evidence, candidate = _allocation_evidence(
            db, workspace_id=workspace_id, transaction=payment,
        )
        permitted = (
            _posting_allowed(payment)
            and candidate is not None
            and _eligible_account(
                db, workspace_id=workspace_id, transaction=payment,
                branch_id=candidate,
            ) is not None
        )
        channel = (
            _electronic_channel_account(
                db, workspace_id=workspace_id, payment_method=payment.payment_method,
            )
            if payment.payment_method in ("visa", "instapay")
            and payment.transaction_type == "payment"
            and payment.currency == "EGP"
            else None
        )
        electronic_action = None
        if channel is not None and row.review_outcome is None:
            electronic_action = (
                "opening_review" if _predates_channel_activation(row, payment, channel)
                else "eligible_retry"
            )
        result.append({
            "pending_id":row.id,
            "transaction_id":payment.id,
            "reason":row.reason,
            "amount_minor":payment.amount_minor,
            "currency":payment.currency,
            "payment_method":payment.payment_method,
            "transaction_type":payment.transaction_type,
            "source":payment.source,
            "external_reference":payment.external_reference,
            "origin_appointment_id":payment.origin_appointment_id,
            "appointment_id":payment.appointment_id,
            "created_at":payment.created_at,
            "allocations":evidence,
            "resolution_eligible":bool(permitted),
            "verified_branch_id":candidate if permitted else None,
            "electronic_action":electronic_action,
            "review_outcome":row.review_outcome,
            "review_note":row.review_note,
            "reviewed_at":row.reviewed_at,
        })
    return result


def resolve_pending_payment(
    db: Session, *, workspace_id: UUID, pending_id: UUID,
    branch_id: UUID, actor_user_id: UUID, explanation: str,
) -> VaultLedgerEntry:
    explanation = explanation.strip()
    if len(explanation) < 8 or len(explanation) > 500:
        raise VaultError("A review explanation of 8–500 characters is required.")
    # Lookup first, then serialize on the *account* before taking pending lock;
    # payment allocation trigger takes the same account row lock.
    pending = db.scalar(select(VaultPendingPayment).where(
        VaultPendingPayment.workspace_id == workspace_id,
        VaultPendingPayment.id == pending_id,
    ))
    if pending is None:
        raise VaultError("Pending payment is no longer awaiting resolution.")
    transaction = db.scalar(select(PaymentTransaction).where(
        PaymentTransaction.workspace_id == workspace_id,
        PaymentTransaction.id == pending.payment_transaction_id,
    ))
    if transaction is None or not _posting_allowed(transaction):
        raise VaultError("This payment is not eligible for branch-based resolution.")
    allocations, evidence_branch = _allocation_evidence(
        db, workspace_id=workspace_id, transaction=transaction,
    )
    if evidence_branch is None:
        raise VaultError("Payment allocations do not prove one branch and full amount.")
    if evidence_branch != branch_id:
        raise VaultError("Selected branch disagrees with the payment allocation evidence.")

    account = _eligible_account(
        db, workspace_id=workspace_id, transaction=transaction, branch_id=branch_id, lock=True,
    )
    if account is None:
        raise VaultError("Branch cash vault was not open when this payment was recorded.")
    # Acquire after account lock. A concurrent auto-post deletes pending under
    # that same account lock; it cannot interleave with this posting.
    pending = db.scalar(select(VaultPendingPayment).where(
        VaultPendingPayment.workspace_id == workspace_id,
        VaultPendingPayment.id == pending_id,
    ).with_for_update())
    if pending is None:
        raise VaultError("Payment has already been posted or is no longer pending.")
    # Fresh evidence after lock, including any concurrently committed allocations.
    evidence_now, branch_now = _allocation_evidence(
        db, workspace_id=workspace_id, transaction=transaction,
    )
    if branch_now != branch_id or evidence_now != allocations:
        raise VaultError("Allocation evidence changed; refresh before resolving.")
    if db.scalar(select(VaultLedgerEntry.id).where(
        VaultLedgerEntry.workspace_id == workspace_id,
        VaultLedgerEntry.payment_transaction_id == transaction.id,
    )) is not None:
        raise VaultError("Payment has already been posted.")
    entry = VaultLedgerEntry(
        workspace_id=workspace_id, account_id=account.id,
        kind="payment", amount_minor=transaction.amount_minor,
        payment_transaction_id=transaction.id,
        actor_user_id=actor_user_id,
        idempotency_key="pending:" + str(transaction.id),
        note="Admin-reviewed payment allocation reconciliation",
    )
    db.add(entry)
    db.flush()
    db.delete(pending)
    record_activity_event(
        db, workspace_id=workspace_id, actor_type="staff",
        actor_user_id=actor_user_id, action="vault.pending_payment_resolved",
        entity_type="payment_transaction", entity_id=transaction.id,
        summary="Pending vault payment posted after evidence review.",
        metadata={
            "pending_id":str(pending_id),
            "branch_id":str(branch_id),
            "account_id":str(account.id),
            "ledger_entry_id":str(entry.id),
            "amount_minor":transaction.amount_minor,
            "currency":transaction.currency,
            "allocation_ids":[str(a["allocation_id"]) for a in evidence_now],
            "review_explanation":explanation,
        },
    )
    db.flush()
    return entry


def _electronic_channel_account(
    db: Session, *, workspace_id: UUID, payment_method: str, lock: bool = False,
) -> VaultAccount | None:
    kinds = {"visa": "main_visa", "instapay": "main_instapay"}
    kind = kinds.get(payment_method)
    if kind is None:
        return None
    stmt = select(VaultAccount).where(
        VaultAccount.workspace_id == workspace_id,
        VaultAccount.kind == kind,
        VaultAccount.currency == "EGP",
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def _verified_electronic_receipt(
    db: Session, *, workspace_id: UUID, pending_id: UUID,
) -> tuple[VaultPendingPayment, PaymentTransaction]:
    row = db.scalar(select(VaultPendingPayment).where(
        VaultPendingPayment.workspace_id == workspace_id,
        VaultPendingPayment.id == pending_id,
    ))
    if row is None:
        raise VaultError("Pending electronic receipt no longer exists.")
    transaction = db.scalar(select(PaymentTransaction).where(
        PaymentTransaction.workspace_id == workspace_id,
        PaymentTransaction.id == row.payment_transaction_id,
    ))
    if transaction is None or (
        transaction.transaction_type != "payment"
        or transaction.payment_method not in ("visa", "instapay")
        or transaction.currency != "EGP"
        or transaction.source == "legacy_backfill"
    ):
        raise VaultError("Only supported EGP electronic receipts can be reviewed here.")
    return row, transaction


def _under_review_lock(
    db: Session, *, workspace_id: UUID, pending_id: UUID,
) -> VaultPendingPayment:
    row = db.scalar(select(VaultPendingPayment).where(
        VaultPendingPayment.workspace_id == workspace_id,
        VaultPendingPayment.id == pending_id,
    ).with_for_update())
    if row is None:
        raise VaultError("Pending electronic receipt was already posted or resolved.")
    return row


def _review_note(explanation: str) -> str:
    result = explanation.strip()
    if not 8 <= len(result) <= 500:
        raise VaultError("An auditable explanation of 8 to 500 characters is required.")
    return result


def review_preactivation_electronic(
    db: Session, *, workspace_id: UUID, pending_id: UUID,
    actor_user_id: UUID, outcome: str, explanation: str,
) -> VaultPendingPayment:
    """Admin attestation only. Never credits a payment from before activation.

    included_in_opening needs an independently declared sufficient opening
    balance. requires_accounting_decision deliberately leaves the receipt
    visible and unposted for the owner's separate reconciliation policy.
    """
    if outcome not in ("included_in_opening", "requires_accounting_decision"):
        raise VaultError("Unsupported opening balance reconciliation outcome.")
    explanation = _review_note(explanation)
    _, payment = _verified_electronic_receipt(
        db, workspace_id=workspace_id, pending_id=pending_id,
    )
    account = _electronic_channel_account(
        db, workspace_id=workspace_id,
        payment_method=payment.payment_method, lock=True,
    )
    if account is None:
        raise VaultError("Activate this treasury channel before opening balance review.")
    pending = _under_review_lock(db, workspace_id=workspace_id, pending_id=pending_id)
    if not _predates_channel_activation(pending, payment, account):
        raise VaultError("This receipt is after activation; use eligible posting retry.")
    if pending.review_outcome is not None:
        raise VaultError("This receipt already has an audited reconciliation outcome.")
    existing = db.scalar(select(VaultLedgerEntry.id).where(
        VaultLedgerEntry.workspace_id == workspace_id,
        VaultLedgerEntry.payment_transaction_id == payment.id,
    ))
    if existing is not None:
        raise VaultError("This electronic payment is already in the vault ledger.")

    if outcome == "included_in_opening":
        opening = int(db.scalar(select(func.coalesce(func.sum(VaultLedgerEntry.amount_minor), 0)).where(
            VaultLedgerEntry.workspace_id == workspace_id,
            VaultLedgerEntry.account_id == account.id,
            VaultLedgerEntry.kind == "opening",
        )) or 0)
        included = int(db.scalar(select(func.coalesce(func.sum(PaymentTransaction.amount_minor), 0))
            .join(
                VaultPendingPayment,
                and_(
                    VaultPendingPayment.workspace_id == PaymentTransaction.workspace_id,
                    VaultPendingPayment.payment_transaction_id == PaymentTransaction.id,
                ),
            )
            .where(
                VaultPendingPayment.workspace_id == workspace_id,
                VaultPendingPayment.reviewed_account_id == account.id,
                VaultPendingPayment.review_outcome == "included_in_opening",
            )
        ) or 0)
        if included + payment.amount_minor > opening:
            raise VaultError(
                "Declared channel opening balance does not cover all attested receipts."
            )

    from datetime import UTC, datetime

    pending.review_outcome = outcome
    pending.review_note = explanation
    pending.reviewed_at = datetime.now(UTC)
    pending.reviewed_by_user_id = actor_user_id
    pending.reviewed_account_id = account.id
    db.flush()
    record_activity_event(
        db, workspace_id=workspace_id, actor_type="staff",
        actor_user_id=actor_user_id, action="vault.electronic_preactivation_review",
        entity_type="payment_transaction", entity_id=payment.id,
        summary="Treasury electronic pre-activation receipt reconciled or held for policy.",
        metadata={
            "pending_id":str(pending_id),
            "account_id":str(account.id),
            "outcome":outcome,
            "amount_minor":payment.amount_minor,
            "payment_method":payment.payment_method,
            "opening_attestation":outcome == "included_in_opening",
            "review_explanation":explanation,
            "ledger_posted":False,
        },
    )
    return pending


def post_eligible_pending_electronic(
    db: Session, *, workspace_id: UUID, pending_id: UUID,
    actor_user_id: UUID, explanation: str,
) -> VaultLedgerEntry:
    """Explicit recovery for receipts dated after activation but still pending."""
    explanation = _review_note(explanation)
    _, payment = _verified_electronic_receipt(
        db, workspace_id=workspace_id, pending_id=pending_id,
    )
    account = _electronic_channel_account(
        db, workspace_id=workspace_id,
        payment_method=payment.payment_method, lock=True,
    )
    if account is None:
        raise VaultError("Treasury channel is not activated.")
    pending = _under_review_lock(db, workspace_id=workspace_id, pending_id=pending_id)
    if _predates_channel_activation(pending, payment, account):
        raise VaultError("Pre-activation electronic receipts cannot be auto-credited.")
    if pending.review_outcome is not None:
        raise VaultError("Already reviewed receipts cannot be reposted.")
    existing = db.scalar(select(VaultLedgerEntry.id).where(
        VaultLedgerEntry.workspace_id == workspace_id,
        VaultLedgerEntry.payment_transaction_id == payment.id,
    ))
    if existing is not None:
        raise VaultError("Payment has already been posted.")
    # Re-use the database's single source-of-truth posting function, including
    # its account row lock, unique source reference and pending-row deletion.
    db.execute(select(func.vault_try_post_payment(payment.id)))
    entry = db.scalar(select(VaultLedgerEntry).where(
        VaultLedgerEntry.workspace_id == workspace_id,
        VaultLedgerEntry.payment_transaction_id == payment.id,
        VaultLedgerEntry.account_id == account.id,
        VaultLedgerEntry.kind == "payment",
    ))
    if entry is None:
        raise VaultError("Electronic receipt could not be posted safely.")
    record_activity_event(
        db, workspace_id=workspace_id, actor_type="staff",
        actor_user_id=actor_user_id, action="vault.pending_electronic_posted",
        entity_type="payment_transaction", entity_id=payment.id,
        summary="Post-activation electronic receipt posted after Admin review.",
        metadata={
            "pending_id":str(pending_id),
            "ledger_entry_id":str(entry.id),
            "account_id":str(account.id),
            "amount_minor":payment.amount_minor,
            "review_explanation":explanation,
        },
    )
    return entry
