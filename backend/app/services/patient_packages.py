from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    require_tia_workspace_domain_write,
)
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PAYMENT_METHODS, PaymentAllocation, PaymentTransaction
from app.models.service import Service
from app.schemas.patient_packages import PatientPackageRead
from app.services.activity import ActivityActorType, record_activity_event
from app.services.payments import refresh_appointment_payment_snapshots


class PackageOperationError(ValueError):
    pass


class PackageNotFound(PackageOperationError):
    pass


def _effective_status(package: PatientPackage, *, on_date: date | None = None) -> str:
    on_date = on_date or datetime.now(UTC).date()
    if package.status == "cancelled":
        return "cancelled"
    if package.expires_at is not None and package.expires_at < on_date:
        return "expired"
    return package.status


def _usage_totals(db: Session, *, workspace_id: UUID, package_id: UUID) -> tuple[int, int]:
    rows = db.execute(
        select(PackageUsage.status, func.coalesce(func.sum(PackageUsage.sessions_used), 0))
        .where(
            PackageUsage.workspace_id == workspace_id,
            PackageUsage.patient_package_id == package_id,
            PackageUsage.status.in_(("reserved", "consumed")),
        )
        .group_by(PackageUsage.status)
    ).all()
    values = {str(status): int(total or 0) for status, total in rows}
    return values.get("reserved", 0), values.get("consumed", 0)


def _settlement_standalone_price_minor(
    db: Session,
    package: PatientPackage,
    *,
    persist_fallback: bool = False,
) -> int | None:
    """Resolve the standalone session price used by package cancellation.

    Normal packages keep the immutable purchase-time snapshot. Migrated packages
    created before that snapshot was populated fall back to the service price already
    configured in Linka. The fallback is only allowed when the imported opening
    balance and original total session count are known.
    """
    snapshot = package.standalone_session_price_minor_at_purchase
    if snapshot is not None:
        return int(snapshot)

    if package.opening_sessions_remaining is None or not package.sessions_total_known:
        return None

    service_price = db.scalar(
        select(Service.price_minor).where(
            Service.workspace_id == package.workspace_id,
            Service.id == package.service_id,
        )
    )
    if service_price is None:
        return None

    resolved = int(service_price)
    if persist_fallback:
        package.standalone_session_price_minor_at_purchase = resolved
    return resolved


def _default_cancellation_charge_minor(
    db: Session,
    package: PatientPackage,
    *,
    consumed_sessions: int,
) -> int | None:
    consumed_sessions = max(0, int(consumed_sessions))
    if consumed_sessions == 0:
        return 0
    unit_price = _settlement_standalone_price_minor(db, package)
    if unit_price is None:
        return None
    return consumed_sessions * unit_price


def package_read(
    db: Session,
    package: PatientPackage,
    *,
    on_date: date | None = None,
    include_financials: bool = False,
) -> PatientPackageRead:
    reserved, consumed = _usage_totals(
        db, workspace_id=package.workspace_id, package_id=package.id
    )
    opening_balance = (
        int(package.opening_sessions_remaining)
        if package.opening_sessions_remaining is not None
        else int(package.sessions_purchased)
    )
    remaining = max(0, opening_balance - reserved - consumed)
    cancellation_consumed = consumed
    cancellation_default_charge: int | None
    if package.opening_sessions_remaining is not None:
        if package.sessions_total_known:
            cancellation_consumed += max(
                0,
                int(package.sessions_purchased) - int(package.opening_sessions_remaining),
            )
        else:
            cancellation_default_charge = None
    cancellation_default_charge = _default_cancellation_charge_minor(
        db,
        package,
        consumed_sessions=cancellation_consumed,
    )
    effective = _effective_status(package, on_date=on_date)
    if effective == "active" and remaining == 0:
        effective = "exhausted"
    amount_paid_minor = 0
    amount_refunded_minor = 0
    balance_due_minor = 0
    if include_financials:
        payments, refunds = _package_financial_rows(
            db, workspace_id=package.workspace_id, package=package, for_update=False
        )
        amount_paid_minor = sum(int(row.amount_minor) for row in payments)
        amount_refunded_minor = sum(int(row.amount_minor) for row in refunds)
        balance_due_minor = (
            max(int(package.sale_price_minor) - amount_paid_minor, 0)
            if effective == "active"
            else 0
        )
    return PatientPackageRead(
        id=package.id,
        workspace_id=package.workspace_id,
        patient_id=package.patient_id,
        service_id=package.service_id,
        purchase_transaction_id=package.purchase_transaction_id,
        package_offer_id=package.package_offer_id,
        external_id=package.external_id,
        name=package.name,
        sessions_purchased=package.sessions_purchased,
        sessions_reserved=reserved,
        sessions_consumed=consumed,
        sessions_remaining=remaining,
        sale_price_minor=package.sale_price_minor,
        amount_paid_minor=amount_paid_minor,
        amount_refunded_minor=amount_refunded_minor,
        balance_due_minor=balance_due_minor,
        cancellation_consumed_sessions=cancellation_consumed,
        cancellation_default_charge_minor=cancellation_default_charge,
        standalone_session_price_minor_at_purchase=(
            package.standalone_session_price_minor_at_purchase
        ),
        laser_device_key=package.laser_device_key,
        laser_device_name=package.laser_device_name,
        currency=package.currency,
        purchased_at=package.purchased_at,
        expires_at=package.expires_at,
        status=package.status,
        effective_status=effective,
        source=package.source,
        created_at=package.created_at,
        updated_at=package.updated_at,
    )


