from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.clinic_inventory import AppointmentProductLine
from app.models.expense import Expense
from app.models.patient import Patient
from app.models.payment_transaction import PaymentTransaction
from app.models.pulse_billing import AppointmentPulseSettlement, PatientPulsePack
from app.models.vault import VaultLedgerEntry
from app.schemas.finance import (
    ExpenseCreate,
    ExpenseUpdate,
    OutstandingBalanceRow,
    OutstandingBalancesRead,
    PaymentMethodBreakdownRead,
    PaymentMethodBreakdownRow,
    ProfitabilityCurrencyRead,
    ProfitabilityRead,
)
from app.services.activity import record_activity_event


class FinanceNotFound(ValueError):
    pass


class FinanceOperationError(ValueError):
    pass


def _expense_or_raise(db: Session, *, workspace_id: UUID, expense_id: UUID) -> Expense:
    expense = db.scalar(
        select(Expense).where(
            Expense.workspace_id == workspace_id,
            Expense.id == expense_id,
        )
    )
    if expense is None:
        raise FinanceNotFound("Expense not found.")
    return expense


def _require_unposted_expense(db: Session, expense: Expense) -> None:
    linked = db.scalar(select(VaultLedgerEntry.id).where(
        VaultLedgerEntry.workspace_id == expense.workspace_id,
        VaultLedgerEntry.expense_id == expense.id,
    ))
    if linked is not None:
        raise FinanceOperationError(
            "This reception vault expense is posted. Use an approved reversal instead of editing or deleting."
        )


def list_expenses(
    db: Session,
    *,
    workspace_id: UUID,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 200,
) -> list[Expense]:
    if start_date and end_date and start_date > end_date:
        raise FinanceOperationError("start_date must be on or before end_date.")
    stmt = select(Expense).where(Expense.workspace_id == workspace_id)
    if start_date is not None:
        stmt = stmt.where(Expense.incurred_on >= start_date)
    if end_date is not None:
        stmt = stmt.where(Expense.incurred_on <= end_date)
    return list(
        db.scalars(
            stmt.order_by(Expense.incurred_on.desc(), Expense.created_at.desc()).limit(limit)
        )
    )


def create_expense(
    db: Session,
    *,
    workspace_id: UUID,
    created_by_user_id: UUID,
    payload: ExpenseCreate,
) -> Expense:
    expense = Expense(
        workspace_id=workspace_id,
        created_by_user_id=created_by_user_id,
        **payload.model_dump(),
    )
    db.add(expense)
    db.flush()
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=created_by_user_id,
        action="expense.created",
        entity_type="expense",
        entity_id=expense.id,
        summary="Added an expense record.",
        metadata={
            "category": expense.category,
            "amount_minor": expense.amount_minor,
            "currency": expense.currency,
            "incurred_on": expense.incurred_on.isoformat(),
        },
    )
    return expense


def update_expense(
    db: Session,
    *,
    workspace_id: UUID,
    expense_id: UUID,
    actor_user_id: UUID,
    payload: ExpenseUpdate,
) -> Expense:
    expense = _expense_or_raise(db, workspace_id=workspace_id, expense_id=expense_id)
    _require_unposted_expense(db, expense)
    requested_changes = payload.model_dump(exclude_unset=True)
    changed_fields: list[str] = []
    for field_name, value in requested_changes.items():
        if getattr(expense, field_name) == value:
            continue
        setattr(expense, field_name, value)
        changed_fields.append(field_name)
    if not changed_fields:
        return expense

    db.flush()
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=actor_user_id,
        action="expense.updated",
        entity_type="expense",
        entity_id=expense.id,
        summary="Updated an expense record.",
        metadata={"changed_fields": sorted(changed_fields)},
    )
    return expense


def delete_expense(
    db: Session,
    *,
    workspace_id: UUID,
    expense_id: UUID,
    actor_user_id: UUID,
) -> None:
    expense = _expense_or_raise(db, workspace_id=workspace_id, expense_id=expense_id)
    _require_unposted_expense(db, expense)
    metadata = {
        "category": expense.category,
        "amount_minor": expense.amount_minor,
        "currency": expense.currency,
        "incurred_on": expense.incurred_on.isoformat(),
    }
    db.delete(expense)
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=actor_user_id,
        action="expense.deleted",
        entity_type="expense",
        entity_id=expense.id,
        summary="Deleted an expense record.",
        metadata=metadata,
    )
    db.flush()


