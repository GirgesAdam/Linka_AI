from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    require_tia_workspace_domain_write,
)
from app.models.activity_event import ActivityEvent
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.clinic_inventory import AppointmentProductLine
from app.models.patient_package import PatientPackage
from app.models.payment_transaction import PAYMENT_METHODS, PaymentAllocation, PaymentTransaction
from app.models.pulse_billing import AppointmentPulseSettlement, PatientPulsePack
from app.schemas.payments import AppointmentPaymentSummaryRead, PaymentTransactionRead
from app.services.activity import record_activity_event

STAFF_PAYMENT_METHODS = frozenset({"cash", "visa", "instapay"})


class PaymentOperationError(ValueError):
    pass


class PaymentOperationNotFound(PaymentOperationError):
    pass


@dataclass(frozen=True)
class PaymentTotals:
    gross_paid_minor: int
    refunded_minor: int
    net_paid_minor: int
    balance_minor: int
    payment_status: str
    payment_method: str


@dataclass(frozen=True)
class AppointmentLedgerEntry:
    """One transaction sliced to the amount explicitly allocated to an appointment."""

    transaction: PaymentTransaction
    allocated_amount_minor: int


def _row_transaction(row: PaymentTransaction | AppointmentLedgerEntry) -> PaymentTransaction:
    if isinstance(row, AppointmentLedgerEntry):
        return row.transaction
    return row


def _row_amount_minor(row: object) -> int:
    allocated = getattr(row, "allocated_amount_minor", None)
    if allocated is not None:
        return int(allocated)
    return int(row.amount_minor)


def _locked_appointment(db: Session, *, workspace_id: UUID, appointment_id: UUID) -> Appointment:
    appointment = db.scalar(
        select(Appointment)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.id == appointment_id,
        )
        .with_for_update()
    )
    if appointment is None:
        raise PaymentOperationNotFound("Appointment not found.")
    return appointment