def list_patient_packages(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    service_id: UUID | None = None,
    usable_only: bool = False,
    on_date: date | None = None,
    include_financials: bool = False,
) -> list[PatientPackageRead]:
    query = select(PatientPackage).where(
        PatientPackage.workspace_id == workspace_id,
        PatientPackage.patient_id == patient_id,
    )
    if service_id is not None:
        query = query.where(PatientPackage.service_id == service_id)
    packages = list(db.scalars(query.order_by(PatientPackage.purchased_at.desc())).all())
    reads = [
        package_read(
            db,
            package,
            on_date=on_date,
            include_financials=include_financials,
        )
        for package in packages
    ]
    if usable_only:
        reads = [item for item in reads if item.effective_status == "active" and item.sessions_remaining > 0]
    return reads


def create_patient_package(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    service_id: UUID,
    name: str,
    sessions_purchased: int,
    sale_price_minor: int,
    amount_paid_minor: int | None = None,
    payment_method: str,
    created_by_user_id: UUID | None,
    purchased_at: datetime | None = None,
    expires_at: date | None = None,
    external_reference: str | None = None,
    external_id: str | None = None,
    idempotency_key: str | None = None,
    actor_type: ActivityActorType = "staff",
    package_offer_id: UUID | None = None,
    origin_appointment_id: UUID | None = None,
    laser_device_key: str | None = None,
    laser_device_name: str | None = None,
    standalone_session_price_minor_at_purchase: int | None = None,
) -> PatientPackage:
    if sessions_purchased <= 0:
        raise PackageOperationError("Package sessions must be positive.")
    if sale_price_minor < 0:
        raise PackageOperationError("Package price cannot be negative.")
    effective_initial_payment = sale_price_minor if amount_paid_minor is None else amount_paid_minor
    if effective_initial_payment < 0 or effective_initial_payment > sale_price_minor:
        raise PackageOperationError("Initial package payment must be between zero and the package price.")
    if effective_initial_payment > 0 and (
        payment_method not in PAYMENT_METHODS or payment_method == "unknown"
    ):
        raise PackageOperationError("A paid package requires a supported payment method.")
    purchased_at = purchased_at or datetime.now(UTC)
    if purchased_at.tzinfo is None or purchased_at.utcoffset() is None:
        raise PackageOperationError("Package purchase time must include a timezone offset.")
    purchased_at = purchased_at.astimezone(UTC)
    if expires_at is not None and expires_at < purchased_at.date():
        raise PackageOperationError("Package expiry cannot be before the purchase date.")

    if idempotency_key:
        existing = db.scalar(
            select(PatientPackage).where(
                PatientPackage.workspace_id == workspace_id,
                PatientPackage.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.patient_id != patient_id
                or existing.service_id != service_id
                or existing.sessions_purchased != sessions_purchased
                or existing.sale_price_minor != sale_price_minor
            ):
                raise PackageOperationError("Idempotency key was already used for another package sale.")
            return existing

    patient = db.scalar(
        select(Patient)
        .where(Patient.workspace_id == workspace_id, Patient.id == patient_id)
        .with_for_update()
    )
    service = db.scalar(
        select(Service).where(Service.workspace_id == workspace_id, Service.id == service_id)
    )
    if patient is None:
        raise PackageNotFound("Patient not found.")
    if service is None or not service.is_active:
        raise PackageNotFound("Service not found or inactive.")

    standalone_snapshot = (
        int(standalone_session_price_minor_at_purchase)
        if standalone_session_price_minor_at_purchase is not None
        else int(service.price_minor)
    )
    transaction = None
    if effective_initial_payment > 0:
        try:
            require_tia_workspace_domain_write(db, workspace_id=workspace_id, domain="payments")
        except ClinicIntegrationAuthorityError as exc:
            raise PackageOperationError(str(exc)) from exc
        transaction = PaymentTransaction(
            workspace_id=workspace_id,
            appointment_id=None,
            origin_appointment_id=None,
            patient_id=patient_id,
            created_by_user_id=created_by_user_id,
            reference_transaction_id=None,
            transaction_type="payment",
            amount_minor=effective_initial_payment,
            currency="EGP",
            payment_method=payment_method,
            source="staff",
            external_reference=external_reference,
            reason="Prepaid package purchase",
            idempotency_key=(f"package-payment:{idempotency_key}"[:128] if idempotency_key else None),
            created_at=purchased_at,
        )
        db.add(transaction)
        db.flush()

    package = PatientPackage(
        workspace_id=workspace_id,
        patient_id=patient_id,
        service_id=service_id,
        purchase_transaction_id=transaction.id if transaction else None,
        package_offer_id=package_offer_id,
        origin_appointment_id=origin_appointment_id,
        created_by_user_id=created_by_user_id,
        external_id=external_id,
        name=name.strip(),
        sessions_purchased=sessions_purchased,
        sale_price_minor=sale_price_minor,
        standalone_session_price_minor_at_purchase=standalone_snapshot,
        laser_device_key=laser_device_key,
        laser_device_name=laser_device_name,
        currency="EGP",
        purchased_at=purchased_at,
        expires_at=expires_at,
        status="active",
        source="staff",
        idempotency_key=idempotency_key,
    )
    db.add(package)
    db.flush()
    if transaction is not None:
        transaction.patient_package_id = package.id
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type=actor_type,
        actor_user_id=created_by_user_id,
        action="package.created",
        entity_type="patient_package",
        entity_id=package.id,
        summary="Prepaid package created",
        metadata={
            "service_id": service_id,
            "package_offer_id": package_offer_id,
            "laser_device_key": laser_device_key,
            "sessions_purchased": sessions_purchased,
            "sale_price_minor": sale_price_minor,
            "amount_paid_minor": effective_initial_payment,
            "standalone_session_price_minor_at_purchase": standalone_snapshot,
            "has_payment": transaction is not None,
        },
    )
    return package


