from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess, get_workspace_admin, get_workspace_reader
from app.database.session import get_db
from app.models.appointment import Appointment
from app.models.pulse_billing import PatientPulsePack
from app.schemas.pulse_billing import (
    AppointmentPulseSettlementRead,
    PatientPulsePackRead,
    PulseBalanceRead,
    PulseBillingSettingsRead,
    PulseBillingSettingsUpsert,
    PulseDeficitPackResolution,
    PulsePackOfferRead,
    PulsePackOfferUpsert,
    PulsePackPaymentCreate,
    PulsePackPurchase,
)
from app.services.pulse_billing import (
    PulseBillingError,
    PulseBillingNotFound,
    get_appointment_pulse_settlement,
    get_pulse_billing_settings,
    list_patient_pulse_balances,
    list_patient_pulse_packs,
    list_pulse_billing_settings,
    list_pulse_pack_offers,
    pulse_pack_read,
    pulse_settlement_read,
    purchase_pulse_pack_offer,
    record_pulse_pack_payment,
    resolve_pulse_deficit_with_overage,
    resolve_pulse_deficit_with_pack,
    upsert_pulse_billing_settings,
    upsert_pulse_pack_offer,
)

router = APIRouter()


def _raise(exc: Exception) -> None:
    code = (
        status.HTTP_404_NOT_FOUND
        if isinstance(exc, PulseBillingNotFound)
        else status.HTTP_409_CONFLICT
    )
    raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.get("/pulse-settings", response_model=PulseBillingSettingsRead)
def read_pulse_settings(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> PulseBillingSettingsRead:
    """Backward-compatible single-device view; new clients use /pulse-device-prices."""
    settings = list_pulse_billing_settings(
        db,
        workspace_id=access.workspace.id,
    )
    if not settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active laser devices are configured for this clinic.",
        )
    return next(
        (item for item in settings if item.device_key == "candela_gentle"),
        settings[0],
    )


