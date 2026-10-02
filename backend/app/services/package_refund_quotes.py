from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.patient_package import PatientPackage
from app.services.patient_packages import (
    _default_cancellation_charge_minor,
    _package_financial_rows,
    _usage_totals,
)


class PackageRefundQuoteError(ValueError):
    pass


@dataclass(frozen=True)
class PackageRefundQuoteRead:
    package_id: UUID
    package_name: str
    service_id: UUID
    laser_device_key: str | None
    currency: str
    consumed_sessions: int
    reserved_sessions: int
    sessions_remaining: int
    collected_minor: int
    previously_refunded_minor: int
    standalone_session_price_minor_at_purchase: int
    consumed_value_minor: int
    refundable_minor: int

    def as_dict(self) -> dict[str, object]:
        return {
            "package_id": str(self.package_id),
            "package_name": self.package_name,
            "service_id": str(self.service_id),
            "laser_device_key": self.laser_device_key,
            "currency": self.currency,
            "consumed_sessions": self.consumed_sessions,
            "reserved_sessions": self.reserved_sessions,
            "sessions_remaining": self.sessions_remaining,
            "collected_minor": self.collected_minor,
            "previously_refunded_minor": self.previously_refunded_minor,
            "standalone_session_price_minor_at_purchase": (
                self.standalone_session_price_minor_at_purchase
            ),
            "consumed_value_minor": self.consumed_value_minor,
            "refundable_minor": self.refundable_minor,
        }


def _quote_one(db: Session, *, package: PatientPackage) -> PackageRefundQuoteRead:
    reserved, ledger_consumed = _usage_totals(
        db,
        workspace_id=package.workspace_id,
        package_id=package.id,
    )
    opening_balance = (
        int(package.opening_sessions_remaining)
        if package.opening_sessions_remaining is not None
        else int(package.sessions_purchased)
    )
    historical_consumed = 0
    if package.opening_sessions_remaining is not None:
        if not package.sessions_total_known:
            raise PackageRefundQuoteError(
                "Migrated package does not include the original total session count."
            )
        historical_consumed = max(
            0,
            int(package.sessions_purchased) - int(package.opening_sessions_remaining),
        )

    consumed = ledger_consumed + historical_consumed
    consumed_value_minor = _default_cancellation_charge_minor(
        package,
        consumed_sessions=consumed,
    )
    if consumed_value_minor is None:
        raise PackageRefundQuoteError(
            "Package pricing data is not sufficient for a safe refund quote."
        )
    unit_price = package.standalone_session_price_minor_at_purchase

    payments, refunds = _package_financial_rows(
        db,
        workspace_id=package.workspace_id,
        package=package,
        for_update=False,
    )
    collected_minor = sum(int(row.amount_minor) for row in payments)
    previously_refunded_minor = sum(int(row.amount_minor) for row in refunds)
    refundable_minor = max(
        collected_minor - consumed_value_minor - previously_refunded_minor,
        0,
    )
    sessions_remaining = max(0, opening_balance - reserved - ledger_consumed)

    return PackageRefundQuoteRead(
        package_id=package.id,
        package_name=package.name,
        service_id=package.service_id,
        laser_device_key=package.laser_device_key,
        currency=package.currency,
        consumed_sessions=consumed,
        reserved_sessions=reserved,
        sessions_remaining=sessions_remaining,
        collected_minor=collected_minor,
        previously_refunded_minor=previously_refunded_minor,
        standalone_session_price_minor_at_purchase=int(unit_price or 0),
        consumed_value_minor=consumed_value_minor,
        refundable_minor=refundable_minor,
    )


def list_patient_package_refund_quotes(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    package_id: UUID | None = None,
    service_id: UUID | None = None,
    laser_device_key: str | None = None,
) -> tuple[list[PackageRefundQuoteRead], list[UUID]]:
    """Return safe, read-only package refund quotes for one patient.

    The calculation mirrors the package cancellation ledger without locking rows,
    changing package state, releasing reservations, or creating refund transactions.
    Packages whose legacy data cannot support a safe quote are returned separately.
    """
    stmt = select(PatientPackage).where(
        PatientPackage.workspace_id == workspace_id,
        PatientPackage.patient_id == patient_id,
    )
    if package_id is not None:
        stmt = stmt.where(PatientPackage.id == package_id)
    if service_id is not None:
        stmt = stmt.where(PatientPackage.service_id == service_id)
    if laser_device_key is not None:
        stmt = stmt.where(PatientPackage.laser_device_key == laser_device_key)

    packages = list(db.scalars(stmt.order_by(PatientPackage.purchased_at.desc())).all())
    quotes: list[PackageRefundQuoteRead] = []
    unsafe: list[UUID] = []
    for package in packages:
        try:
            quotes.append(_quote_one(db, package=package))
        except PackageRefundQuoteError:
            unsafe.append(package.id)
    return quotes, unsafe