def _package_financial_rows(
    db: Session,
    *,
    workspace_id: UUID,
    package: PatientPackage,
    for_update: bool = False,
) -> tuple[list[PaymentTransaction], list[PaymentTransaction]]:
    payment_filter = or_(
        PaymentTransaction.patient_package_id == package.id,
        (
            PaymentTransaction.id == package.purchase_transaction_id
            if package.purchase_transaction_id is not None
            else False
        ),
    )
    payment_stmt = (
        select(PaymentTransaction)
        .where(
            PaymentTransaction.workspace_id == workspace_id,
            PaymentTransaction.transaction_type == "payment",
            payment_filter,
        )
        .order_by(PaymentTransaction.created_at.asc(), PaymentTransaction.id.asc())
    )
    if for_update:
        payment_stmt = payment_stmt.with_for_update()
    payments = list(db.scalars(payment_stmt).all())
    payment_ids = [row.id for row in payments]
    if not payment_ids:
        return payments, []
    refund_stmt = select(PaymentTransaction).where(
        PaymentTransaction.workspace_id == workspace_id,
        PaymentTransaction.transaction_type == "refund",
        or_(
            PaymentTransaction.patient_package_id == package.id,
            PaymentTransaction.reference_transaction_id.in_(payment_ids),
        ),
    )
    if for_update:
        refund_stmt = refund_stmt.with_for_update()
    refunds = list(db.scalars(refund_stmt).all())
    return payments, refunds


