from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    require_tia_workspace_domain_write,
)
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.clinic_inventory import ClinicLaserDevice
from app.models.patient import Patient
from app.models.payment_transaction import PAYMENT_METHODS, PaymentAllocation, PaymentTransaction
from app.models.pulse_billing import (
    AppointmentPulseSettlement,
    PatientPulsePack,
    PulseBillingSettings,
    PulsePackOffer,
    PulseUsage,
)
from app.schemas.pulse_billing import (
    AppointmentPulseSettlementRead,
    PatientPulsePackRead,
    PulseBalanceRead,
    PulseBillingSettingsRead,
    PulsePackOfferRead,
)
from app.services.activity import ActivityActorType, record_activity_event
from app.services.inventory import ensure_legacy_laser_devices
from app.services.payments import refresh_appointment_payment_snapshots


class PulseBillingError(ValueError):
    pass


class PulseBillingNotFound(PulseBillingError):
    pass


def _active_clinic_laser_device(
    db: Session,
    *,
    workspace_id: UUID,
    device_key: str,
) -> ClinicLaserDevice:
    ensure_legacy_laser_devices(db, workspace_id=workspace_id)
    row = db.scalar(
        select(ClinicLaserDevice).where(
            ClinicLaserDevice.workspace_id == workspace_id,
            ClinicLaserDevice.device_key == device_key,
            ClinicLaserDevice.is_active.is_(True),
        )
    )
    if row is None:
        raise PulseBillingError("Laser device is not active for this clinic.")
    return row
def get_pulse_billing_settings(
    db: Session,
    *,
    workspace_id: UUID,
    device_key: str,
) -> PulseBillingSettingsRead:
    device = _active_clinic_laser_device(
        db,
        workspace_id=workspace_id,
        device_key=device_key,
    )
    row = db.scalar(
        select(PulseBillingSettings).where(
            PulseBillingSettings.workspace_id == workspace_id,
            PulseBillingSettings.device_key == device_key,
        )
    )
    return PulseBillingSettingsRead(
        device_key=device_key,
        device_name=device.name,
        overage_price_minor=(int(row.overage_price_minor) if row is not None else None),
        currency=row.currency if row is not None else "EGP",
    )


def list_pulse_billing_settings(
    db: Session,
    *,
    workspace_id: UUID,
) -> list[PulseBillingSettingsRead]:
    ensure_legacy_laser_devices(db, workspace_id=workspace_id)
    devices = list(
        db.scalars(
            select(ClinicLaserDevice)
            .where(
                ClinicLaserDevice.workspace_id == workspace_id,
                ClinicLaserDevice.is_active.is_(True),
            )
            .order_by(ClinicLaserDevice.name, ClinicLaserDevice.id)
        )
    )
    rows = list(
        db.scalars(
            select(PulseBillingSettings).where(
                PulseBillingSettings.workspace_id == workspace_id,
            )
        )
    )
    settings_by_key = {row.device_key: row for row in rows}
    return [
        PulseBillingSettingsRead(
            device_key=device.device_key,
            device_name=device.name,
            overage_price_minor=(
                int(settings_by_key[device.device_key].overage_price_minor)
                if device.device_key in settings_by_key
                else None
            ),
            currency=(
                settings_by_key[device.device_key].currency
                if device.device_key in settings_by_key
                else "EGP"
            ),
        )
        for device in devices
    ]


def upsert_pulse_billing_settings(
    db: Session,
    *,
    workspace_id: UUID,
    device_key: str,
    overage_price_minor: int,
    currency: str,
) -> PulseBillingSettings:
    _active_clinic_laser_device(
        db,
        workspace_id=workspace_id,
        device_key=device_key,
    )
    if overage_price_minor <= 0:
        raise PulseBillingError("Pulse overage price must be greater than zero.")
    row = db.scalar(
        select(PulseBillingSettings)
        .where(
            PulseBillingSettings.workspace_id == workspace_id,
            PulseBillingSettings.device_key == device_key,
        )
        .with_for_update()
    )
    if row is None:
        row = PulseBillingSettings(
            workspace_id=workspace_id,
            device_key=device_key,
            overage_price_minor=overage_price_minor,
            currency=currency.upper(),
        )
        db.add(row)
    else:
        row.overage_price_minor = overage_price_minor
        row.currency = currency.upper()
    db.flush()
    return row


def list_pulse_pack_offers(
    db: Session,
    *,
    workspace_id: UUID,
    active_only: bool = False,
) -> list[PulsePackOfferRead]:
    stmt = select(PulsePackOffer).where(PulsePackOffer.workspace_id == workspace_id)
    if active_only:
        stmt = stmt.where(PulsePackOffer.is_active.is_(True))
    rows = list(
        db.scalars(
            stmt.order_by(PulsePackOffer.device_key, PulsePackOffer.pulses_count)
        ).all()
    )
    return [PulsePackOfferRead.model_validate(row) for row in rows]


def upsert_pulse_pack_offer(
    db: Session,
    *,
    workspace_id: UUID,
    device_key: str,
    pulses_count: int,
    price_minor: int,
    currency: str,
    is_active: bool,
) -> PulsePackOffer:
    device = _active_clinic_laser_device(
        db,
        workspace_id=workspace_id,
        device_key=device_key,
    )
    if pulses_count <= 0:
        raise PulseBillingError("Pulse pack size must be positive.")
    if price_minor < 0:
        raise PulseBillingError("Pulse pack price cannot be negative.")
    row = db.scalar(
        select(PulsePackOffer).where(
            PulsePackOffer.workspace_id == workspace_id,
            PulsePackOffer.device_key == device_key,
            PulsePackOffer.pulses_count == pulses_count,
        )
    )
    if row is None:
        row = PulsePackOffer(
            workspace_id=workspace_id,
            device_key=device.device_key,
            device_name=device.name,
            pulses_count=pulses_count,
            price_minor=price_minor,
            currency=currency.upper(),
            is_active=is_active,
        )
        db.add(row)
    else:
        row.device_name = device.name
        row.price_minor = price_minor
        row.currency = currency.upper()
        row.is_active = is_active
    db.flush()
    return row
