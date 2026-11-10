from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies.section_access import require_section_access
from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.database.session import get_db
from app.schemas.package_offers import (
    PatientPackageOfferPurchase,
    ServicePackageOfferRead,
    ServicePackageOfferUpsert,
)
from app.schemas.patient_packages import PatientPackageRead
from app.services.package_offers import (
    PackageOfferError,
    PackageOfferNotFound,
    list_package_offers,
    purchase_package_offer,
    upsert_package_offer,
)
from app.services.patient_packages import PackageOperationError, package_read

router = APIRouter()


def _raise(exc: Exception) -> None:
    code = status.HTTP_404_NOT_FOUND if isinstance(exc, PackageOfferNotFound) else status.HTTP_409_CONFLICT
    raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.get("/package-offers", response_model=list[ServicePackageOfferRead])
def package_offers(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    db: Annotated[Session, Depends(get_db)],
    service_id: UUID | None = None,
    active_only: bool = False,
) -> list[ServicePackageOfferRead]:
    return list_package_offers(
        db,
        workspace_id=access.workspace.id,
        service_id=service_id,
        active_only=active_only,
    )


@router.put("/package-offers", response_model=list[ServicePackageOfferRead])
def set_package_offer(
    payload: ServicePackageOfferUpsert,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("clinic_operations"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[ServicePackageOfferRead]:
    try:
        upsert_package_offer(
            db,
            workspace_id=access.workspace.id,
            service_id=payload.service_id,
            device_key=payload.device_key,
            sessions_count=payload.sessions_count,
            price_minor=payload.price_minor,
            currency=payload.currency,
            is_active=payload.is_active,
        )
        db.commit()
    except PackageOfferError as exc:
        db.rollback()
        _raise(exc)
    return list_package_offers(db, workspace_id=access.workspace.id)


@router.post(
    "/package-offers/purchase",
    response_model=PatientPackageRead,
    status_code=status.HTTP_201_CREATED,
)
def buy_package_offer(
    payload: PatientPackageOfferPurchase,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> PatientPackageRead:
    try:
        package = purchase_package_offer(
            db,
            workspace_id=access.workspace.id,
            patient_id=payload.patient_id,
            offer_id=payload.offer_id,
            amount_paid_minor=payload.amount_paid_minor,
            payment_method=payload.payment_method,
            created_by_user_id=access.user.id,
            external_reference=payload.external_reference,
            idempotency_key=idempotency_key,
            actor_type="staff",
        )
        db.commit()
        db.refresh(package)
        return package_read(db, package, include_financials=True)
    except (PackageOfferError, PackageOperationError) as exc:
        db.rollback()
        _raise(exc)