def record_package_payment(
    db: Session,
    *,
    workspace_id: UUID,
    package_id: UUID,
    amount_minor: int,
    payment_method: str,
    created_by_user_id: UUID | None,
    external_reference: str | None = None,
    idempotency_key: str | None = None,
    actor_type: ActivityActorType = "staff",
) -> PaymentTransaction:
    if amount_minor <= 0:
        raise PackageOperationError("Package payment amount must be positive.")
    if payment_method not in PAYMENT_METHODS or payment_method == "unknown":
        raise PackageOperationError("A package payment requires a supported payment method.")
    try:
        require_tia_workspace_domain_write(db, workspace_id=workspace_id, domain="payments")
    except ClinicIntegrationAuthorityError as exc:
        raise PackageOperationError(str(exc)) from exc

    if idempotency_key:
        existing = db.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.workspace_id == workspace_id,
                PaymentTransaction.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.transaction_type != "payment"
                or existing.patient_package_id != package_id
                or existing.amount_minor != amount_minor
            ):
                raise PackageOperationError("Idempotency key was already used for another package payment.")
            return existing

    package = _locked_package(db, workspace_id=workspace_id, package_id=package_id)
    if _effective_status(package) != "active":
        raise PackageOperationError("Payments can only be added to an active package.")
    payments, _refunds = _package_financial_rows(
        db, workspace_id=workspace_id, package=package, for_update=True
    )
    gross_collected = sum(int(row.amount_minor) for row in payments)
    remaining_due = max(int(package.sale_price_minor) - gross_collected, 0)
    if amount_minor > remaining_due:
        raise PackageOperationError(
            f"Package payment exceeds remaining package balance of {remaining_due} minor units."
        )
    transaction = PaymentTransaction(
        workspace_id=workspace_id,
        appointment_id=package.origin_appointment_id,
        origin_appointment_id=package.origin_appointment_id,
        patient_id=package.patient_id,
        created_by_user_id=created_by_user_id,
        reference_transaction_id=None,
        patient_package_id=package.id,
        transaction_type="payment",
        amount_minor=amount_minor,
        currency=package.currency,
        payment_method=payment_method,
        source="staff",
        external_reference=external_reference,
        reason="Prepaid package payment",
        idempotency_key=idempotency_key,
        created_at=datetime.now(UTC),
    )
    db.add(transaction)
    db.flush()
    if package.origin_appointment_id is not None:
        db.add(
            PaymentAllocation(
                workspace_id=workspace_id,
                transaction_id=transaction.id,
                appointment_id=package.origin_appointment_id,
                amount_minor=amount_minor,
                created_at=transaction.created_at,
            )
        )
        db.flush()
        refresh_appointment_payment_snapshots(
            db,
            workspace_id=workspace_id,
            appointment_ids={package.origin_appointment_id},
        )
    if package.purchase_transaction_id is None:
        package.purchase_transaction_id = transaction.id
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type=actor_type,
        actor_user_id=created_by_user_id,
        action="package.payment_recorded",
        entity_type="patient_package",
        entity_id=package.id,
        summary="Package payment recorded",
        metadata={"amount_minor": amount_minor, "payment_method": payment_method},
    )
    return transaction


