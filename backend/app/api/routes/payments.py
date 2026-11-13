from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api.dependencies.section_access import require_section_access
from app.api.dependencies.security import (
    WorkspaceAccess,
    get_workspace_access,
)
from app.database.session import get_db
from app.schemas.payments import (
    AppointmentCheckoutCreate,
    AppointmentDiscountUpdate,
    AppointmentPaymentSummaryRead,
    PaymentCreate,
    RefundCreate,
)
from app.services.appointment_checkout import (
    AppointmentCheckoutError,
    checkout_appointment,
)
from app.services.payments import (
    PaymentOperationError,
    PaymentOperationNotFound,
    get_appointment_payment_summary,
    record_payment,
    record_refund,
    set_appointment_discount,
)
from app.services.section_access import section_access_allows

router = APIRouter()


def _can_refund_appointment(access: WorkspaceAccess) -> bool:
    return section_access_allows(
        access.membership,
        workspace_id=access.workspace.id,
        user_id=access.user.id,
        section="administration",
    )


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _vault_refund_conflict(exc: DBAPIError) -> HTTPException | None:
    message = str(exc.orig)
    if "VAULT_REFUND_REQUIRES_APPROVED_FUNDING" in message:
        return _conflict(
            "رصيد الكاش في خزنة الريسبشن غير كافٍ لتنفيذ المرتجع بعد التوريد. "
            "لم يُسجّل المرتجع؛ يلزم توفر كاش كافٍ أو قرار من المدير بشأن تمويله."
        )
    if "VAULT_REFUND_SOURCE_MISMATCH" in message:
        return _conflict("تعذر إثبات مصدر تمويل المرتجع. لم يُسجّل أي مرتجع نقدي.")
    return None


@router.get("/appointments/{appointment_id}", response_model=AppointmentPaymentSummaryRead)
def appointment_payment_summary(
    appointment_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    db: Annotated[Session, Depends(get_db)],
) -> AppointmentPaymentSummaryRead:
    try:
        return get_appointment_payment_summary(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            can_refund=_can_refund_appointment(access),
        )
    except PaymentOperationNotFound as exc:
        raise _not_found(str(exc)) from exc


@router.post(
    "/appointments/{appointment_id}/payments",
    response_model=AppointmentPaymentSummaryRead,
    status_code=status.HTTP_201_CREATED,
)
def create_payment(
    appointment_id: UUID,
    payload: PaymentCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128),
    ] = None,
) -> AppointmentPaymentSummaryRead:
    try:
        record_payment(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            amount_minor=payload.amount_minor,
            payment_method=payload.payment_method,
            discount_minor=payload.discount_minor,
            external_reference=payload.external_reference,
            created_by_user_id=access.user.id,
            idempotency_key=idempotency_key,
        )
        db.commit()
    except PaymentOperationNotFound as exc:
        db.rollback()
        raise _not_found(str(exc)) from exc
    except PaymentOperationError as exc:
        db.rollback()
        raise _conflict(str(exc)) from exc
    except DBAPIError as exc:
        db.rollback()
        vault_conflict = _vault_refund_conflict(exc)
        if vault_conflict is not None:
            raise vault_conflict from exc
        raise

    return get_appointment_payment_summary(
        db,
        workspace_id=access.workspace.id,
        appointment_id=appointment_id,
        can_refund=_can_refund_appointment(access),
    )


@router.post(
    "/appointments/{appointment_id}/checkout",
    response_model=AppointmentPaymentSummaryRead,
)
def checkout_appointment_payment(
    appointment_id: UUID,
    payload: AppointmentCheckoutCreate,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128),
    ] = None,
) -> AppointmentPaymentSummaryRead:
    try:
        checkout_appointment(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            amount_minor=payload.amount_minor,
            payment_method=payload.payment_method,
            discount_minor=payload.discount_minor,
            external_reference=payload.external_reference,
            pulse_mode=payload.pulse_mode,
            pulse_pack_offer_id=payload.pulse_pack_offer_id,
            created_by_user_id=access.user.id,
            idempotency_key=idempotency_key,
        )
        db.commit()
    except AppointmentCheckoutError as exc:
        db.rollback()
        detail = str(exc)
        if detail == "Appointment not found.":
            raise _not_found(detail) from exc
        raise _conflict(detail) from exc
    except Exception:
        # No partial payment, pulse purchase, usage, history or appointment
        # status survives a failed flush/commit.
        db.rollback()
        raise

    return get_appointment_payment_summary(
        db,
        workspace_id=access.workspace.id,
        appointment_id=appointment_id,
        can_refund=_can_refund_appointment(access),
    )


@router.put(
    "/appointments/{appointment_id}/discount",
    response_model=AppointmentPaymentSummaryRead,
)
def update_appointment_discount(
    appointment_id: UUID,
    payload: AppointmentDiscountUpdate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    db: Annotated[Session, Depends(get_db)],
) -> AppointmentPaymentSummaryRead:
    try:
        set_appointment_discount(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            discount_minor=payload.discount_minor,
            created_by_user_id=access.user.id,
        )
        db.commit()
    except PaymentOperationNotFound as exc:
        db.rollback()
        raise _not_found(str(exc)) from exc
    except PaymentOperationError as exc:
        db.rollback()
        raise _conflict(str(exc)) from exc
    return get_appointment_payment_summary(
        db,
        workspace_id=access.workspace.id,
        appointment_id=appointment_id,
        can_refund=_can_refund_appointment(access),
    )


@router.post(
    "/appointments/{appointment_id}/refunds",
    response_model=AppointmentPaymentSummaryRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_section_access("administration"))],
)
def create_refund(
    appointment_id: UUID,
    payload: RefundCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128),
    ] = None,
) -> AppointmentPaymentSummaryRead:
    try:
        record_refund(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            payment_transaction_id=payload.payment_transaction_id,
            amount_minor=payload.amount_minor,
            reason=payload.reason,
            created_by_user_id=access.user.id,
            idempotency_key=idempotency_key,
        )
        db.commit()
    except PaymentOperationNotFound as exc:
        db.rollback()
        raise _not_found(str(exc)) from exc
    except PaymentOperationError as exc:
        db.rollback()
        raise _conflict(str(exc)) from exc
    except DBAPIError as exc:
        db.rollback()
        vault_conflict = _vault_refund_conflict(exc)
        if vault_conflict is not None:
            raise vault_conflict from exc
        raise

    return get_appointment_payment_summary(
        db,
        workspace_id=access.workspace.id,
        appointment_id=appointment_id,
        can_refund=_can_refund_appointment(access),
    )