def _ledger_rows(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    for_update: bool = False,
) -> list[AppointmentLedgerEntry]:
    stmt = (
        select(PaymentTransaction, PaymentAllocation.amount_minor)
        .join(
            PaymentAllocation,
            (PaymentAllocation.workspace_id == PaymentTransaction.workspace_id)
            & (PaymentAllocation.transaction_id == PaymentTransaction.id),
        )
        .where(
            PaymentTransaction.workspace_id == workspace_id,
            PaymentAllocation.appointment_id == appointment_id,
        )
        .order_by(PaymentTransaction.created_at, PaymentTransaction.id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return [
        AppointmentLedgerEntry(transaction=transaction, allocated_amount_minor=int(amount_minor))
        for transaction, amount_minor in db.execute(stmt).all()
    ]


def _appointment_charge_breakdown(
    db: Session,
    *,
    workspace_id: UUID,
    appointment: Appointment,
) -> tuple[int, int, int, int, int, int, int]:
    """Return service, products, extra services, package/pulse sales, overage, and due.

    Prepaid package or pulse appointments do not charge their standalone service
    price again. New packages/pulse packs bought during the visit are charged once.
    """
    products_total = int(
        db.scalar(
            select(
                func.coalesce(
                    func.sum(
                        AppointmentProductLine.quantity * AppointmentProductLine.unit_price_minor
                    ),
                    0,
                )
            ).where(
                AppointmentProductLine.workspace_id == workspace_id,
                AppointmentProductLine.appointment_id == appointment.id,
            )
        )
        or 0
    )
    additional_services_total = int(
        db.scalar(
            select(
                func.coalesce(
                    func.sum(AppointmentAdditionalService.unit_price_minor),
                    0,
                )
            ).where(
                AppointmentAdditionalService.workspace_id == workspace_id,
                AppointmentAdditionalService.appointment_id == appointment.id,
                AppointmentAdditionalService.patient_package_id.is_(None),
                AppointmentAdditionalService.billing_context != "pulse_prepaid",
            )
        )
        or 0
    )
    package_sales_total = int(
        db.scalar(
            select(func.coalesce(func.sum(PatientPackage.sale_price_minor), 0)).where(
                PatientPackage.workspace_id == workspace_id,
                PatientPackage.origin_appointment_id == appointment.id,
                PatientPackage.status != "cancelled",
            )
        )
        or 0
    )
    pulse_pack_sales_total = int(
        db.scalar(
            select(func.coalesce(func.sum(PatientPulsePack.sale_price_minor), 0)).where(
                PatientPulsePack.workspace_id == workspace_id,
                PatientPulsePack.origin_appointment_id == appointment.id,
                PatientPulsePack.status != "cancelled",
            )
        )
        or 0
    )
    primary_pulse_overage_total = int(
        db.scalar(
            select(
                func.coalesce(
                    func.sum(AppointmentPulseSettlement.overage_charge_minor),
                    0,
                )
            ).where(
                AppointmentPulseSettlement.workspace_id == workspace_id,
                AppointmentPulseSettlement.appointment_id == appointment.id,
                AppointmentPulseSettlement.resolution == "overage",
            )
        )
        or 0
    )
    additional_pulse_overage_total = int(
        db.scalar(
            select(
                func.coalesce(
                    func.sum(AppointmentAdditionalService.pulse_overage_charge_minor),
                    0,
                )
            ).where(
                AppointmentAdditionalService.workspace_id == workspace_id,
                AppointmentAdditionalService.appointment_id == appointment.id,
                AppointmentAdditionalService.billing_context == "pulse_prepaid",
                AppointmentAdditionalService.pulse_resolution == "overage",
            )
        )
        or 0
    )
    pulse_overage_total = primary_pulse_overage_total + additional_pulse_overage_total
    service_price = int(appointment.price_minor)
    service_due = (
        0
        if getattr(appointment, "billing_context", "standard")
        in {"package_prepaid", "pulse_prepaid"}
        else service_price
    )
    subtotal = (
        service_due
        + products_total
        + additional_services_total
        + package_sales_total
        + pulse_pack_sales_total
        + pulse_overage_total
    )
    discount = max(int(getattr(appointment, "discount_minor", 0) or 0), 0)
    return (
        service_price,
        products_total,
        additional_services_total,
        package_sales_total,
        pulse_pack_sales_total,
        pulse_overage_total,
        max(subtotal - discount, 0),
    )


def _payment_totals(
    *,
    appointment: Appointment,
    rows: list[object],
    due_minor: int | None = None,
) -> PaymentTotals:
    due = int(appointment.price_minor) if due_minor is None else max(int(due_minor), 0)
    payments = [row for row in rows if _row_transaction(row).transaction_type == "payment"]
    refunds = [row for row in rows if _row_transaction(row).transaction_type == "refund"]
    gross = sum(_row_amount_minor(row) for row in payments)
    refunded = sum(_row_amount_minor(row) for row in refunds)
    net = max(gross - refunded, 0)
    balance = max(due - net, 0)

    if due == 0 and net == 0:
        status = "paid"
    elif gross > 0 and net == 0 and refunded > 0:
        status = "refunded"
    elif net <= 0:
        status = "unpaid"
    elif net >= due:
        status = "paid"
    else:
        status = "partial"

    methods = {
        _row_transaction(row).payment_method
        for row in payments
        if _row_transaction(row).payment_method != "unknown"
    }
    if not payments:
        method = "unknown"
    elif len(methods) == 1:
        method = next(iter(methods))
    elif not methods:
        method = "unknown"
    else:
        method = "other"
    if method == "online":
        method = "other"

    return PaymentTotals(
        gross_paid_minor=gross,
        refunded_minor=refunded,
        net_paid_minor=net,
        balance_minor=balance,
        payment_status=status,
        payment_method=method,
    )


def _refunds_by_payment(rows: list[object]) -> dict[UUID, int]:
    result: dict[UUID, int] = {}
    for row in rows:
        transaction = _row_transaction(row)
        if transaction.transaction_type == "refund" and transaction.reference_transaction_id is not None:
            result[transaction.reference_transaction_id] = (
                result.get(transaction.reference_transaction_id, 0) + _row_amount_minor(row)
            )
    return result


def _transaction_reads(
    rows: list[PaymentTransaction | AppointmentLedgerEntry],
) -> list[PaymentTransactionRead]:
    refunds = _refunds_by_payment(list(rows))
    result: list[PaymentTransactionRead] = []
    for row in rows:
        transaction = _row_transaction(row)
        allocated_amount_minor = _row_amount_minor(row)
        refunded_minor = (
            refunds.get(transaction.id, 0)
            if transaction.transaction_type == "payment"
            else 0
        )
        refundable_minor = (
            max(allocated_amount_minor - refunded_minor, 0)
            if transaction.transaction_type == "payment"
            else 0
        )
        result.append(
            PaymentTransactionRead(
                id=transaction.id,
                workspace_id=transaction.workspace_id,
                appointment_id=transaction.appointment_id,
                origin_appointment_id=transaction.origin_appointment_id,
                patient_id=transaction.patient_id,
                created_by_user_id=transaction.created_by_user_id,
                reference_transaction_id=transaction.reference_transaction_id,
                patient_package_id=transaction.patient_package_id,
                patient_pulse_pack_id=transaction.patient_pulse_pack_id,
                transaction_type=transaction.transaction_type,
                amount_minor=transaction.amount_minor,
                allocated_amount_minor=allocated_amount_minor,
                currency=transaction.currency,
                payment_method=transaction.payment_method,
                source=transaction.source,
                external_reference=transaction.external_reference,
                reason=transaction.reason,
                created_at=transaction.created_at,
                refunded_minor=refunded_minor,
                refundable_minor=refundable_minor,
            )
        )
    return result


def _transaction_has_allocation(
    db: Session,
    *,
    workspace_id: UUID,
    transaction_id: UUID,
    appointment_id: UUID,
) -> bool:
    allocation_id = db.scalar(
        select(PaymentAllocation.id).where(
            PaymentAllocation.workspace_id == workspace_id,
            PaymentAllocation.transaction_id == transaction_id,
            PaymentAllocation.appointment_id == appointment_id,
        )
    )
    return allocation_id is not None


def _add_single_appointment_allocation(
    db: Session,
    *,
    transaction: PaymentTransaction,
    appointment_id: UUID,
    amount_minor: int,
) -> PaymentAllocation:
    if amount_minor <= 0 or amount_minor > int(transaction.amount_minor):
        raise PaymentOperationError(
            "Payment allocation must be positive and cannot exceed the transaction amount."
        )
    allocation = PaymentAllocation(
        workspace_id=transaction.workspace_id,
        transaction_id=transaction.id,
        appointment_id=appointment_id,
        amount_minor=amount_minor,
        created_at=transaction.created_at,
    )
    db.add(allocation)
    db.flush()
    return allocation


def validate_allocation_total(
    *, transaction_amount_minor: int, allocation_amounts: list[int]
) -> None:
    if transaction_amount_minor <= 0:
        raise PaymentOperationError("Payment amount must be positive.")
    if any(int(amount) <= 0 for amount in allocation_amounts):
        raise PaymentOperationError("Payment allocations must be positive.")
    if sum(int(amount) for amount in allocation_amounts) > int(transaction_amount_minor):
        raise PaymentOperationError(
            "Payment allocations cannot exceed the transaction amount."
        )


def sync_appointment_payment_snapshot(
    appointment: Appointment,
    rows: list[object],
    *,
    due_minor: int | None = None,
) -> PaymentTotals:
    totals = _payment_totals(appointment=appointment, rows=rows, due_minor=due_minor)
    appointment.payment_status = totals.payment_status
    if (
        getattr(appointment, "billing_context", "standard")
        in {"package_prepaid", "pulse_prepaid"}
        and totals.gross_paid_minor == 0
        and totals.refunded_minor == 0
        and (due_minor is None or due_minor == 0)
    ):
        appointment.amount_paid_minor = None
    else:
        appointment.amount_paid_minor = totals.net_paid_minor
    appointment.payment_method = totals.payment_method
    return totals


def refresh_appointment_payment_snapshots(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_ids: set[UUID],
) -> None:
    for appointment_id in sorted(appointment_ids, key=str):
        appointment = _locked_appointment(
            db, workspace_id=workspace_id, appointment_id=appointment_id
        )
        rows = _ledger_rows(
            db,
            workspace_id=workspace_id,
            appointment_id=appointment_id,
            for_update=True,
        )
        (
            _service_price,
            _products_total,
            _additional_services_total,
            _package_sales_total,
            _pulse_pack_sales_total,
            _pulse_overage_total,
            due,
        ) = _appointment_charge_breakdown(
            db,
            workspace_id=workspace_id,
            appointment=appointment,
        )
        sync_appointment_payment_snapshot(appointment, list(rows), due_minor=due)


def get_appointment_payment_summary(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    can_refund: bool = False,
) -> AppointmentPaymentSummaryRead:
    appointment = db.scalar(
        select(Appointment).where(
            Appointment.workspace_id == workspace_id,
            Appointment.id == appointment_id,
        )
    )
    if appointment is None:
        raise PaymentOperationNotFound("Appointment not found.")
    rows = _ledger_rows(db, workspace_id=workspace_id, appointment_id=appointment.id)
    (
        service_price,
        products_total,
        additional_services_total,
        package_sales_total,
        pulse_pack_sales_total,
        pulse_overage_total,
        due,
    ) = _appointment_charge_breakdown(
        db,
        workspace_id=workspace_id,
        appointment=appointment,
    )
    discount = int(getattr(appointment, "discount_minor", 0) or 0)
    subtotal = due + discount
    totals = _payment_totals(appointment=appointment, rows=list(rows), due_minor=due)
    return AppointmentPaymentSummaryRead(
        appointment_id=appointment.id,
        patient_id=appointment.patient_id,
        currency=appointment.currency,
        price_minor=due,
        subtotal_minor=subtotal,
        discount_minor=discount,
        service_price_minor=service_price,
        products_total_minor=products_total,
        additional_services_total_minor=additional_services_total,
        package_sales_total_minor=package_sales_total,
        pulse_pack_sales_total_minor=pulse_pack_sales_total,
        pulse_overage_total_minor=pulse_overage_total,
        gross_paid_minor=totals.gross_paid_minor,
        refunded_minor=totals.refunded_minor,
        net_paid_minor=totals.net_paid_minor,
        balance_minor=totals.balance_minor,
        payment_status=totals.payment_status,
        billing_context=getattr(appointment, "billing_context", "standard"),
        package_external_id=getattr(appointment, "package_external_id", None),
        transactions=_transaction_reads(rows),
        can_refund=can_refund,
    )



def _visit_packages(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    for_update: bool = False,
) -> list[PatientPackage]:
    stmt = (
        select(PatientPackage)
        .where(
            PatientPackage.workspace_id == workspace_id,
            PatientPackage.origin_appointment_id == appointment_id,
            PatientPackage.status != "cancelled",
        )
        .order_by(PatientPackage.purchased_at, PatientPackage.id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list(db.scalars(stmt).all())


def _visit_pulse_packs(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    for_update: bool = False,
) -> list[PatientPulsePack]:
    stmt = (
        select(PatientPulsePack)
        .where(
            PatientPulsePack.workspace_id == workspace_id,
            PatientPulsePack.origin_appointment_id == appointment_id,
            PatientPulsePack.status != "cancelled",
        )
        .order_by(PatientPulsePack.purchased_at, PatientPulsePack.id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list(db.scalars(stmt).all())


def _pulse_pack_net_paid_minor(
    db: Session,
    *,
    workspace_id: UUID,
    pulse_pack_id: UUID,
) -> int:
    payments = list(
        db.scalars(
            select(PaymentTransaction).where(
                PaymentTransaction.workspace_id == workspace_id,
                PaymentTransaction.patient_pulse_pack_id == pulse_pack_id,
                PaymentTransaction.transaction_type == "payment",
            )
        ).all()
    )
    payment_ids = [row.id for row in payments]
    refunds: list[PaymentTransaction] = []
    if payment_ids:
        refunds = list(
            db.scalars(
                select(PaymentTransaction).where(
                    PaymentTransaction.workspace_id == workspace_id,
                    PaymentTransaction.transaction_type == "refund",
                    (
                        PaymentTransaction.patient_pulse_pack_id == pulse_pack_id
                    )
                    | PaymentTransaction.reference_transaction_id.in_(payment_ids),
                )
            ).all()
        )
    return max(
        sum(int(row.amount_minor) for row in payments)
        - sum(int(row.amount_minor) for row in refunds),
        0,
    )


def _package_net_paid_minor(
    db: Session,
    *,
    workspace_id: UUID,
    package_id: UUID,
) -> int:
    payments = list(
        db.scalars(
            select(PaymentTransaction).where(
                PaymentTransaction.workspace_id == workspace_id,
                PaymentTransaction.patient_package_id == package_id,
                PaymentTransaction.transaction_type == "payment",
            )
        ).all()
    )
    payment_ids = [row.id for row in payments]
    refunds: list[PaymentTransaction] = []
    if payment_ids:
        refunds = list(
            db.scalars(
                select(PaymentTransaction).where(
                    PaymentTransaction.workspace_id == workspace_id,
                    PaymentTransaction.transaction_type == "refund",
                    (
                        (PaymentTransaction.patient_package_id == package_id)
                        | (PaymentTransaction.reference_transaction_id.in_(payment_ids))
                    ),
                )
            ).all()
        )
    return max(
        sum(int(row.amount_minor) for row in payments)
        - sum(int(row.amount_minor) for row in refunds),
        0,
    )


def _payment_part_key(base_key: str | None, index: int) -> str | None:
    if not base_key:
        return None
    if index == 0:
        return base_key
    suffix = f":part{index + 1}"
    return f"{base_key[: 128 - len(suffix)]}{suffix}"


def _payment_recorded_metadata(
    db: Session, *, workspace_id: UUID, transaction_id: UUID
) -> dict:
    event = db.scalar(
        select(ActivityEvent)
        .where(
            ActivityEvent.workspace_id == workspace_id,
            ActivityEvent.action == "payment.recorded",
            ActivityEvent.entity_type == "payment_transaction",
            ActivityEvent.entity_id == transaction_id,
        )
        .order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
        .limit(1)
    )
    return dict(event.metadata_json or {}) if event is not None else {}


def _assert_same_payment_replay(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    existing: PaymentTransaction,
    amount_minor: int,
    payment_method: str,
    discount_minor: int | None,
    external_reference: str | None,
    source: str,
) -> None:
    metadata = _payment_recorded_metadata(
        db, workspace_id=workspace_id, transaction_id=existing.id
    )
    recorded_amount = int(metadata.get("amount_minor", existing.amount_minor))
    mismatch = (
        existing.transaction_type != "payment"
        or not _transaction_has_allocation(
            db,
            workspace_id=workspace_id,
            transaction_id=existing.id,
            appointment_id=appointment_id,
        )
        or recorded_amount != amount_minor
        or existing.payment_method != payment_method
        or existing.source != source
        or existing.external_reference != external_reference
    )
    if not mismatch:
        recorded_discount_mode = metadata.get("discount_mode")
        if recorded_discount_mode is not None:
            requested_mode = "unchanged" if discount_minor is None else "set"
            mismatch = recorded_discount_mode != requested_mode
            if not mismatch and discount_minor is not None:
                mismatch = int(metadata.get("discount_minor", -1)) != int(discount_minor)
        elif discount_minor is not None:
            appointment = db.scalar(
                select(Appointment).where(
                    Appointment.workspace_id == workspace_id,
                    Appointment.id == appointment_id,
                )
            )
            mismatch = (
                appointment is None
                or int(getattr(appointment, "discount_minor", 0) or 0)
                != int(discount_minor)
            )
    if mismatch:
        raise PaymentOperationError(
            "Idempotency key was already used for a different payment operation."
        )


def _assert_same_refund_replay(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    existing: PaymentTransaction,
    payment_transaction_id: UUID,
    amount_minor: int,
    reason: str,
) -> None:
    if (
        existing.transaction_type != "refund"
        or not _transaction_has_allocation(
            db,
            workspace_id=workspace_id,
            transaction_id=existing.id,
            appointment_id=appointment_id,
        )
        or existing.reference_transaction_id != payment_transaction_id
        or int(existing.amount_minor) != amount_minor
        or (existing.reason or "") != reason[:500]
    ):
        raise PaymentOperationError(
            "Idempotency key was already used for a different refund operation."
        )


def _set_discount_locked(
    db: Session,
    *,
    appointment: Appointment,
    rows: list[AppointmentLedgerEntry],
    discount_minor: int,
    actor_user_id: UUID | None,
) -> None:
    if discount_minor < 0:
        raise PaymentOperationError("Discount cannot be negative.")
    (
        _service_price,
        _products_total,
        _additional_services_total,
        _package_sales_total,
        _pulse_pack_sales_total,
        _pulse_overage_total,
        current_due,
    ) = _appointment_charge_breakdown(
        db,
        workspace_id=appointment.workspace_id,
        appointment=appointment,
    )
    current_discount = int(getattr(appointment, "discount_minor", 0) or 0)
    subtotal = current_due + current_discount
    if discount_minor > subtotal:
        raise PaymentOperationError("Discount cannot exceed the visit subtotal.")
    totals_before = _payment_totals(
        appointment=appointment,
        rows=list(rows),
        due_minor=subtotal,
    )
    new_due = subtotal - discount_minor
    if totals_before.net_paid_minor > new_due:
        raise PaymentOperationError(
            "The new discount would make recorded payments exceed the visit total. "
            "Refund the excess payment first."
        )
    previous = int(getattr(appointment, "discount_minor", 0) or 0)
    appointment.discount_minor = discount_minor
    sync_appointment_payment_snapshot(appointment, list(rows), due_minor=new_due)
    if previous != discount_minor:
        record_activity_event(
            db,
            workspace_id=appointment.workspace_id,
            actor_type="staff",
            actor_user_id=actor_user_id,
            action="appointment.discount_updated",
            entity_type="appointment",
            entity_id=appointment.id,
            summary="Appointment discount updated",
            metadata={
                "previous_discount_minor": previous,
                "discount_minor": discount_minor,
                "subtotal_minor": subtotal,
            },
        )


def set_appointment_discount(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    discount_minor: int,
    created_by_user_id: UUID | None,
) -> None:
    try:
        require_tia_workspace_domain_write(db, workspace_id=workspace_id, domain="payments")
    except ClinicIntegrationAuthorityError as exc:
        raise PaymentOperationError(str(exc)) from exc
    appointment = _locked_appointment(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
    )
    if appointment.status not in {"pending", "confirmed", "completed"}:
        raise PaymentOperationError(
            f"Discounts cannot be changed for an appointment in '{appointment.status}' status."
        )
    rows = _ledger_rows(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment.id,
        for_update=True,
    )
    _set_discount_locked(
        db,
        appointment=appointment,
        rows=rows,
        discount_minor=discount_minor,
        actor_user_id=created_by_user_id,
    )
    db.flush()


def record_payment(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    amount_minor: int,
    payment_method: str,
    created_by_user_id: UUID | None,
    discount_minor: int | None = None,
    external_reference: str | None = None,
    idempotency_key: str | None = None,
    source: str = "staff",
) -> PaymentTransaction:
    if amount_minor <= 0:
        raise PaymentOperationError("Payment amount must be positive.")
    if source not in {"staff", "integration", "system"}:
        raise PaymentOperationError("Unsupported payment source.")
    if source == "integration":
        if payment_method not in PAYMENT_METHODS:
            raise PaymentOperationError("Unsupported payment method.")
    elif payment_method not in STAFF_PAYMENT_METHODS:
        raise PaymentOperationError("Payment method must be cash, visa, or instapay.")

    if source != "integration":
        try:
            require_tia_workspace_domain_write(
                db, workspace_id=workspace_id, domain="payments"
            )
        except ClinicIntegrationAuthorityError as exc:
            raise PaymentOperationError(str(exc)) from exc

    if idempotency_key:
        existing = db.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.workspace_id == workspace_id,
                PaymentTransaction.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            _assert_same_payment_replay(
                db,
                workspace_id=workspace_id,
                appointment_id=appointment_id,
                existing=existing,
                amount_minor=amount_minor,
                payment_method=payment_method,
                discount_minor=discount_minor,
                external_reference=external_reference,
                source=source,
            )
            return existing

    appointment = _locked_appointment(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
    )
    if appointment.status not in {"pending", "confirmed", "completed"}:
        raise PaymentOperationError(
            f"Payments cannot be recorded for an appointment in '{appointment.status}' status."
        )
    rows = _ledger_rows(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment.id,
        for_update=True,
    )
    if discount_minor is not None:
        _set_discount_locked(
            db,
            appointment=appointment,
            rows=rows,
            discount_minor=discount_minor,
            actor_user_id=created_by_user_id,
        )
    (
        _service_price,
        _products_total,
        _additional_services_total,
        _package_sales_total,
        _pulse_pack_sales_total,
        _pulse_overage_total,
        due,
    ) = _appointment_charge_breakdown(
        db,
        workspace_id=workspace_id,
        appointment=appointment,
    )
    totals = _payment_totals(appointment=appointment, rows=list(rows), due_minor=due)
    if totals.balance_minor <= 0:
        raise PaymentOperationError("This appointment has no outstanding balance.")
    if amount_minor > totals.balance_minor:
        raise PaymentOperationError(
            f"Payment exceeds outstanding balance of {totals.balance_minor} "
            f"{appointment.currency} minor units."
        )

    remaining = amount_minor
    created_transactions: list[PaymentTransaction] = []
    transaction_index = 0
    now = datetime.now(UTC)

    for package in _visit_packages(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment.id,
        for_update=True,
    ):
        if remaining <= 0:
            break
        package_balance = max(
            int(package.sale_price_minor)
            - _package_net_paid_minor(
                db,
                workspace_id=workspace_id,
                package_id=package.id,
            ),
            0,
        )
        if package_balance <= 0:
            continue
        allocated = min(remaining, package_balance)
        transaction = PaymentTransaction(
            workspace_id=workspace_id,
            appointment_id=appointment.id,
            origin_appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            created_by_user_id=created_by_user_id,
            reference_transaction_id=None,
            patient_package_id=package.id,
            transaction_type="payment",
            amount_minor=allocated,
            currency=appointment.currency,
            payment_method=payment_method,
            source=source,
            external_reference=external_reference,
            reason="Package payment from appointment checkout",
            idempotency_key=_payment_part_key(idempotency_key, transaction_index),
            created_at=now,
        )
        db.add(transaction)
        db.flush()
        _add_single_appointment_allocation(
            db,
            transaction=transaction,
            appointment_id=appointment.id,
            amount_minor=allocated,
        )
        if package.purchase_transaction_id is None:
            package.purchase_transaction_id = transaction.id
        rows.append(
            AppointmentLedgerEntry(
                transaction=transaction,
                allocated_amount_minor=allocated,
            )
        )
        created_transactions.append(transaction)
        transaction_index += 1
        remaining -= allocated

    for pulse_pack in _visit_pulse_packs(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment.id,
        for_update=True,
    ):
        if remaining <= 0:
            break
        pulse_pack_balance = max(
            int(pulse_pack.sale_price_minor)
            - _pulse_pack_net_paid_minor(
                db,
                workspace_id=workspace_id,
                pulse_pack_id=pulse_pack.id,
            ),
            0,
        )
        if pulse_pack_balance <= 0:
            continue
        allocated = min(remaining, pulse_pack_balance)
        transaction = PaymentTransaction(
            workspace_id=workspace_id,
            appointment_id=appointment.id,
            origin_appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            created_by_user_id=created_by_user_id,
            reference_transaction_id=None,
            patient_package_id=None,
            patient_pulse_pack_id=pulse_pack.id,
            transaction_type="payment",
            amount_minor=allocated,
            currency=appointment.currency,
            payment_method=payment_method,
            source=source,
            external_reference=external_reference,
            reason="Pulse pack payment from appointment checkout",
            idempotency_key=_payment_part_key(idempotency_key, transaction_index),
            created_at=now,
        )
        db.add(transaction)
        db.flush()
        _add_single_appointment_allocation(
            db,
            transaction=transaction,
            appointment_id=appointment.id,
            amount_minor=allocated,
        )
        if pulse_pack.purchase_transaction_id is None:
            pulse_pack.purchase_transaction_id = transaction.id
        rows.append(
            AppointmentLedgerEntry(
                transaction=transaction,
                allocated_amount_minor=allocated,
            )
        )
        created_transactions.append(transaction)
        transaction_index += 1
        remaining -= allocated

    if remaining > 0:
        transaction = PaymentTransaction(
            workspace_id=workspace_id,
            appointment_id=appointment.id,
            origin_appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            created_by_user_id=created_by_user_id,
            reference_transaction_id=None,
            patient_package_id=None,
            transaction_type="payment",
            amount_minor=remaining,
            currency=appointment.currency,
            payment_method=payment_method,
            source=source,
            external_reference=external_reference,
            reason=None,
            idempotency_key=_payment_part_key(idempotency_key, transaction_index),
            created_at=now,
        )
        db.add(transaction)
        db.flush()
        _add_single_appointment_allocation(
            db,
            transaction=transaction,
            appointment_id=appointment.id,
            amount_minor=remaining,
        )
        rows.append(
            AppointmentLedgerEntry(
                transaction=transaction,
                allocated_amount_minor=remaining,
            )
        )
        created_transactions.append(transaction)

    sync_appointment_payment_snapshot(appointment, list(rows), due_minor=due)
    primary_transaction = created_transactions[0]
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff" if created_by_user_id else "system",
        actor_user_id=created_by_user_id,
        action="payment.recorded",
        entity_type="payment_transaction",
        entity_id=primary_transaction.id,
        summary="Payment recorded",
        metadata={
            "appointment_id": appointment.id,
            "amount_minor": amount_minor,
            "currency": appointment.currency,
            "payment_method": payment_method,
            "source": source,
            "discount_mode": "unchanged" if discount_minor is None else "set",
            "discount_minor": discount_minor,
            "transaction_ids": [str(row.id) for row in created_transactions],
        },
    )
    db.flush()
    return primary_transaction


def record_refund(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    payment_transaction_id: UUID,
    amount_minor: int,
    reason: str,
    created_by_user_id: UUID,
    idempotency_key: str | None = None,
) -> PaymentTransaction:
    if amount_minor <= 0:
        raise PaymentOperationError("Refund amount must be positive.")
    try:
        require_tia_workspace_domain_write(
            db, workspace_id=workspace_id, domain="payments"
        )
    except ClinicIntegrationAuthorityError as exc:
        raise PaymentOperationError(str(exc)) from exc
    reason = reason.strip()
    if not reason:
        raise PaymentOperationError("Refund reason cannot be empty.")

    if idempotency_key:
        existing = db.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.workspace_id == workspace_id,
                PaymentTransaction.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            _assert_same_refund_replay(
                db,
                workspace_id=workspace_id,
                appointment_id=appointment_id,
                existing=existing,
                payment_transaction_id=payment_transaction_id,
                amount_minor=amount_minor,
                reason=reason,
            )
            return existing

    appointment = _locked_appointment(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
    )
    if appointment.status == "rescheduled":
        raise PaymentOperationError(
            "Refunds must be recorded on the replacement appointment."
        )
    rows = _ledger_rows(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment.id,
        for_update=True,
    )
    payment_entry = next(
        (
            row
            for row in rows
            if row.transaction.id == payment_transaction_id
            and row.transaction.transaction_type == "payment"
        ),
        None,
    )
    if payment_entry is None:
        raise PaymentOperationNotFound(
            "Payment transaction not found for this appointment."
        )
    payment = payment_entry.transaction
    already_refunded = _refunds_by_payment(list(rows)).get(payment.id, 0)
    refundable = max(int(payment_entry.allocated_amount_minor) - already_refunded, 0)
    if amount_minor > refundable:
        raise PaymentOperationError(
            f"Refund exceeds refundable amount of {refundable} "
            f"{appointment.currency} minor units."
        )

    transaction = PaymentTransaction(
        workspace_id=workspace_id,
        appointment_id=appointment.id,
        origin_appointment_id=appointment.id,
        patient_id=appointment.patient_id,
        created_by_user_id=created_by_user_id,
        reference_transaction_id=payment.id,
        transaction_type="refund",
        amount_minor=amount_minor,
        currency=appointment.currency,
        payment_method=payment.payment_method,
        source="staff",
        external_reference=None,
        reason=reason[:500],
        idempotency_key=idempotency_key,
        created_at=datetime.now(UTC),
    )
    db.add(transaction)
    db.flush()
    _add_single_appointment_allocation(
        db,
        transaction=transaction,
        appointment_id=appointment.id,
        amount_minor=amount_minor,
    )
    rows.append(
        AppointmentLedgerEntry(
            transaction=transaction,
            allocated_amount_minor=amount_minor,
        )
    )
    (
        _service_price,
        _products_total,
        _additional_services_total,
        _package_sales_total,
        _pulse_pack_sales_total,
        _pulse_overage_total,
        due,
    ) = _appointment_charge_breakdown(
        db,
        workspace_id=workspace_id,
        appointment=appointment,
    )
    sync_appointment_payment_snapshot(appointment, list(rows), due_minor=due)
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=created_by_user_id,
        action="payment.refunded",
        entity_type="payment_transaction",
        entity_id=transaction.id,
        summary="Payment refunded",
        metadata={
            "appointment_id": appointment.id,
            "payment_transaction_id": payment.id,
            "amount_minor": amount_minor,
            "currency": appointment.currency,
            "reason_present": True,
        },
    )
    db.flush()
    return transaction


def reallocate_appointment_payments_on_reschedule(
    db: Session,
    *,
    workspace_id: UUID,
    from_appointment_id: UUID,
    to_appointment_id: UUID,
) -> None:
    db.execute(
        update(PaymentAllocation)
        .where(
            PaymentAllocation.workspace_id == workspace_id,
            PaymentAllocation.appointment_id == from_appointment_id,
        )
        .values(appointment_id=to_appointment_id)
    )
    db.execute(
        update(PaymentTransaction)
        .where(
            PaymentTransaction.workspace_id == workspace_id,
            PaymentTransaction.appointment_id == from_appointment_id,
        )
        .values(appointment_id=to_appointment_id)
    )


def seed_payment_ledger_from_appointment_snapshot(
    db: Session,
    *,
    appointment: Appointment,
    source_key: str,
    payment_external_reference: str | None = None,
    refund_amount_minor: int | None = None,
    refund_reason: str | None = None,
    refunded_at: datetime | None = None,
) -> None:
    rows = _ledger_rows(
        db,
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        for_update=False,
    )
    if rows:
        sync_appointment_payment_snapshot(appointment, list(rows))
        return

    if getattr(appointment, "billing_context", "standard") in {
        "package_prepaid",
        "pulse_prepaid",
    }:
        sync_appointment_payment_snapshot(appointment, [], due_minor=0)
        return

    amount = int(appointment.amount_paid_minor or 0)
    status = appointment.payment_status
    if amount <= 0 or status not in {"paid", "partial", "refunded"}:
        return

    explicit_refund = int(refund_amount_minor or 0)
    if explicit_refund < 0:
        raise PaymentOperationError("Imported refund amount cannot be negative.")
    refund_amount = (
        amount if status == "refunded" and explicit_refund == 0 else explicit_refund
    )
    if refund_amount > amount:
        raise PaymentOperationError(
            "Imported refund amount cannot exceed the direct payment amount."
        )

    method = (
        appointment.payment_method
        if appointment.payment_method in PAYMENT_METHODS
        else "unknown"
    )
    now = datetime.now(UTC)
    payment = PaymentTransaction(
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        origin_appointment_id=appointment.id,
        patient_id=appointment.patient_id,
        created_by_user_id=None,
        reference_transaction_id=None,
        transaction_type="payment",
        amount_minor=amount,
        currency=appointment.currency,
        payment_method=method,
        source="integration",
        external_reference=payment_external_reference,
        reason=None,
        idempotency_key=f"import-payment:{source_key}"[:128],
        created_at=now,
    )
    db.add(payment)
    db.flush()
    _add_single_appointment_allocation(
        db,
        transaction=payment,
        appointment_id=appointment.id,
        amount_minor=amount,
    )
    rows.append(
        AppointmentLedgerEntry(
            transaction=payment,
            allocated_amount_minor=amount,
        )
    )

    if refund_amount > 0:
        refund = PaymentTransaction(
            workspace_id=appointment.workspace_id,
            appointment_id=appointment.id,
            origin_appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            created_by_user_id=None,
            reference_transaction_id=payment.id,
            transaction_type="refund",
            amount_minor=refund_amount,
            currency=appointment.currency,
            payment_method=method,
            source="integration",
            external_reference=None,
            reason=refund_reason or "Imported appointment refund",
            idempotency_key=f"import-refund:{source_key}"[:128],
            created_at=refunded_at or now,
        )
        db.add(refund)
        db.flush()
        _add_single_appointment_allocation(
            db,
            transaction=refund,
            appointment_id=appointment.id,
            amount_minor=refund_amount,
        )
        rows.append(
            AppointmentLedgerEntry(
                transaction=refund,
                allocated_amount_minor=refund_amount,
            )
        )
    sync_appointment_payment_snapshot(appointment, list(rows))