def cancel_patient_package_with_refund(
    db: Session,
    *,
    workspace_id: UUID,
    package_id: UUID,
    reason: str,
    created_by_user_id: UUID,
    standalone_session_price_minor_at_purchase: int | None = None,
    settlement_target_minor: int | None = None,
    payment_method: str = "cash",
    idempotency_key: str | None = None,
) -> tuple[PatientPackage, int, int, int, int, int, int, list[PaymentTransaction]]:
    """Cancel a package and settle the consumed value before refunding unused value.

    Reserved sessions are released. New packages use the standalone service price
    snapshot captured at purchase. Historical migrated packages created before that
    snapshot existed use the service price configured in Linka, and that fallback is
    persisted when cancellation is executed. Refunds are created against the original
    package payments.
    """
    reason = reason.strip()
    if not reason:
        raise PackageOperationError("Refund reason cannot be empty.")
    try:
        require_tia_workspace_domain_write(db, workspace_id=workspace_id, domain="payments")
    except ClinicIntegrationAuthorityError as exc:
        raise PackageOperationError(str(exc)) from exc

    package = _locked_package(db, workspace_id=workspace_id, package_id=package_id)
    reserved, consumed = _usage_totals(db, workspace_id=workspace_id, package_id=package.id)
    if package.opening_sessions_remaining is not None:
        if not package.sessions_total_known:
            raise PackageOperationError(
                "This migrated package does not include the original total session count, "
                "so a safe package refund cannot be calculated automatically."
            )
        consumed += max(
            0,
            int(package.sessions_purchased) - int(package.opening_sessions_remaining),
        )
    if package.standalone_session_price_minor_at_purchase is None:
        if standalone_session_price_minor_at_purchase is not None:
            package.standalone_session_price_minor_at_purchase = (
                standalone_session_price_minor_at_purchase
            )
    elif (
        standalone_session_price_minor_at_purchase is not None
        and standalone_session_price_minor_at_purchase
        != package.standalone_session_price_minor_at_purchase
    ):
        raise PackageOperationError(
            "Standalone session price at purchase is already fixed for this package."
        )

    if package.standalone_session_price_minor_at_purchase is None:
        _settlement_standalone_price_minor(
            db,
            package,
            persist_fallback=True,
        )

    consumed_value_minor = _default_cancellation_charge_minor(
        db,
        package,
        consumed_sessions=consumed,
    )
    if consumed_value_minor is None:
        raise PackageOperationError(
            "The package does not include enough pricing or original-session data "
            "to calculate a safe cancellation settlement automatically."
        )

    payments, refunds = _package_financial_rows(
        db, workspace_id=workspace_id, package=package, for_update=True
    )
    collected_minor = sum(int(row.amount_minor) for row in payments)
    previously_refunded_minor = sum(int(row.amount_minor) for row in refunds)
    settlement_target = (
        consumed_value_minor
        if settlement_target_minor is None
        else int(settlement_target_minor)
    )
    net_collected_before = max(collected_minor - previously_refunded_minor, 0)
    collected_now_minor = 0
    if settlement_target > net_collected_before:
        if payment_method not in PAYMENT_METHODS or payment_method == "unknown":
            raise PackageOperationError(
                "A supported payment method is required to collect the package settlement."
            )
        collected_now_minor = settlement_target - net_collected_before
        settlement_key = (
            f"package-settlement:{idempotency_key}"[:128]
            if idempotency_key
            else None
        )
        settlement_payment = None
        if settlement_key:
            settlement_payment = db.scalar(
                select(PaymentTransaction).where(
                    PaymentTransaction.workspace_id == workspace_id,
                    PaymentTransaction.idempotency_key == settlement_key,
                )
            )
            if settlement_payment is not None and (
                settlement_payment.transaction_type != "payment"
                or settlement_payment.patient_package_id != package.id
            ):
                raise PackageOperationError(
                    "Idempotency key was already used for another package settlement."
                )
        if settlement_payment is None:
            settlement_payment = PaymentTransaction(
                workspace_id=workspace_id,
                appointment_id=None,
                origin_appointment_id=package.origin_appointment_id,
                patient_id=package.patient_id,
                created_by_user_id=created_by_user_id,
                reference_transaction_id=None,
                patient_package_id=package.id,
                transaction_type="payment",
                amount_minor=collected_now_minor,
                currency=package.currency,
                payment_method=payment_method,
                source="staff",
                external_reference=None,
                reason=f"Package cancellation settlement: {reason}"[:500],
                idempotency_key=settlement_key,
                created_at=datetime.now(UTC),
            )
            db.add(settlement_payment)
            db.flush()
            if package.origin_appointment_id is not None:
                db.add(
                    PaymentAllocation(
                        workspace_id=workspace_id,
                        transaction_id=settlement_payment.id,
                        appointment_id=package.origin_appointment_id,
                        amount_minor=collected_now_minor,
                        created_at=settlement_payment.created_at,
                    )
                )
                db.flush()
        else:
            collected_now_minor = int(settlement_payment.amount_minor)
        collected_minor += collected_now_minor
        payments.append(settlement_payment)

    net_collected = max(collected_minor - previously_refunded_minor, 0)
    refundable_minor = max(net_collected - settlement_target, 0)

    refunds_by_payment: dict[UUID, int] = {}
    for row in refunds:
        if row.reference_transaction_id is not None:
            refunds_by_payment[row.reference_transaction_id] = (
                refunds_by_payment.get(row.reference_transaction_id, 0) + int(row.amount_minor)
            )

    created_refunds: list[PaymentTransaction] = []
    remaining_to_refund = refundable_minor
    for index, payment in enumerate(reversed(payments)):
        if remaining_to_refund <= 0:
            break
        available_on_payment = max(
            int(payment.amount_minor) - refunds_by_payment.get(payment.id, 0), 0
        )
        amount = min(available_on_payment, remaining_to_refund)
        if amount <= 0:
            continue
        refund_key = (
            f"package-refund:{idempotency_key}:{index}"[:128]
            if idempotency_key
            else None
        )
        if refund_key:
            existing = db.scalar(
                select(PaymentTransaction).where(
                    PaymentTransaction.workspace_id == workspace_id,
                    PaymentTransaction.idempotency_key == refund_key,
                )
            )
            if existing is not None:
                if (
                    existing.transaction_type != "refund"
                    or existing.patient_package_id != package.id
                    or existing.reference_transaction_id != payment.id
                ):
                    raise PackageOperationError(
                        "Idempotency key was already used for another package refund."
                    )
                remaining_to_refund -= int(existing.amount_minor)
                created_refunds.append(existing)
                continue
        refund = PaymentTransaction(
            workspace_id=workspace_id,
            appointment_id=None,
            origin_appointment_id=None,
            patient_id=package.patient_id,
            created_by_user_id=created_by_user_id,
            reference_transaction_id=payment.id,
            patient_package_id=package.id,
            transaction_type="refund",
            amount_minor=amount,
            currency=package.currency,
            payment_method=payment.payment_method,
            source="staff",
            external_reference=None,
            reason=reason[:500],
            idempotency_key=refund_key,
            created_at=datetime.now(UTC),
        )
        db.add(refund)
        db.flush()
        if package.origin_appointment_id is not None:
            db.add(
                PaymentAllocation(
                    workspace_id=workspace_id,
                    transaction_id=refund.id,
                    appointment_id=package.origin_appointment_id,
                    amount_minor=amount,
                    created_at=refund.created_at,
                )
            )
            db.flush()
        created_refunds.append(refund)
        remaining_to_refund -= amount

    if remaining_to_refund > 0:
        raise PackageOperationError("Package refund could not be reconciled to original payments.")

    usages = list(
        db.scalars(
            select(PackageUsage)
            .where(
                PackageUsage.workspace_id == workspace_id,
                PackageUsage.patient_package_id == package.id,
            )
            .with_for_update()
        ).all()
    )
    linked_appointment_ids = {usage.appointment_id for usage in usages}
    additional_line_ids = {
        usage.appointment_additional_service_id
        for usage in usages
        if usage.appointment_additional_service_id is not None
    }
    for usage in usages:
        if usage.status == "reserved":
            usage.status = "released"
            usage.used_at = None

    package.status = "cancelled"

    if linked_appointment_ids:
        db.execute(
            update(Appointment)
            .where(
                Appointment.workspace_id == workspace_id,
                Appointment.id.in_(linked_appointment_ids),
                Appointment.patient_package_id == package.id,
                Appointment.billing_context == "package_prepaid",
            )
            .values(
                patient_package_id=None,
                billing_context="standard",
                package_external_id=None,
            )
        )
    if additional_line_ids:
        db.execute(
            update(AppointmentAdditionalService)
            .where(
                AppointmentAdditionalService.workspace_id == workspace_id,
                AppointmentAdditionalService.id.in_(additional_line_ids),
                AppointmentAdditionalService.patient_package_id == package.id,
            )
            .values(
                patient_package_id=None,
                billing_context="standard",
            )
        )
    if linked_appointment_ids:
        refresh_appointment_payment_snapshots(
            db,
            workspace_id=workspace_id,
            appointment_ids=linked_appointment_ids,
        )
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=created_by_user_id,
        action="package.cancelled_refund",
        entity_type="patient_package",
        entity_id=package.id,
        summary="Package cancelled and unused value refunded",
        metadata={
            "consumed_sessions": consumed,
            "released_reservations": reserved,
            "consumed_value_minor": consumed_value_minor,
            "settlement_target_minor": settlement_target,
            "collected_now_minor": collected_now_minor,
            "refunded_now_minor": refundable_minor,
            "previously_refunded_minor": previously_refunded_minor,
        },
    )
    return (
        package,
        collected_minor,
        consumed_value_minor,
        settlement_target,
        previously_refunded_minor,
        collected_now_minor,
        refundable_minor,
        created_refunds,
    )