def _effective_pack_status(pack: PatientPulsePack, *, on_date: date | None = None) -> str:
    if pack.status == "cancelled":
        return "cancelled"
    on_date = on_date or datetime.now(UTC).date()
    if pack.expires_at is not None and pack.expires_at < on_date:
        return "expired"
    return "active"


def _consumed_for_pack(
    db: Session,
    *,
    workspace_id: UUID,
    pack_id: UUID,
) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(PulseUsage.pulses_used), 0)).where(
                PulseUsage.workspace_id == workspace_id,
                PulseUsage.patient_pulse_pack_id == pack_id,
                PulseUsage.status == "consumed",
            )
        )
        or 0
    )


def _pack_financial_rows(
    db: Session,
    *,
    workspace_id: UUID,
    pack: PatientPulsePack,
    for_update: bool = False,
) -> tuple[list[PaymentTransaction], list[PaymentTransaction]]:
    payment_filter = or_(
        PaymentTransaction.patient_pulse_pack_id == pack.id,
        (
            PaymentTransaction.id == pack.purchase_transaction_id
            if pack.purchase_transaction_id is not None
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
        .order_by(PaymentTransaction.created_at, PaymentTransaction.id)
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
            PaymentTransaction.patient_pulse_pack_id == pack.id,
            PaymentTransaction.reference_transaction_id.in_(payment_ids),
        ),
    )
    if for_update:
        refund_stmt = refund_stmt.with_for_update()
    refunds = list(db.scalars(refund_stmt).all())
    return payments, refunds


def pulse_pack_read(
    db: Session,
    pack: PatientPulsePack,
    *,
    include_financials: bool = False,
) -> PatientPulsePackRead:
    consumed = _consumed_for_pack(
        db,
        workspace_id=pack.workspace_id,
        pack_id=pack.id,
    )
    remaining = max(int(pack.pulses_purchased) - consumed, 0)
    effective = _effective_pack_status(pack)
    if effective == "active" and remaining == 0:
        effective = "exhausted"
    paid = 0
    refunded = 0
    if include_financials:
        payments, refunds = _pack_financial_rows(
            db,
            workspace_id=pack.workspace_id,
            pack=pack,
        )
        paid = sum(int(row.amount_minor) for row in payments)
        refunded = sum(int(row.amount_minor) for row in refunds)
    return PatientPulsePackRead(
        id=pack.id,
        workspace_id=pack.workspace_id,
        patient_id=pack.patient_id,
        pulse_pack_offer_id=pack.pulse_pack_offer_id,
        origin_appointment_id=pack.origin_appointment_id,
        purchase_transaction_id=pack.purchase_transaction_id,
        device_key=pack.device_key,
        device_name=pack.device_name,
        pulses_purchased=int(pack.pulses_purchased),
        pulses_consumed=consumed,
        pulses_remaining=remaining,
        sale_price_minor=int(pack.sale_price_minor),
        amount_paid_minor=paid,
        amount_refunded_minor=refunded,
        balance_due_minor=max(int(pack.sale_price_minor) - paid + refunded, 0),
        standalone_pulse_price_minor_at_purchase=(
            int(pack.standalone_pulse_price_minor_at_purchase)
            if pack.standalone_pulse_price_minor_at_purchase is not None
            else None
        ),
        currency=pack.currency,
        purchased_at=pack.purchased_at,
        expires_at=pack.expires_at,
        status=pack.status,
        effective_status=effective,
        created_at=pack.created_at,
        updated_at=pack.updated_at,
    )


def list_patient_pulse_packs(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    device_key: str | None = None,
    include_financials: bool = False,
) -> list[PatientPulsePackRead]:
    stmt = select(PatientPulsePack).where(
        PatientPulsePack.workspace_id == workspace_id,
        PatientPulsePack.patient_id == patient_id,
    )
    if device_key is not None:
        stmt = stmt.where(PatientPulsePack.device_key == device_key)
    packs = list(
        db.scalars(
            stmt.order_by(PatientPulsePack.purchased_at.desc(), PatientPulsePack.id.desc())
        ).all()
    )
    return [
        pulse_pack_read(db, pack, include_financials=include_financials)
        for pack in packs
    ]


def list_patient_pulse_balances(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
) -> list[PulseBalanceRead]:
    reads = list_patient_pulse_packs(
        db,
        workspace_id=workspace_id,
        patient_id=patient_id,
        include_financials=False,
    )
    result: list[PulseBalanceRead] = []
    device_keys = sorted({item.device_key for item in reads})
    for device_key in device_keys:
        active = [
            item
            for item in reads
            if item.device_key == device_key
            and item.effective_status == "active"
            and item.pulses_remaining > 0
        ]
        if not active:
            continue
        result.append(
            PulseBalanceRead(
                device_key=device_key,
                device_name=active[0].device_name,
                pulses_purchased=sum(item.pulses_purchased for item in active),
                pulses_consumed=sum(item.pulses_consumed for item in active),
                pulses_remaining=sum(item.pulses_remaining for item in active),
                active_pack_count=len(active),
            )
        )
    return result
def _settings_price_snapshot(
    db: Session,
    *,
    workspace_id: UUID,
    device_key: str,
) -> int | None:
    row = db.scalar(
        select(PulseBillingSettings).where(
            PulseBillingSettings.workspace_id == workspace_id,
            PulseBillingSettings.device_key == device_key,
        )
    )
    return int(row.overage_price_minor) if row is not None else None


def _get_active_offer(
    db: Session,
    *,
    workspace_id: UUID,
    offer_id: UUID,
    for_update: bool = False,
) -> PulsePackOffer:
    stmt = select(PulsePackOffer).where(
        PulsePackOffer.workspace_id == workspace_id,
        PulsePackOffer.id == offer_id,
        PulsePackOffer.is_active.is_(True),
    )
    if for_update:
        stmt = stmt.with_for_update()
    offer = db.scalar(stmt)
    if offer is None:
        raise PulseBillingNotFound("Pulse pack offer not found or inactive.")
    return offer