def _utc_bounds(
    *,
    start_date: date,
    end_date: date,
    timezone_name: str,
) -> tuple[datetime, datetime]:
    if start_date > end_date:
        raise FinanceOperationError("start_date must be on or before end_date.")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    start_local = datetime.combine(start_date, time.min, tzinfo=timezone)
    end_local = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def profitability_summary(
    db: Session,
    *,
    workspace_id: UUID,
    timezone_name: str,
    start_date: date,
    end_date: date,
) -> ProfitabilityRead:
    start_at, end_at = _utc_bounds(
        start_date=start_date,
        end_date=end_date,
        timezone_name=timezone_name,
    )

    payment_rows = db.execute(
        select(
            PaymentTransaction.currency,
            func.coalesce(
                func.sum(
                    case(
                        (PaymentTransaction.transaction_type == "payment", PaymentTransaction.amount_minor),
                        else_=0,
                    )
                ),
                0,
            ).label("gross_payments_minor"),
            func.coalesce(
                func.sum(
                    case(
                        (PaymentTransaction.transaction_type == "refund", PaymentTransaction.amount_minor),
                        else_=0,
                    )
                ),
                0,
            ).label("refunds_minor"),
        )
        .where(
            PaymentTransaction.workspace_id == workspace_id,
            PaymentTransaction.created_at >= start_at,
            PaymentTransaction.created_at < end_at,
        )
        .group_by(PaymentTransaction.currency)
    ).all()
    expense_rows = db.execute(
        select(
            Expense.currency,
            func.coalesce(func.sum(Expense.amount_minor), 0).label("expenses_minor"),
        )
        .where(
            Expense.workspace_id == workspace_id,
            Expense.incurred_on >= start_date,
            Expense.incurred_on <= end_date,
        )
        .group_by(Expense.currency)
    ).all()

    payments = {
        str(currency).upper(): (int(gross or 0), int(refunds or 0))
        for currency, gross, refunds in payment_rows
    }
    expenses = {
        str(currency).upper(): int(amount or 0)
        for currency, amount in expense_rows
    }
    currencies = sorted(set(payments) | set(expenses))
    rows: list[ProfitabilityCurrencyRead] = []
    for currency in currencies:
        gross, refunds = payments.get(currency, (0, 0))
        expense_total = expenses.get(currency, 0)
        net_revenue = gross - refunds
        rows.append(
            ProfitabilityCurrencyRead(
                currency=currency,
                gross_payments_minor=gross,
                refunds_minor=refunds,
                net_revenue_minor=net_revenue,
                expenses_minor=expense_total,
                profit_minor=net_revenue - expense_total,
            )
        )
    return ProfitabilityRead(start_date=start_date, end_date=end_date, currencies=rows)


def payment_method_breakdown(
    db: Session,
    *,
    workspace_id: UUID,
    timezone_name: str,
    start_date: date,
    end_date: date,
) -> PaymentMethodBreakdownRead:
    start_at, end_at = _utc_bounds(
        start_date=start_date,
        end_date=end_date,
        timezone_name=timezone_name,
    )
    rows = db.execute(
        select(
            PaymentTransaction.payment_method,
            PaymentTransaction.currency,
            func.coalesce(func.sum(PaymentTransaction.amount_minor), 0),
            func.count(PaymentTransaction.id),
        )
        .where(
            PaymentTransaction.workspace_id == workspace_id,
            PaymentTransaction.transaction_type == "payment",
            PaymentTransaction.payment_method.in_(("cash", "visa", "instapay")),
            PaymentTransaction.created_at >= start_at,
            PaymentTransaction.created_at < end_at,
        )
        .group_by(PaymentTransaction.payment_method, PaymentTransaction.currency)
        .order_by(PaymentTransaction.currency, PaymentTransaction.payment_method)
    ).all()
    return PaymentMethodBreakdownRead(
        start_date=start_date,
        end_date=end_date,
        rows=[
            PaymentMethodBreakdownRow(
                payment_method=str(method),
                currency=str(currency).upper(),
                amount_minor=int(amount or 0),
                transaction_count=int(count or 0),
            )
            for method, currency, amount, count in rows
        ],
    )