def _locked_package(
    db: Session,
    *,
    workspace_id: UUID,
    package_id: UUID,
) -> PatientPackage:
    package = db.scalar(
        select(PatientPackage)
        .where(PatientPackage.workspace_id == workspace_id, PatientPackage.id == package_id)
        .with_for_update()
    )
    if package is None:
        raise PackageNotFound("Package not found.")
    return package


def validate_package_for_booking(
    db: Session,
    *,
    workspace_id: UUID,
    package_id: UUID,
    patient_id: UUID,
    service_id: UUID,
    appointment_start_at: datetime,
    sessions: int = 1,
    laser_device_key: str | None = None,
) -> PatientPackage:
    package = _locked_package(db, workspace_id=workspace_id, package_id=package_id)
    if package.patient_id != patient_id:
        raise PackageOperationError("Selected package belongs to another patient.")
    if package.service_id != service_id:
        raise PackageOperationError("Selected package is for a different service.")
    if package.laser_device_key is not None and package.laser_device_key != laser_device_key:
        raise PackageOperationError("Selected package is for a different laser device.")
    if _effective_status(package, on_date=appointment_start_at.date()) != "active":
        raise PackageOperationError("Selected package is not active on the appointment date.")
    if appointment_start_at.astimezone(UTC) < package.purchased_at.astimezone(UTC):
        raise PackageOperationError("Appointment cannot use a package before its purchase time.")
    reserved, consumed = _usage_totals(db, workspace_id=workspace_id, package_id=package.id)
    opening_sessions_remaining = getattr(package, "opening_sessions_remaining", None)
    opening_balance = (
        int(opening_sessions_remaining)
        if opening_sessions_remaining is not None
        else int(package.sessions_purchased)
    )
    remaining = opening_balance - reserved - consumed
    if remaining < sessions:
        raise PackageOperationError(
            f"Selected package has only {max(0, remaining)} session(s) remaining."
        )
    return package