def purchase_pulse_pack_offer(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    offer_id: UUID,
    amount_paid_minor: int,
    payment_method: str,
    created_by_user_id: UUID | None,
    external_reference: str | None = None,
    idempotency_key: str | None = None,
    actor_type: ActivityActorType = "staff",
    origin_appointment_id: UUID | None = None,
    expires_at: date | None = None,
) -> PatientPulsePack:
    offer = _get_active_offer(
        db,
        workspace_id=workspace_id,
        offer_id=offer_id,
        for_update=True,
    )
    if amount_paid_minor < 0 or amount_paid_minor > int(offer.price_minor):
        raise PulseBillingError("Initial pulse pack payment is outside the pack price.")
    if amount_paid_minor > 0 and (
        payment_method not in PAYMENT_METHODS or payment_method == "unknown"
    ):
        raise PulseBillingError("A paid pulse pack requires a supported payment method.")
    if idempotency_key:
        existing = db.scalar(
            select(PatientPulsePack).where(
                PatientPulsePack.workspace_id == workspace_id,
                PatientPulsePack.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.patient_id != patient_id or existing.pulse_pack_offer_id != offer_id:
                raise PulseBillingError(
                    "Idempotency key was already used for another pulse pack sale."
                )
            return existing

    patient = db.scalar(
        select(Patient)
        .where(Patient.workspace_id == workspace_id, Patient.id == patient_id)
        .with_for_update()
    )
    if patient is None:
        raise PulseBillingNotFound("Patient not found.")
    purchased_at = datetime.now(UTC)
    if expires_at is not None and expires_at < purchased_at.date():
        raise PulseBillingError("Pulse pack expiry cannot be before the purchase date.")

    transaction: PaymentTransaction | None = None
    if amount_paid_minor > 0:
        try:
            require_tia_workspace_domain_write(
                db,
                workspace_id=workspace_id,
                domain="payments",
            )
        except ClinicIntegrationAuthorityError as exc:
            raise PulseBillingError(str(exc)) from exc
        transaction = PaymentTransaction(
            workspace_id=workspace_id,
            appointment_id=origin_appointment_id,
            origin_appointment_id=origin_appointment_id,
            patient_id=patient_id,
            created_by_user_id=created_by_user_id,
            reference_transaction_id=None,
            patient_package_id=None,
            patient_pulse_pack_id=None,
            transaction_type="payment",
            amount_minor=amount_paid_minor,
            currency=offer.currency,
            payment_method=payment_method,
            source="staff" if actor_type == "staff" else "system",
            external_reference=external_reference,
            reason="Prepaid pulse pack purchase",
            idempotency_key=(
                f"pulse-payment:{idempotency_key}"[:128] if idempotency_key else None
            ),
            created_at=purchased_at,
        )
        db.add(transaction)
        db.flush()

    pack = PatientPulsePack(
        workspace_id=workspace_id,
        patient_id=patient_id,
        pulse_pack_offer_id=offer.id,
        origin_appointment_id=origin_appointment_id,
        purchase_transaction_id=transaction.id if transaction is not None else None,
        created_by_user_id=created_by_user_id,
        device_key=offer.device_key,
        device_name=offer.device_name,
        pulses_purchased=int(offer.pulses_count),
        sale_price_minor=int(offer.price_minor),
        standalone_pulse_price_minor_at_purchase=_settings_price_snapshot(
            db,
            workspace_id=workspace_id,
            device_key=offer.device_key,
        ),
        currency=offer.currency,
        purchased_at=purchased_at,
        expires_at=expires_at,
        status="active",
        idempotency_key=idempotency_key,
    )
    db.add(pack)
    db.flush()
    if transaction is not None:
        transaction.patient_pulse_pack_id = pack.id
        if origin_appointment_id is not None:
            db.add(
                PaymentAllocation(
                    workspace_id=workspace_id,
                    transaction_id=transaction.id,
                    appointment_id=origin_appointment_id,
                    amount_minor=amount_paid_minor,
                    created_at=transaction.created_at,
                )
            )
            db.flush()
            refresh_appointment_payment_snapshots(
                db,
                workspace_id=workspace_id,
                appointment_ids={origin_appointment_id},
            )
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type=actor_type,
        actor_user_id=created_by_user_id,
        action="pulse_pack.created",
        entity_type="patient_pulse_pack",
        entity_id=pack.id,
        summary="Prepaid pulse pack created",
        metadata={
            "offer_id": offer.id,
            "device_key": offer.device_key,
            "pulses_purchased": int(offer.pulses_count),
            "sale_price_minor": int(offer.price_minor),
            "amount_paid_minor": amount_paid_minor,
        },
    )
    return pack
def record_pulse_pack_payment(
    db: Session,
    *,
    workspace_id: UUID,
    pack_id: UUID,
    amount_minor: int,
    payment_method: str,
    created_by_user_id: UUID | None,
    external_reference: str | None = None,
    idempotency_key: str | None = None,
) -> PaymentTransaction:
    if amount_minor <= 0:
        raise PulseBillingError("Pulse pack payment amount must be positive.")
    if payment_method not in PAYMENT_METHODS or payment_method == "unknown":
        raise PulseBillingError("A pulse pack payment requires a supported payment method.")
    try:
        require_tia_workspace_domain_write(db, workspace_id=workspace_id, domain="payments")
    except ClinicIntegrationAuthorityError as exc:
        raise PulseBillingError(str(exc)) from exc
    if idempotency_key:
        existing = db.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.workspace_id == workspace_id,
                PaymentTransaction.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.patient_pulse_pack_id != pack_id
                or existing.amount_minor != amount_minor
                or existing.transaction_type != "payment"
            ):
                raise PulseBillingError(
                    "Idempotency key was already used for another pulse pack payment."
                )
            return existing
    pack = db.scalar(
        select(PatientPulsePack)
        .where(
            PatientPulsePack.workspace_id == workspace_id,
            PatientPulsePack.id == pack_id,
        )
        .with_for_update()
    )
    if pack is None:
        raise PulseBillingNotFound("Pulse pack not found.")
    if _effective_pack_status(pack) != "active":
        raise PulseBillingError("Payments can only be added to an active pulse pack.")
    payments, _refunds = _pack_financial_rows(
        db,
        workspace_id=workspace_id,
        pack=pack,
        for_update=True,
    )
    remaining_due = max(
        int(pack.sale_price_minor) - sum(int(row.amount_minor) for row in payments),
        0,
    )
    if amount_minor > remaining_due:
        raise PulseBillingError(
            f"Pulse pack payment exceeds remaining balance of {remaining_due} minor units."
        )
    transaction = PaymentTransaction(
        workspace_id=workspace_id,
        appointment_id=pack.origin_appointment_id,
        origin_appointment_id=pack.origin_appointment_id,
        patient_id=pack.patient_id,
        created_by_user_id=created_by_user_id,
        reference_transaction_id=None,
        patient_package_id=None,
        patient_pulse_pack_id=pack.id,
        transaction_type="payment",
        amount_minor=amount_minor,
        currency=pack.currency,
        payment_method=payment_method,
        source="staff",
        external_reference=external_reference,
        reason="Prepaid pulse pack payment",
        idempotency_key=idempotency_key,
        created_at=datetime.now(UTC),
    )
    db.add(transaction)
    db.flush()
    if pack.origin_appointment_id is not None:
        db.add(
            PaymentAllocation(
                workspace_id=workspace_id,
                transaction_id=transaction.id,
                appointment_id=pack.origin_appointment_id,
                amount_minor=amount_minor,
                created_at=transaction.created_at,
            )
        )
        db.flush()
        refresh_appointment_payment_snapshots(
            db,
            workspace_id=workspace_id,
            appointment_ids={pack.origin_appointment_id},
        )
    if pack.purchase_transaction_id is None:
        pack.purchase_transaction_id = transaction.id
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=created_by_user_id,
        action="pulse_pack.payment_recorded",
        entity_type="patient_pulse_pack",
        entity_id=pack.id,
        summary="Pulse pack payment recorded",
        metadata={"amount_minor": amount_minor, "payment_method": payment_method},
    )
    return transaction
