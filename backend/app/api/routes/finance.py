from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies.page_permissions import (
    require_page_permission,
)
from app.api.dependencies.security import WorkspaceAccess
from app.database.session import get_db
from app.schemas.finance import (
    ExpenseCreate,
    ExpenseRead,
    ExpenseUpdate,
    FinanceTrendRead,
    OutstandingBalancesRead,
    PaymentMethodBreakdownRead,
    ProfitabilityRead,
)
from app.services.finance import (
    FinanceNotFound,
    FinanceOperationError,
    create_expense,
    delete_expense,
    list_expenses,
    outstanding_balances,
    payment_method_breakdown,
    profitability_summary,
    update_expense,
)
from app.services.finance_dashboard import financial_dashboard_trend

router = APIRouter()


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _workspace_today(timezone_name: str, *, now: datetime | None = None) -> date:
    reference = now or datetime.now(UTC)
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    return reference.astimezone(timezone).date()


@router.get("/expenses", response_model=list[ExpenseRead])
def expense_list(
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("finance", legacy_access="reader"))],
    db: Annotated[Session, Depends(get_db)],
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> list[ExpenseRead]:
    try:
        rows = list_expenses(
            db,
            workspace_id=access.workspace.id,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
    except FinanceOperationError as exc:
        raise _bad_request(str(exc)) from exc
    return [ExpenseRead.model_validate(row) for row in rows]


@router.post("/expenses", response_model=ExpenseRead, status_code=status.HTTP_201_CREATED)
def expense_create(
    payload: ExpenseCreate,
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("finance", legacy_access="admin"))],
    db: Annotated[Session, Depends(get_db)],
) -> ExpenseRead:
    expense = create_expense(
        db,
        workspace_id=access.workspace.id,
        created_by_user_id=access.user.id,
        payload=payload,
    )
    db.commit()
    db.refresh(expense)
    return ExpenseRead.model_validate(expense)


@router.patch("/expenses/{expense_id}", response_model=ExpenseRead)
def expense_update(
    expense_id: UUID,
    payload: ExpenseUpdate,
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("finance", legacy_access="admin"))],
    db: Annotated[Session, Depends(get_db)],
) -> ExpenseRead:
    try:
        expense = update_expense(
            db,
            workspace_id=access.workspace.id,
            expense_id=expense_id,
            actor_user_id=access.user.id,
            payload=payload,
        )
        db.commit()
        db.refresh(expense)
    except FinanceNotFound as exc:
        db.rollback()
        raise _not_found(str(exc)) from exc
    return ExpenseRead.model_validate(expense)


@router.delete("/expenses/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
def expense_delete(
    expense_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("finance", legacy_access="admin"))],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    try:
        delete_expense(
            db,
            workspace_id=access.workspace.id,
            expense_id=expense_id,
            actor_user_id=access.user.id,
        )
        db.commit()
    except FinanceNotFound as exc:
        db.rollback()
        raise _not_found(str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/profitability", response_model=ProfitabilityRead)
def profitability(
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("finance", legacy_access="reader"))],
    db: Annotated[Session, Depends(get_db)],
    end_date: date | None = None,
    start_date: date | None = None,
) -> ProfitabilityRead:
    resolved_end = end_date or _workspace_today(access.workspace.timezone)
    resolved_start = start_date or (resolved_end - timedelta(days=29))
    try:
        return profitability_summary(
            db,
            workspace_id=access.workspace.id,
            timezone_name=access.workspace.timezone,
            start_date=resolved_start,
            end_date=resolved_end,
        )
    except FinanceOperationError as exc:
        raise _bad_request(str(exc)) from exc


@router.get("/dashboard-trend", response_model=FinanceTrendRead)
def dashboard_trend(
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("finance", legacy_access="reader"))],
    db: Annotated[Session, Depends(get_db)],
    start_date: date,
    end_date: date,
    mode: Literal["month", "year"],
) -> FinanceTrendRead:
    try:
        return financial_dashboard_trend(
            db,
            workspace_id=access.workspace.id,
            timezone_name=access.workspace.timezone,
            start_date=start_date,
            end_date=end_date,
            mode=mode,
            currency="EGP",
        )
    except FinanceOperationError as exc:
        raise _bad_request(str(exc)) from exc


@router.get("/payment-method-breakdown", response_model=PaymentMethodBreakdownRead)
def payment_breakdown(
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("finance", legacy_access="reader"))],
    db: Annotated[Session, Depends(get_db)],
    start_date: date | None = None,
    end_date: date | None = None,
) -> PaymentMethodBreakdownRead:
    resolved_end = end_date or _workspace_today(access.workspace.timezone)
    resolved_start = start_date or (resolved_end - timedelta(days=29))
    try:
        return payment_method_breakdown(
            db,
            workspace_id=access.workspace.id,
            timezone_name=access.workspace.timezone,
            start_date=resolved_start,
            end_date=resolved_end,
        )
    except FinanceOperationError as exc:
        raise _bad_request(str(exc)) from exc


@router.get("/outstanding-balances", response_model=OutstandingBalancesRead)
def outstanding_balance_report(
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("analytics.outstanding_balances", legacy_access="reader"))],
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=200, ge=1, le=500),
) -> OutstandingBalancesRead:
    return outstanding_balances(
        db,
        workspace_id=access.workspace.id,
        limit=limit,
    )