def reserve_package_usage(
    db: Session,
    *,
    appointment: Appointment,
    package: PatientPackage,
    sessions: int = 1,
    actor_type: ActivityActorType = "staff",
    actor_user_id: UUID | None = None,
) -> PackageUsage:
    if package.laser_device_key is not None and package.laser_device_key != appointment.laser_device_key:
        raise PackageOperationError("Package laser device does not match the appointment device.")
    existing = db.scalar(
        select(PackageUsage).where(
            PackageUsage.workspace_id == appointment.workspace_id,
            PackageUsage.appointment_id == appointment.id,
            PackageUsage.appointment_additional_service_id.is_(None),
        )
    )
    if existing is not None:
        if existing.patient_package_id != package.id:
            raise PackageOperationError("Appointment is already linked to another package.")
        if existing.status == "released":
            existing.status = "reserved"
            existing.used_at = None
        return existing
    usage = PackageUsage(
        workspace_id=appointment.workspace_id,
        patient_package_id=package.id,
        appointment_id=appointment.id,
        appointment_additional_service_id=None,
        sessions_used=sessions,
        status="reserved",
        used_at=None,
    )
    appointment.patient_package_id = package.id
    appointment.billing_context = "package_prepaid"
    appointment.package_external_id = package.external_id or str(package.id)
    appointment.payment_status = "paid"
    appointment.amount_paid_minor = None
    appointment.payment_method = "unknown"
    db.add(usage)
    db.flush()
    record_activity_event(
        db,
        workspace_id=appointment.workspace_id,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="package.session_reserved",
        entity_type="patient_package",
        entity_id=package.id,
        summary="Package session reserved",
        metadata={"appointment_id": appointment.id, "sessions": sessions},
    )
    return usage



def reserve_additional_service_package_usage(
    db: Session,
    *,
    appointment: Appointment,
    line: AppointmentAdditionalService,
    package: PatientPackage,
    sessions: int = 1,
    actor_type: ActivityActorType = "staff",
    actor_user_id: UUID | None = None,
) -> PackageUsage:
    if package.service_id != line.service_id:
        raise PackageOperationError("Package service does not match the additional service.")
    if package.laser_device_key is not None and package.laser_device_key != line.laser_device_key:
        raise PackageOperationError("Package laser device does not match the additional service device.")
    existing = db.scalar(
        select(PackageUsage).where(
            PackageUsage.workspace_id == appointment.workspace_id,
            PackageUsage.appointment_additional_service_id == line.id,
        )
    )
    if existing is not None:
        if existing.patient_package_id != package.id:
            raise PackageOperationError("Additional service is already linked to another package.")
        if existing.status == "released":
            existing.status = "reserved"
            existing.used_at = None
        return existing
    usage = PackageUsage(
        workspace_id=appointment.workspace_id,
        patient_package_id=package.id,
        appointment_id=appointment.id,
        appointment_additional_service_id=line.id,
        sessions_used=sessions,
        status="reserved",
        used_at=None,
    )
    line.patient_package_id = package.id
    line.billing_context = "package_prepaid"
    db.add(usage)
    db.flush()
    record_activity_event(
        db,
        workspace_id=appointment.workspace_id,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="package.additional_service_session_reserved",
        entity_type="patient_package",
        entity_id=package.id,
        summary="Additional visit service reserved from package",
        metadata={
            "appointment_id": appointment.id,
            "appointment_additional_service_id": line.id,
            "sessions": sessions,
        },
    )
    return usage


def consume_visit_package_usages(
    db: Session,
    *,
    appointment: Appointment,
    used_at: datetime | None = None,
    actor_type: ActivityActorType = "staff",
    actor_user_id: UUID | None = None,
) -> list[PackageUsage]:
    usages = list(
        db.scalars(
            select(PackageUsage)
            .where(
                PackageUsage.workspace_id == appointment.workspace_id,
                PackageUsage.appointment_id == appointment.id,
            )
            .with_for_update()
        ).all()
    )
    consumed_at = (used_at or datetime.now(UTC)).astimezone(UTC)
    for usage in usages:
        if usage.status == "consumed":
            continue
        if usage.status == "released":
            continue
        usage.status = "consumed"
        usage.used_at = consumed_at
        record_activity_event(
            db,
            workspace_id=appointment.workspace_id,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            action="package.session_consumed",
            entity_type="patient_package",
            entity_id=usage.patient_package_id,
            summary="Package session consumed",
            metadata={
                "appointment_id": appointment.id,
                "appointment_additional_service_id": usage.appointment_additional_service_id,
                "sessions": usage.sessions_used,
            },
        )
    return usages