def _available_pack_rows(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    device_key: str,
    on_date: date | None = None,
    for_update: bool = False,
) -> list[tuple[PatientPulsePack, int]]:
    on_date = on_date or datetime.now(UTC).date()
    stmt = select(PatientPulsePack).where(
        PatientPulsePack.workspace_id == workspace_id,
        PatientPulsePack.patient_id == patient_id,
        PatientPulsePack.device_key == device_key,
        PatientPulsePack.status == "active",
        or_(
            PatientPulsePack.expires_at.is_(None),
            PatientPulsePack.expires_at >= on_date,
        ),
    )
    if for_update:
        stmt = stmt.with_for_update()
    packs = list(
        db.scalars(
            stmt.order_by(
                PatientPulsePack.expires_at.asc().nullslast(),
                PatientPulsePack.purchased_at.asc(),
                PatientPulsePack.id.asc(),
            )
        ).all()
    )
    result: list[tuple[PatientPulsePack, int]] = []
    for pack in packs:
        remaining = max(
            int(pack.pulses_purchased)
            - _consumed_for_pack(db, workspace_id=workspace_id, pack_id=pack.id),
            0,
        )
        if remaining > 0:
            result.append((pack, remaining))
    return result
def available_pulse_balance(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    device_key: str,
    on_date: date | None = None,
) -> int:
    return sum(
        remaining
        for _pack, remaining in _available_pack_rows(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            device_key=device_key,
            on_date=on_date,
        )
    )


def validate_pulse_booking(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    device_key: str | None,
    appointment_date: date,
) -> None:
    if device_key is None:
        raise PulseBillingError("Pulse balance can only be used for a laser appointment.")
    balance = available_pulse_balance(
        db,
        workspace_id=workspace_id,
        patient_id=patient_id,
        device_key=device_key,
        on_date=appointment_date,
    )
    if balance <= 0:
        raise PulseBillingError(
            "The patient has no available pulse balance for the selected laser device."
        )


def _reverse_existing_appointment_usages(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
) -> None:
    rows = list(
        db.scalars(
            select(PulseUsage)
            .where(
                PulseUsage.workspace_id == workspace_id,
                PulseUsage.appointment_id == appointment_id,
                PulseUsage.appointment_additional_service_id.is_(None),
                PulseUsage.status == "consumed",
            )
            .with_for_update()
        ).all()
    )
    for row in rows:
        row.status = "reversed"
    if rows:
        db.flush()
def _consume_from_available_packs(
    db: Session,
    *,
    appointment: Appointment,
    pulses_needed: int,
    used_at: datetime,
    device_key: str | None = None,
    additional_service_id: UUID | None = None,
) -> int:
    remaining_needed = max(int(pulses_needed), 0)
    consumed = 0
    for pack, available in _available_pack_rows(
        db,
        workspace_id=appointment.workspace_id,
        patient_id=appointment.patient_id,
        device_key=str(device_key or appointment.laser_device_key),
        on_date=used_at.date(),
        for_update=True,
    ):
        if remaining_needed <= 0:
            break
        quantity = min(available, remaining_needed)
        db.add(
            PulseUsage(
                workspace_id=appointment.workspace_id,
                patient_pulse_pack_id=pack.id,
                appointment_id=appointment.id,
                appointment_additional_service_id=additional_service_id,
                pulses_used=quantity,
                status="consumed",
                used_at=used_at,
            )
        )
        consumed += quantity
        remaining_needed -= quantity
    db.flush()
    return consumed