def outstanding_balances(
    db: Session,
    *,
    workspace_id: UUID,
    limit: int = 200,
) -> OutstandingBalancesRead:
    """Return money still due after a session has actually been completed.

    Standard visits owe the service plus any products sold on the appointment.
    For package-prepaid visits the service is already covered by the package, but
    separately sold products can still leave an appointment-level balance.
    Future/confirmed visits never appear because only ``completed`` is eligible.
    """
    product_totals = (
        select(
            AppointmentProductLine.workspace_id.label("workspace_id"),
            AppointmentProductLine.appointment_id.label("appointment_id"),
            func.coalesce(
                func.sum(
                    AppointmentProductLine.quantity * AppointmentProductLine.unit_price_minor
                ),
                0,
            ).label("products_total_minor"),
        )
        .where(AppointmentProductLine.workspace_id == workspace_id)
        .group_by(
            AppointmentProductLine.workspace_id,
            AppointmentProductLine.appointment_id,
        )
        .subquery()
    )
    pulse_pack_totals = (
        select(
            PatientPulsePack.workspace_id.label("workspace_id"),
            PatientPulsePack.origin_appointment_id.label("appointment_id"),
            func.coalesce(func.sum(PatientPulsePack.sale_price_minor), 0).label(
                "pulse_pack_sales_minor"
            ),
        )
        .where(
            PatientPulsePack.workspace_id == workspace_id,
            PatientPulsePack.origin_appointment_id.is_not(None),
            PatientPulsePack.status != "cancelled",
        )
        .group_by(
            PatientPulsePack.workspace_id,
            PatientPulsePack.origin_appointment_id,
        )
        .subquery()
    )
    pulse_overage_totals = (
        select(
            AppointmentPulseSettlement.workspace_id.label("workspace_id"),
            AppointmentPulseSettlement.appointment_id.label("appointment_id"),
            func.coalesce(
                func.sum(AppointmentPulseSettlement.overage_charge_minor), 0
            ).label("pulse_overage_minor"),
        )
        .where(
            AppointmentPulseSettlement.workspace_id == workspace_id,
            AppointmentPulseSettlement.resolution == "overage",
        )
        .group_by(
            AppointmentPulseSettlement.workspace_id,
            AppointmentPulseSettlement.appointment_id,
        )
        .subquery()
    )
    products_due = func.coalesce(product_totals.c.products_total_minor, 0)
    pulse_pack_due = func.coalesce(pulse_pack_totals.c.pulse_pack_sales_minor, 0)
    pulse_overage_due = func.coalesce(pulse_overage_totals.c.pulse_overage_minor, 0)
    service_due = case(
        (
            Appointment.billing_context.in_(("package_prepaid", "pulse_prepaid")),
            0,
        ),
        else_=Appointment.price_minor,
    )
    total_due = service_due + products_due + pulse_pack_due + pulse_overage_due
    paid_expr = func.coalesce(Appointment.amount_paid_minor, 0)
    balance_expr = case(
        (total_due > paid_expr, total_due - paid_expr),
        else_=0,
    )
    rows = db.execute(
        select(
            Patient.id,
            Patient.first_name,
            Patient.last_name,
            Patient.phone,
            Appointment.currency,
            func.coalesce(func.sum(balance_expr), 0).label("balance_minor"),
            func.count(Appointment.id).label("appointment_count"),
        )
        .join(
            Patient,
            (Patient.workspace_id == Appointment.workspace_id)
            & (Patient.id == Appointment.patient_id),
        )
        .outerjoin(
            product_totals,
            (product_totals.c.workspace_id == Appointment.workspace_id)
            & (product_totals.c.appointment_id == Appointment.id),
        )
        .outerjoin(
            pulse_pack_totals,
            (pulse_pack_totals.c.workspace_id == Appointment.workspace_id)
            & (pulse_pack_totals.c.appointment_id == Appointment.id),
        )
        .outerjoin(
            pulse_overage_totals,
            (pulse_overage_totals.c.workspace_id == Appointment.workspace_id)
            & (pulse_overage_totals.c.appointment_id == Appointment.id),
        )
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.status == "completed",
            total_due > paid_expr,
        )
        .group_by(
            Patient.id,
            Patient.first_name,
            Patient.last_name,
            Patient.phone,
            Appointment.currency,
        )
        .order_by(func.sum(balance_expr).desc())
        .limit(max(1, min(limit, 500)))
    ).all()
    result_rows = [
        OutstandingBalanceRow(
            patient_id=patient_id,
            patient_name=f"{first_name or ''} {last_name or ''}".strip() or "عميل",
            phone=phone,
            currency=str(currency).upper(),
            balance_minor=int(balance or 0),
            appointment_count=int(count or 0),
        )
        for patient_id, first_name, last_name, phone, currency, balance, count in rows
        if int(balance or 0) > 0
    ]
    totals: dict[str, int] = {}
    for row in result_rows:
        totals[row.currency] = totals.get(row.currency, 0) + row.balance_minor
    return OutstandingBalancesRead(
        rows=result_rows,
        total_patients=len({row.patient_id for row in result_rows}),
        totals_by_currency=totals,
    )