def release_visit_package_usages(
    db: Session,
    *,
    appointment: Appointment,
    actor_type: ActivityActorType = "staff",
    actor_user_id: UUID | None = None,
    reason: str,
) -> list[PackageUsage]:
    usages = list(
        db.scalars(
            select(PackageUsage)
            .where(
                PackageUsage.workspace_id == appointment.workspace_id,
                PackageUsage.appointment_id == appointment.id,
            )
            .with_for_update()
        ).all()
    )
    for usage in usages:
        if usage.status == "released":
            continue
        if usage.status == "consumed":
            raise PackageOperationError("Consumed package entitlement cannot be released.")
        usage.status = "released"
        usage.used_at = None
        record_activity_event(
            db,
            workspace_id=appointment.workspace_id,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            action="package.session_released",
            entity_type="patient_package",
            entity_id=usage.patient_package_id,
            summary="Package session released",
            metadata={
                "appointment_id": appointment.id,
                "appointment_additional_service_id": usage.appointment_additional_service_id,
                "reason": reason[:80],
            },
        )
    return usages


def consume_package_usage(
    db: Session,
    *,
    appointment: Appointment,
    used_at: datetime | None = None,
    actor_type: ActivityActorType = "staff",
    actor_user_id: UUID | None = None,
) -> PackageUsage | None:
    usage = db.scalar(
        select(PackageUsage)
        .where(
            PackageUsage.workspace_id == appointment.workspace_id,
            PackageUsage.appointment_id == appointment.id,
            PackageUsage.appointment_additional_service_id.is_(None),
        )
        .with_for_update()
    )
    if usage is None:
        return None
    if usage.status == "consumed":
        return usage
    if usage.status == "released":
        raise PackageOperationError("Released package entitlement cannot be consumed.")
    usage.status = "consumed"
    usage.used_at = (used_at or datetime.now(UTC)).astimezone(UTC)
    record_activity_event(
        db,
        workspace_id=appointment.workspace_id,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="package.session_consumed",
        entity_type="patient_package",
        entity_id=usage.patient_package_id,
        summary="Package session consumed",
        metadata={"appointment_id": appointment.id, "sessions": usage.sessions_used},
    )
    return usage


def release_package_usage(
    db: Session,
    *,
    appointment: Appointment,
    actor_type: ActivityActorType = "staff",
    actor_user_id: UUID | None = None,
    reason: str,
) -> PackageUsage | None:
    usage = db.scalar(
        select(PackageUsage)
        .where(
            PackageUsage.workspace_id == appointment.workspace_id,
            PackageUsage.appointment_id == appointment.id,
            PackageUsage.appointment_additional_service_id.is_(None),
        )
        .with_for_update()
    )
    if usage is None or usage.status == "released":
        return usage
    if usage.status == "consumed":
        raise PackageOperationError("Consumed package entitlement cannot be released.")
    usage.status = "released"
    usage.used_at = None
    record_activity_event(
        db,
        workspace_id=appointment.workspace_id,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="package.session_released",
        entity_type="patient_package",
        entity_id=usage.patient_package_id,
        summary="Package session released",
        metadata={"appointment_id": appointment.id, "reason": reason[:80]},
    )
    return usage


def transfer_package_usage(
    db: Session,
    *,
    from_appointment: Appointment,
    to_appointment: Appointment,
) -> PackageUsage | None:
    usage = db.scalar(
        select(PackageUsage)
        .where(
            PackageUsage.workspace_id == from_appointment.workspace_id,
            PackageUsage.appointment_id == from_appointment.id,
            PackageUsage.appointment_additional_service_id.is_(None),
        )
        .with_for_update()
    )
    if usage is None:
        return None
    if usage.status != "reserved":
        raise PackageOperationError("Only a reserved package session can be rescheduled.")
    package = _locked_package(
        db,
        workspace_id=from_appointment.workspace_id,
        package_id=usage.patient_package_id,
    )
    if package.service_id != to_appointment.service_id:
        raise PackageOperationError("Package service does not match the rescheduled appointment.")
    if package.laser_device_key is not None and package.laser_device_key != to_appointment.laser_device_key:
        raise PackageOperationError("Package laser device does not match the rescheduled appointment.")
    usage.appointment_id = to_appointment.id
    to_appointment.patient_package_id = from_appointment.patient_package_id
    to_appointment.billing_context = from_appointment.billing_context
    to_appointment.package_external_id = from_appointment.package_external_id
    to_appointment.payment_status = "paid"
    to_appointment.amount_paid_minor = None
    to_appointment.payment_method = "unknown"
    return usage