def settle_appointment_pulses(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    pulses_used: int,
    changed_by_user_id: UUID | None,
) -> AppointmentPulseSettlement:
    if pulses_used < 0:
        raise PulseBillingError("Pulse usage cannot be negative.")
    appointment = db.scalar(
        select(Appointment)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.id == appointment_id,
        )
        .with_for_update()
    )
    if appointment is None:
        raise PulseBillingNotFound("Appointment not found.")
    if appointment.billing_context != "pulse_prepaid":
        raise PulseBillingError("This appointment is not billed from a pulse balance.")
    if appointment.laser_device_key is None:
        raise PulseBillingError("Pulse billing requires a laser device.")
    if appointment.status == "completed":
        raise PulseBillingError(
            "Pulse usage cannot be changed after the appointment is completed."
        )
    settlement = db.scalar(
        select(AppointmentPulseSettlement)
        .where(
            AppointmentPulseSettlement.workspace_id == workspace_id,
            AppointmentPulseSettlement.appointment_id == appointment.id,
        )
        .with_for_update()
    )
    if settlement is not None and settlement.resolution in {"new_pack", "overage"}:
        raise PulseBillingError(
            "Pulse usage is already financially settled and needs admin correction."
        )

    _reverse_existing_appointment_usages(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment.id,
    )
    now = datetime.now(UTC)
    consumed = _consume_from_available_packs(
        db,
        appointment=appointment,
        pulses_needed=pulses_used,
        used_at=now,
    )
    deficit = max(pulses_used - consumed, 0)
    appointment.laser_pulses_used = pulses_used
    if settlement is None:
        settlement = AppointmentPulseSettlement(
            workspace_id=workspace_id,
            appointment_id=appointment.id,
            pulses_used=pulses_used,
            pulses_from_balance=consumed,
            deficit_pulses=deficit,
            resolution="pending" if deficit else "balance",
            overage_charge_minor=0,
            resolved_at=None if deficit else now,
        )
        db.add(settlement)
    else:
        settlement.pulses_used = pulses_used
        settlement.pulses_from_balance = consumed
        settlement.deficit_pulses = deficit
        settlement.resolution = "pending" if deficit else "balance"
        settlement.resolution_pulse_pack_id = None
        settlement.overage_unit_price_minor = None
        settlement.overage_charge_minor = 0
        settlement.resolved_at = None if deficit else now
    db.flush()
    refresh_appointment_payment_snapshots(
        db,
        workspace_id=workspace_id,
        appointment_ids={appointment.id},
    )
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=changed_by_user_id,
        action="appointment.pulse_usage_settled",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Laser pulse usage reconciled against patient balance",
        metadata={
            "pulses_used": pulses_used,
            "pulses_from_balance": consumed,
            "deficit_pulses": deficit,
            "resolution": settlement.resolution,
        },
    )
    return settlement
def _appointment_net_paid_minor(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
) -> int:
    rows = db.execute(
        select(
            PaymentTransaction.transaction_type,
            PaymentAllocation.amount_minor,
        )
        .join(
            PaymentAllocation,
            (PaymentAllocation.workspace_id == PaymentTransaction.workspace_id)
            & (PaymentAllocation.transaction_id == PaymentTransaction.id),
        )
        .where(
            PaymentTransaction.workspace_id == workspace_id,
            PaymentAllocation.appointment_id == appointment_id,
        )
    ).all()
    paid = sum(int(amount) for kind, amount in rows if kind == "payment")
    refunded = sum(int(amount) for kind, amount in rows if kind == "refund")
    return max(paid - refunded, 0)


def _device_overage_price(
    db: Session,
    *,
    workspace_id: UUID,
    device_key: str,
    for_update: bool = False,
) -> tuple[int, str]:
    stmt = select(PulseBillingSettings).where(
        PulseBillingSettings.workspace_id == workspace_id,
        PulseBillingSettings.device_key == device_key,
    )
    if for_update:
        stmt = stmt.with_for_update()
    settings = db.scalar(stmt)
    if settings is None or int(settings.overage_price_minor) <= 0:
        raise PulseBillingError(
            "Set the pulse overage price for this laser device before charging extra pulses."
        )
    return int(settings.overage_price_minor), settings.currency


