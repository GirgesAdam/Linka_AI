"""Protected reception and treasury API; only one workspace from auth context."""
from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies.section_access import require_section_access
from app.api.dependencies.security import (
    WorkspaceAccess,
    get_workspace_admin,
)
from app.database.session import get_db
from app.schemas.vault import (
    MainTreasuryRead,
    ReceptionVaultRead,
    TreasuryMovementRead,
    VaultActivationRead,
    VaultActivationRequest,
    VaultBranchGrantRead,
    VaultBranchGrantRequest,
    VaultExpenseRead,
    VaultExpenseRequest,
    VaultTransferRead,
    VaultTransferRequest,
)
from app.schemas.vault_pending import (
    VaultElectronicPostRead,
    VaultElectronicRetryRequest,
    VaultElectronicReviewRead,
    VaultElectronicReviewRequest,
    VaultPendingPaymentRead,
    VaultResolveRead,
    VaultResolveRequest,
)
from app.services.vault import (
    VaultError,
    activate_account,
    allowed_branch,
    list_branch_grants,
    main_treasury_summary,
    reception_summaries,
    record_cash_expense,
    set_branch_grant,
    transfer_full_balance,
    treasury_movements,
)
from app.services.vault_pending import (
    list_pending_payments,
    post_eligible_pending_electronic,
    resolve_pending_payment,
    review_preactivation_electronic,
)

router = APIRouter()


def _conflict(message: str) -> HTTPException:
    return HTTPException(status_code=409, detail=message)