@router.put("/pulse-settings", response_model=PulseBillingSettingsRead)
def save_pulse_settings(
    payload: PulseBillingSettingsUpsert,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> PulseBillingSettingsRead:
    """Backward-compatible endpoint that updates the requested device."""
    try:
        upsert_pulse_billing_settings(
            db,
            workspace_id=access.workspace.id,
            device_key=payload.device_key,
            overage_price_minor=payload.overage_price_minor,
            currency=payload.currency,
        )
        db.commit()
    except PulseBillingError as exc:
        db.rollback()
        _raise(exc)
    return get_pulse_billing_settings(
        db,
        workspace_id=access.workspace.id,
        device_key=payload.device_key,
    )


@router.get(
    "/pulse-device-prices",
    response_model=list[PulseBillingSettingsRead],
)
def pulse_device_prices(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[PulseBillingSettingsRead]:
    return list_pulse_billing_settings(
        db,
        workspace_id=access.workspace.id,
    )


@router.put(
    "/pulse-device-prices",
    response_model=list[PulseBillingSettingsRead],
)
def save_pulse_device_price(
    payload: PulseBillingSettingsUpsert,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[PulseBillingSettingsRead]:
    try:
        upsert_pulse_billing_settings(
            db,
            workspace_id=access.workspace.id,
            device_key=payload.device_key,
            overage_price_minor=payload.overage_price_minor,
            currency=payload.currency,
        )
        db.commit()
    except PulseBillingError as exc:
        db.rollback()
        _raise(exc)
    return list_pulse_billing_settings(
        db,
        workspace_id=access.workspace.id,
    )


@router.get("/pulse-pack-offers", response_model=list[PulsePackOfferRead])
def pulse_pack_offers(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    active_only: bool = False,
) -> list[PulsePackOfferRead]:
    return list_pulse_pack_offers(
        db,
        workspace_id=access.workspace.id,
        active_only=active_only,
    )


@router.put("/pulse-pack-offers", response_model=list[PulsePackOfferRead])
def save_pulse_pack_offer(
    payload: PulsePackOfferUpsert,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[PulsePackOfferRead]:
    try:
        upsert_pulse_pack_offer(
            db,
            workspace_id=access.workspace.id,
            device_key=payload.device_key,
            pulses_count=payload.pulses_count,
            price_minor=payload.price_minor,
            currency=payload.currency,
            is_active=payload.is_active,
        )
        db.commit()
    except PulseBillingError as exc:
        db.rollback()
        _raise(exc)
    return list_pulse_pack_offers(db, workspace_id=access.workspace.id)
@router.post(
    "/pulse-pack-offers/purchase",
    response_model=PatientPulsePackRead,
    status_code=status.HTTP_201_CREATED,
)
def buy_pulse_pack_offer(
    payload: PulsePackPurchase,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128),
    ] = None,
) -> PatientPulsePackRead:
    try:
        pack = purchase_pulse_pack_offer(
            db,
            workspace_id=access.workspace.id,
            patient_id=payload.patient_id,
            offer_id=payload.offer_id,
            amount_paid_minor=payload.amount_paid_minor,
            payment_method=payload.payment_method,
            external_reference=payload.external_reference,
            created_by_user_id=access.user.id,
            idempotency_key=idempotency_key,
            expires_at=payload.expires_at,
        )
        db.commit()
        db.refresh(pack)
        return pulse_pack_read(db, pack, include_financials=True)
    except PulseBillingError as exc:
        db.rollback()
        _raise(exc)


@router.get(
    "/patients/{patient_id}/pulse-packs",
    response_model=list[PatientPulsePackRead],
)
def patient_pulse_packs(
    patient_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[PatientPulsePackRead]:
    return list_patient_pulse_packs(
        db,
        workspace_id=access.workspace.id,
        patient_id=patient_id,
        include_financials=True,
    )


@router.get(
    "/patients/{patient_id}/pulse-balance",
    response_model=list[PulseBalanceRead],
)
def patient_pulse_balance(
    patient_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[PulseBalanceRead]:
    return list_patient_pulse_balances(
        db,
        workspace_id=access.workspace.id,
        patient_id=patient_id,
    )


@router.post(
    "/patient-pulse-packs/{pack_id}/payments",
    response_model=PatientPulsePackRead,
    status_code=status.HTTP_201_CREATED,
)
def add_pulse_pack_payment(
    pack_id: UUID,
    payload: PulsePackPaymentCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128),
    ] = None,
) -> PatientPulsePackRead:
    try:
        record_pulse_pack_payment(
            db,
            workspace_id=access.workspace.id,
            pack_id=pack_id,
            amount_minor=payload.amount_minor,
            payment_method=payload.payment_method,
            external_reference=payload.external_reference,
            created_by_user_id=access.user.id,
            idempotency_key=idempotency_key,
        )
        pack = db.scalar(
            select(PatientPulsePack).where(
                PatientPulsePack.workspace_id == access.workspace.id,
                PatientPulsePack.id == pack_id,
            )
        )
        if pack is None:
            raise PulseBillingNotFound("Pulse pack not found.")
        db.commit()
        db.refresh(pack)
        return pulse_pack_read(db, pack, include_financials=True)
    except PulseBillingError as exc:
        db.rollback()
        _raise(exc)


@router.get(
    "/appointments/{appointment_id}/pulse-settlement",
    response_model=AppointmentPulseSettlementRead | None,
)
def read_appointment_pulse_settlement(
    appointment_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> AppointmentPulseSettlementRead | None:
    appointment = db.scalar(
        select(Appointment).where(
            Appointment.workspace_id == access.workspace.id,
            Appointment.id == appointment_id,
        )
    )
    if appointment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found.")
    settlement = get_appointment_pulse_settlement(
        db,
        workspace_id=access.workspace.id,
        appointment_id=appointment_id,
    )
    if settlement is None:
        return None
    return pulse_settlement_read(
        db,
        settlement=settlement,
        appointment=appointment,
    )


@router.post(
    "/appointments/{appointment_id}/pulse-settlement/overage",
    response_model=AppointmentPulseSettlementRead,
)
def charge_pulse_overage(
    appointment_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> AppointmentPulseSettlementRead:
    try:
        settlement = resolve_pulse_deficit_with_overage(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            changed_by_user_id=access.user.id,
        )
        appointment = db.scalar(
            select(Appointment).where(
                Appointment.workspace_id == access.workspace.id,
                Appointment.id == appointment_id,
            )
        )
        if appointment is None:
            raise PulseBillingNotFound("Appointment not found.")
        db.commit()
        db.refresh(settlement)
        return pulse_settlement_read(db, settlement=settlement, appointment=appointment)
    except PulseBillingError as exc:
        db.rollback()
        _raise(exc)


@router.post(
    "/appointments/{appointment_id}/pulse-settlement/purchase-pack",
    response_model=AppointmentPulseSettlementRead,
)
def cover_pulse_deficit_with_pack(
    appointment_id: UUID,
    payload: PulseDeficitPackResolution,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128),
    ] = None,
) -> AppointmentPulseSettlementRead:
    try:
        settlement = resolve_pulse_deficit_with_pack(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            offer_id=payload.offer_id,
            amount_paid_minor=payload.amount_paid_minor,
            payment_method=payload.payment_method,
            external_reference=payload.external_reference,
            changed_by_user_id=access.user.id,
            idempotency_key=idempotency_key,
        )
        appointment = db.scalar(
            select(Appointment).where(
                Appointment.workspace_id == access.workspace.id,
                Appointment.id == appointment_id,
            )
        )
        if appointment is None:
            raise PulseBillingNotFound("Appointment not found.")
        db.commit()
        db.refresh(settlement)
        return pulse_settlement_read(db, settlement=settlement, appointment=appointment)
    except PulseBillingError as exc:
        db.rollback()
        _raise(exc)