def apply_additional_service_pulse_billing(
    db: Session,
    *,
    appointment: Appointment,
    line: AppointmentAdditionalService,
    pulses_used: int,
    mode: str,
    offer_id: UUID | None,
    changed_by_user_id: UUID | None,
    idempotency_key: str | None,
) -> AppointmentAdditionalService:
    if mode not in {"use_balance", "purchase_pack", "overage"}:
        raise PulseBillingError("Unsupported additional-service pulse billing mode.")
    if line.patient_package_id is not None:
        raise PulseBillingError(
            "A package-backed additional service cannot also use pulse billing."
        )
    if line.billing_context != "standard":
        raise PulseBillingError("This additional service already has a billing method.")
    if line.laser_device_key is None:
        raise PulseBillingError("Pulse billing is only available for laser services.")
    if pulses_used <= 0:
        raise PulseBillingError("Pulse usage must be greater than zero.")

    now = datetime.now(UTC)
    consumed = _consume_from_available_packs(
        db,
        appointment=appointment,
        pulses_needed=pulses_used,
        used_at=now,
        device_key=line.laser_device_key,
        additional_service_id=line.id,
    )
    deficit = max(pulses_used - consumed, 0)
    resolution = "balance"
    resolution_pack_id: UUID | None = None
    overage_unit_price: int | None = None
    overage_charge = 0

    if mode == "use_balance":
        if deficit > 0:
            raise PulseBillingError(
                f"The patient pulse balance is short by {deficit} pulses."
            )
    elif mode == "purchase_pack":
        if offer_id is None:
            raise PulseBillingError("Select a pulse pack.")
        offer = _get_active_offer(
            db,
            workspace_id=appointment.workspace_id,
            offer_id=offer_id,
            for_update=True,
        )
        if offer.device_key != line.laser_device_key:
            raise PulseBillingError(
                "Selected pulse pack is for a different laser device."
            )
        if int(offer.pulses_count) < deficit:
            raise PulseBillingError(
                "Selected pulse pack does not contain enough pulses to cover this service."
            )
        pack = purchase_pulse_pack_offer(
            db,
            workspace_id=appointment.workspace_id,
            patient_id=appointment.patient_id,
            offer_id=offer.id,
            amount_paid_minor=0,
            payment_method="unknown",
            created_by_user_id=changed_by_user_id,
            external_reference=None,
            idempotency_key=(
                f"{idempotency_key}:extra-pulse-pack"[:128]
                if idempotency_key
                else None
            ),
            actor_type="staff",
            origin_appointment_id=appointment.id,
        )
        if deficit > 0:
            db.add(
                PulseUsage(
                    workspace_id=appointment.workspace_id,
                    patient_pulse_pack_id=pack.id,
                    appointment_id=appointment.id,
                    appointment_additional_service_id=line.id,
                    pulses_used=deficit,
                    status="consumed",
                    used_at=now,
                )
            )
            consumed += deficit
        resolution = "new_pack"
        resolution_pack_id = pack.id
    else:
        if deficit > 0:
            overage_unit_price, _currency = _device_overage_price(
                db,
                workspace_id=appointment.workspace_id,
                device_key=line.laser_device_key,
                for_update=True,
            )
            overage_charge = deficit * overage_unit_price
            resolution = "overage"

    line.billing_context = "pulse_prepaid"
    line.laser_pulses_used = pulses_used
    line.pulse_resolution = resolution
    line.pulse_resolution_pulse_pack_id = resolution_pack_id
    line.pulse_overage_unit_price_minor = overage_unit_price
    line.pulse_overage_charge_minor = overage_charge
    db.flush()

    record_activity_event(
        db,
        workspace_id=appointment.workspace_id,
        actor_type="staff",
        actor_user_id=changed_by_user_id,
        action="appointment.additional_service_pulse_billing_applied",
        entity_type="appointment_additional_service",
        entity_id=line.id,
        summary="Additional laser service billed with pulses",
        metadata={
            "appointment_id": str(appointment.id),
            "device_key": line.laser_device_key,
            "pulses_used": pulses_used,
            "pulses_from_balance": consumed,
            "deficit_pulses": deficit,
            "resolution": resolution,
            "pulse_pack_offer_id": str(offer_id) if offer_id else None,
            "overage_unit_price_minor": overage_unit_price,
            "overage_charge_minor": overage_charge,
        },
    )
    return line


def checkout_appointment_pulses(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    mode: str,
    offer_id: UUID | None,
    changed_by_user_id: UUID | None,
    idempotency_key: str | None,
) -> AppointmentPulseSettlement:
    if mode not in {"use_balance", "purchase_pack", "overage"}:
        raise PulseBillingError("Unsupported pulse checkout mode.")

    appointment = db.scalar(
        select(Appointment)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.id == appointment_id,
        )
        .with_for_update()
    )
    if appointment is None:
        raise PulseBillingNotFound("Appointment not found.")
    if appointment.status not in {"pending", "confirmed", "completed"}:
        raise PulseBillingError(
            f"Pulse checkout is not available for an appointment in '{appointment.status}' status."
        )
    if appointment.laser_device_key is None:
        raise PulseBillingError("Pulse checkout requires a laser appointment.")
    if appointment.laser_pulses_used is None:
        raise PulseBillingError(
            "Record the actual laser pulse usage before choosing pulse billing."
        )
    if appointment.patient_package_id is not None or appointment.billing_context == "package_prepaid":
        raise PulseBillingError(
            "A session package and pulse billing cannot be used for the same appointment."
        )

    existing_settlement = get_appointment_pulse_settlement(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment.id,
    )
    if appointment.billing_context == "pulse_prepaid":
        if existing_settlement is None:
            existing_settlement = settle_appointment_pulses(
                db,
                workspace_id=workspace_id,
                appointment_id=appointment.id,
                pulses_used=int(appointment.laser_pulses_used),
                changed_by_user_id=changed_by_user_id,
            )
        if existing_settlement.resolution != "pending":
            return existing_settlement
        if mode == "use_balance":
            raise PulseBillingError(
                "The existing pulse balance does not fully cover this session."
            )
        if mode == "purchase_pack":
            if offer_id is None:
                raise PulseBillingError("Select a pulse pack to cover the deficit.")
            return resolve_pulse_deficit_with_pack(
                db,
                workspace_id=workspace_id,
                appointment_id=appointment.id,
                offer_id=offer_id,
                amount_paid_minor=0,
                payment_method="unknown",
                external_reference=None,
                changed_by_user_id=changed_by_user_id,
                idempotency_key=(
                    f"{idempotency_key}:pulse-pack"[:128] if idempotency_key else None
                ),
            )
        return resolve_pulse_deficit_with_overage(
            db,
            workspace_id=workspace_id,
            appointment_id=appointment.id,
            changed_by_user_id=changed_by_user_id,
        )

    if _appointment_net_paid_minor(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment.id,
    ) > 0:
        raise PulseBillingError(
            "This appointment already has recorded payment. Refund or correct that payment "
            "before switching the base service to pulse billing."
        )

    _reverse_existing_appointment_usages(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment.id,
    )
    now = datetime.now(UTC)
    pulses_used = int(appointment.laser_pulses_used)
    consumed = _consume_from_available_packs(
        db,
        appointment=appointment,
        pulses_needed=pulses_used,
        used_at=now,
    )
    deficit = max(pulses_used - consumed, 0)
    resolution = "balance"
    resolution_pack_id: UUID | None = None
    overage_unit_price: int | None = None
    overage_charge = 0

    if mode == "use_balance":
        if deficit > 0:
            raise PulseBillingError(
                f"The patient pulse balance is short by {deficit} pulses."
            )
    elif mode == "purchase_pack":
        if offer_id is None:
            raise PulseBillingError("Select a pulse pack.")
        offer = _get_active_offer(
            db,
            workspace_id=workspace_id,
            offer_id=offer_id,
            for_update=True,
        )
        if offer.device_key != appointment.laser_device_key:
            raise PulseBillingError(
                "Selected pulse pack is for a different laser device."
            )
        if int(offer.pulses_count) < deficit:
            raise PulseBillingError(
                "Selected pulse pack does not contain enough pulses to cover this session."
            )
        pack = purchase_pulse_pack_offer(
            db,
            workspace_id=workspace_id,
            patient_id=appointment.patient_id,
            offer_id=offer.id,
            amount_paid_minor=0,
            payment_method="unknown",
            created_by_user_id=changed_by_user_id,
            external_reference=None,
            idempotency_key=(
                f"{idempotency_key}:pulse-pack"[:128] if idempotency_key else None
            ),
            actor_type="staff",
            origin_appointment_id=appointment.id,
        )
        if deficit > 0:
            db.add(
                PulseUsage(
                    workspace_id=workspace_id,
                    patient_pulse_pack_id=pack.id,
                    appointment_id=appointment.id,
                    pulses_used=deficit,
                    status="consumed",
                    used_at=now,
                )
            )
            consumed += deficit
            resolution = "new_pack"
            resolution_pack_id = pack.id
    else:
        if deficit > 0:
            overage_unit_price, _currency = _device_overage_price(
                db,
                workspace_id=workspace_id,
                device_key=appointment.laser_device_key,
                for_update=True,
            )
            overage_charge = deficit * overage_unit_price
            resolution = "overage"

    settlement = existing_settlement
    if settlement is None:
        settlement = AppointmentPulseSettlement(
            workspace_id=workspace_id,
            appointment_id=appointment.id,
            pulses_used=pulses_used,
            pulses_from_balance=consumed,
            deficit_pulses=deficit,
            resolution=resolution,
            resolution_pulse_pack_id=resolution_pack_id,
            overage_unit_price_minor=overage_unit_price,
            overage_charge_minor=overage_charge,
            resolved_at=now,
        )
        db.add(settlement)
    else:
        settlement.pulses_used = pulses_used
        settlement.pulses_from_balance = consumed
        settlement.deficit_pulses = deficit
        settlement.resolution = resolution
        settlement.resolution_pulse_pack_id = resolution_pack_id
        settlement.overage_unit_price_minor = overage_unit_price
        settlement.overage_charge_minor = overage_charge
        settlement.resolved_at = now

    appointment.billing_context = "pulse_prepaid"
    db.flush()
    refresh_appointment_payment_snapshots(
        db,
        workspace_id=workspace_id,
        appointment_ids={appointment.id},
    )
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=changed_by_user_id,
        action="appointment.pulse_checkout_applied",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Appointment switched to pulse billing during checkout",
        metadata={
            "device_key": appointment.laser_device_key,
            "pulses_used": pulses_used,
            "pulses_from_balance": consumed,
            "deficit_pulses": deficit,
            "resolution": resolution,
            "pulse_pack_offer_id": str(offer_id) if offer_id else None,
            "overage_unit_price_minor": overage_unit_price,
            "overage_charge_minor": overage_charge,
        },
    )
    return settlement