def _safe_write(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict("Financial operation could not be committed. No balance changed.") from exc


@router.get("/reception", response_model=list[ReceptionVaultRead])
def reception_vaults(
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[ReceptionVaultRead]:
    rows = reception_summaries(
        db, workspace_id=access.workspace.id, actor_user_id=access.user.id,
        membership=access.membership, workspace_timezone=access.workspace.timezone,
    )
    return [ReceptionVaultRead.model_validate(row) for row in rows]


@router.post(
    "/reception/{branch_id}/expenses", response_model=VaultExpenseRead,
    status_code=status.HTTP_201_CREATED,
)
def reception_cash_expense(
    branch_id: UUID, payload: VaultExpenseRequest,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
) -> VaultExpenseRead:
    try:
        branch = allowed_branch(
            db, workspace_id=access.workspace.id, branch_id=branch_id,
            actor_user_id=access.user.id, membership=access.membership,
        )
        row = record_cash_expense(
            db, workspace_id=access.workspace.id, branch=branch,
            actor_user_id=access.user.id, amount_minor=payload.amount_minor,
            description=payload.description, idempotency_key=payload.idempotency_key,
            workspace_timezone=access.workspace.timezone,
        )
        _safe_write(db)
    except VaultError as exc:
        db.rollback()
        status_code = 403 if "access" in str(exc).lower() or "denied" in str(exc).lower() else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _conflict("Could not confirm the financial result. Refresh before retrying.") from exc
    return VaultExpenseRead(expense_id=row.id, amount_minor=row.amount_minor, currency=row.currency)


@router.post(
    "/reception/{branch_id}/transfer", response_model=VaultTransferRead,
    status_code=status.HTTP_201_CREATED,
)
def reception_transfer(
    branch_id: UUID, payload: VaultTransferRequest,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
) -> VaultTransferRead:
    try:
        allowed_branch(
            db, workspace_id=access.workspace.id, branch_id=branch_id,
            actor_user_id=access.user.id, membership=access.membership,
        )
        row = transfer_full_balance(
            db, workspace_id=access.workspace.id, branch_id=branch_id,
            actor_user_id=access.user.id, idempotency_key=payload.idempotency_key,
        )
        _safe_write(db)
    except VaultError as exc:
        db.rollback()
        status_code = 403 if "access" in str(exc).lower() or "denied" in str(exc).lower() else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _conflict("Could not confirm the financial result. Refresh before retrying.") from exc
    return VaultTransferRead(
        transfer_id=row.id, amount_minor=row.amount_minor,
        currency=row.currency, branch_id=row.branch_id,
    )


@router.get("/treasury", response_model=MainTreasuryRead)
def treasury(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> MainTreasuryRead:
    return MainTreasuryRead.model_validate(main_treasury_summary(
        db, workspace_id=access.workspace.id, workspace_timezone=access.workspace.timezone,
    ))


@router.post("/accounts/activate", response_model=VaultActivationRead, status_code=201)
def activate(
    payload: VaultActivationRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> VaultActivationRead:
    try:
        row = activate_account(
            db, workspace_id=access.workspace.id, actor_user_id=access.user.id,
            kind=payload.kind, branch_id=payload.branch_id,
            opening_balance_minor=payload.opening_balance_minor,
            currency=payload.currency,
        )
        _safe_write(db)
    except VaultError as exc:
        db.rollback()
        raise _conflict(str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _conflict("Could not confirm account activation. Refresh before retrying.") from exc
    return VaultActivationRead(
        account_id=row.id, kind=row.kind, branch_id=row.branch_id, currency=row.currency,
    )


@router.post("/branch-grants", status_code=204)
def branch_grant(
    payload: VaultBranchGrantRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    try:
        set_branch_grant(
            db, workspace_id=access.workspace.id, branch_id=payload.branch_id,
            user_id=payload.user_id, actor_user_id=access.user.id,
            enabled=payload.enabled,
        )
        _safe_write(db)
    except VaultError as exc:
        db.rollback()
        raise _conflict(str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _conflict("Could not confirm branch grant change.") from exc


@router.get("/branch-grants", response_model=list[VaultBranchGrantRead])
def branch_grant_list(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[VaultBranchGrantRead]:
    return [VaultBranchGrantRead.model_validate(row) for row in list_branch_grants(
        db, workspace_id=access.workspace.id,
    )]


@router.get("/treasury/movements", response_model=list[TreasuryMovementRead])
def treasury_history(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
    start_date: date | None = None, end_date: date | None = None,
    branch_id: UUID | None = None, kind: str | None = None,
    method: str | None = None, limit: int = Query(default=100, ge=1, le=500),
) -> list[TreasuryMovementRead]:
    try:
        rows = treasury_movements(
            db, workspace_id=access.workspace.id, timezone_name=access.workspace.timezone,
            start_date=start_date, end_date=end_date, branch_id=branch_id,
            kind=kind, method=method, limit=limit,
        )
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [TreasuryMovementRead.model_validate(row) for row in rows]


@router.get("/pending-payments", response_model=list[VaultPendingPaymentRead])
def pending_payments(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=100, ge=1, le=200),
) -> list[VaultPendingPaymentRead]:
    return [
        VaultPendingPaymentRead.model_validate(row)
        for row in list_pending_payments(db, workspace_id=access.workspace.id, limit=limit)
    ]


@router.post(
    "/pending-payments/{pending_id}/resolve",
    response_model=VaultResolveRead,
    status_code=status.HTTP_201_CREATED,
)
def resolve_pending(
    pending_id: UUID,
    payload: VaultResolveRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> VaultResolveRead:
    try:
        row = resolve_pending_payment(
            db, workspace_id=access.workspace.id, pending_id=pending_id,
            branch_id=payload.branch_id, actor_user_id=access.user.id,
            explanation=payload.explanation,
        )
        _safe_write(db)
    except VaultError as exc:
        db.rollback()
        raise _conflict(str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _conflict("Payment could not be resolved. Refresh to verify the status.") from exc
    return VaultResolveRead(
        entry_id=row.id, payment_transaction_id=row.payment_transaction_id,
        branch_id=payload.branch_id, amount_minor=row.amount_minor,
    )


@router.post(
    "/pending-payments/{pending_id}/electronic-opening-review",
    response_model=VaultElectronicReviewRead,
)
def review_electronic_opening(
    pending_id: UUID,
    payload: VaultElectronicReviewRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> VaultElectronicReviewRead:
    try:
        row = review_preactivation_electronic(
            db, workspace_id=access.workspace.id, pending_id=pending_id,
            actor_user_id=access.user.id,
            outcome=payload.outcome, explanation=payload.explanation,
        )
        _safe_write(db)
    except VaultError as exc:
        db.rollback()
        raise _conflict(str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _conflict("Electronic receipt review could not be confirmed.") from exc
    return VaultElectronicReviewRead(pending_id=row.id, outcome=row.review_outcome)


@router.post(
    "/pending-payments/{pending_id}/electronic-retry",
    response_model=VaultElectronicPostRead,
)
def retry_pending_electronic(
    pending_id: UUID,
    payload: VaultElectronicRetryRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> VaultResolveRead:
    try:
        row = post_eligible_pending_electronic(
            db, workspace_id=access.workspace.id, pending_id=pending_id,
            actor_user_id=access.user.id, explanation=payload.explanation,
        )
        _safe_write(db)
    except VaultError as exc:
        db.rollback()
        raise _conflict(str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _conflict("Electronic receipt posting could not be confirmed.") from exc
    return VaultElectronicPostRead(
        entry_id=row.id,
        payment_transaction_id=row.payment_transaction_id,
        amount_minor=row.amount_minor,
    )