def _locked_pending_settlement(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
) -> tuple[Appointment, AppointmentPulseSettlement]:
    appointment = db.scalar(
        select(Appointment)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.id == appointment_id,
        )
        .with_for_update()
    )
    settlement = db.scalar(
        select(AppointmentPulseSettlement)
        .where(
            AppointmentPulseSettlement.workspace_id == workspace_id,
            AppointmentPulseSettlement.appointment_id == appointment_id,
        )
        .with_for_update()
    )
    if appointment is None or settlement is None:
        raise PulseBillingNotFound("Pulse settlement not found.")
    if appointment.billing_context != "pulse_prepaid":
        raise PulseBillingError("This appointment is not billed from a pulse balance.")
    if settlement.resolution != "pending" or settlement.deficit_pulses <= 0:
        raise PulseBillingError("This pulse settlement has no unresolved deficit.")
    return appointment, settlement


def resolve_pulse_deficit_with_overage(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    changed_by_user_id: UUID | None,
) -> AppointmentPulseSettlement:
    appointment, settlement = _locked_pending_settlement(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
    )
    settings = db.scalar(
        select(PulseBillingSettings)
        .where(
            PulseBillingSettings.workspace_id == workspace_id,
            PulseBillingSettings.device_key == appointment.laser_device_key,
        )
        .with_for_update()
    )
    if settings is None or int(settings.overage_price_minor) <= 0:
        raise PulseBillingError(
            "Set the clinic pulse price before charging extra pulses."
        )
    unit_price = int(settings.overage_price_minor)
    settlement.resolution = "overage"
    settlement.overage_unit_price_minor = unit_price
    settlement.overage_charge_minor = int(settlement.deficit_pulses) * unit_price
    settlement.resolved_at = datetime.now(UTC)
    db.flush()
    refresh_appointment_payment_snapshots(
        db,
        workspace_id=workspace_id,
        appointment_ids={appointment.id},
    )
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=changed_by_user_id,
        action="appointment.pulse_deficit_charged",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Pulse deficit added to appointment charges",
        metadata={
            "deficit_pulses": int(settlement.deficit_pulses),
            "unit_price_minor": unit_price,
            "charge_minor": int(settlement.overage_charge_minor),
        },
    )
    return settlement


def resolve_pulse_deficit_with_pack(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    offer_id: UUID,
    amount_paid_minor: int,
    payment_method: str,
    external_reference: str | None,
    changed_by_user_id: UUID | None,
    idempotency_key: str | None,
) -> AppointmentPulseSettlement:
    appointment, settlement = _locked_pending_settlement(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
    )
    offer = _get_active_offer(
        db,
        workspace_id=workspace_id,
        offer_id=offer_id,
        for_update=True,
    )
    if offer.device_key != appointment.laser_device_key:
        raise PulseBillingError("Selected pulse pack is for a different laser device.")
    if int(offer.pulses_count) < int(settlement.deficit_pulses):
        raise PulseBillingError(
            "Selected pulse pack does not contain enough pulses to cover the deficit."
        )
    pack = purchase_pulse_pack_offer(
        db,
        workspace_id=workspace_id,
        patient_id=appointment.patient_id,
        offer_id=offer.id,
        amount_paid_minor=amount_paid_minor,
        payment_method=payment_method,
        external_reference=external_reference,
        created_by_user_id=changed_by_user_id,
        idempotency_key=idempotency_key,
        actor_type="staff",
        origin_appointment_id=appointment.id,
    )
    deficit = int(settlement.deficit_pulses)
    db.add(
        PulseUsage(
            workspace_id=workspace_id,
            patient_pulse_pack_id=pack.id,
            appointment_id=appointment.id,
            pulses_used=deficit,
            status="consumed",
            used_at=datetime.now(UTC),
        )
    )
    settlement.pulses_from_balance = int(settlement.pulses_from_balance) + deficit
    settlement.resolution = "new_pack"
    settlement.resolution_pulse_pack_id = pack.id
    settlement.overage_unit_price_minor = None
    settlement.overage_charge_minor = 0
    settlement.resolved_at = datetime.now(UTC)
    db.flush()
    refresh_appointment_payment_snapshots(
        db,
        workspace_id=workspace_id,
        appointment_ids={appointment.id},
    )
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=changed_by_user_id,
        action="appointment.pulse_deficit_covered_by_pack",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Pulse deficit covered by a new pulse pack",
        metadata={
            "deficit_pulses": deficit,
            "patient_pulse_pack_id": pack.id,
            "offer_id": offer.id,
        },
    )
    return settlement


def get_appointment_pulse_settlement(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
) -> AppointmentPulseSettlement | None:
    return db.scalar(
        select(AppointmentPulseSettlement).where(
            AppointmentPulseSettlement.workspace_id == workspace_id,
            AppointmentPulseSettlement.appointment_id == appointment_id,
        )
    )


def pulse_settlement_read(
    db: Session,
    *,
    settlement: AppointmentPulseSettlement,
    appointment: Appointment,
) -> AppointmentPulseSettlementRead:
    balance = 0
    if appointment.laser_device_key is not None:
        balance = available_pulse_balance(
            db,
            workspace_id=appointment.workspace_id,
            patient_id=appointment.patient_id,
            device_key=appointment.laser_device_key,
        )
    settings = get_pulse_billing_settings(
        db,
        workspace_id=appointment.workspace_id,
        device_key=str(appointment.laser_device_key),
    )
    return AppointmentPulseSettlementRead(
        appointment_id=appointment.id,
        pulses_used=int(settlement.pulses_used),
        pulses_from_balance=int(settlement.pulses_from_balance),
        deficit_pulses=int(settlement.deficit_pulses),
        resolution=settlement.resolution,
        resolution_pulse_pack_id=settlement.resolution_pulse_pack_id,
        overage_unit_price_minor=settlement.overage_unit_price_minor,
        overage_charge_minor=int(settlement.overage_charge_minor),
        resolved_at=settlement.resolved_at,
        available_balance_after=balance,
        currency=settings.currency,
    )


def require_pulse_settlement_before_completion(
    db: Session,
    *,
    appointment: Appointment,
) -> None:
    if appointment.billing_context != "pulse_prepaid":
        return
    if appointment.laser_pulses_used is None:
        raise PulseBillingError(
            "Record the actual laser pulses before completing a pulse-billed appointment."
        )
    settlement = get_appointment_pulse_settlement(
        db,
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
    )
    if settlement is None or settlement.resolution == "pending":
        raise PulseBillingError(
            "Resolve the pulse balance deficit before completing this appointment."
        )

def release_appointment_pulse_usage(
    db: Session,
    *,
    appointment: Appointment,
    changed_by_user_id: UUID | None,
    reason: str,
) -> None:
    primary_pulse_billed = (
        getattr(appointment, "billing_context", "standard") == "pulse_prepaid"
    )
    additional_lines = list(
        db.scalars(
            select(AppointmentAdditionalService)
            .where(
                AppointmentAdditionalService.workspace_id == appointment.workspace_id,
                AppointmentAdditionalService.appointment_id == appointment.id,
                AppointmentAdditionalService.billing_context == "pulse_prepaid",
            )
            .with_for_update()
        ).all()
    )
    if not primary_pulse_billed and not additional_lines:
        return

    settlement = (
        get_appointment_pulse_settlement(
            db,
            workspace_id=appointment.workspace_id,
            appointment_id=appointment.id,
        )
        if primary_pulse_billed
        else None
    )
    rows = list(
        db.scalars(
            select(PulseUsage)
            .where(
                PulseUsage.workspace_id == appointment.workspace_id,
                PulseUsage.appointment_id == appointment.id,
                PulseUsage.status == "consumed",
            )
            .with_for_update()
        ).all()
    )
    for row in rows:
        row.status = "reversed"

    released_at = datetime.now(UTC)
    if settlement is not None:
        settlement.pulses_used = 0
        settlement.pulses_from_balance = 0
        settlement.deficit_pulses = 0
        settlement.resolution = "balance"
        settlement.resolution_pulse_pack_id = None
        settlement.overage_unit_price_minor = None
        settlement.overage_charge_minor = 0
        settlement.resolved_at = released_at
    if primary_pulse_billed:
        appointment.laser_pulses_used = 0

    for line in additional_lines:
        line.laser_pulses_used = 0
        line.pulse_resolution = "balance"
        line.pulse_resolution_pulse_pack_id = None
        line.pulse_overage_unit_price_minor = None
        line.pulse_overage_charge_minor = 0

    db.flush()
    refresh_appointment_payment_snapshots(
        db,
        workspace_id=appointment.workspace_id,
        appointment_ids={appointment.id},
    )
    record_activity_event(
        db,
        workspace_id=appointment.workspace_id,
        actor_type="staff",
        actor_user_id=changed_by_user_id,
        action="appointment.pulse_usage_released",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Pulse usage released from appointment",
        metadata={
            "reason": reason,
            "reversed_usage_rows": len(rows),
            "additional_service_lines": len(additional_lines),
        },
    )
