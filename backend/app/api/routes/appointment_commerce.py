from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess, get_workspace_reader
from app.database.session import get_db
from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    require_tia_workspace_domain_write,
)
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.schemas.patient_packages import PatientPackageRead
from app.services.appointment_commerce import (
    AppointmentCommerceError,
    AppointmentCommerceNotFound,
    add_additional_service,
    list_additional_services,
    purchase_package_for_additional_service,
    purchase_package_for_appointment,
    remove_additional_service,
)
from app.services.patient_packages import package_read

router = APIRouter()


class AdditionalServiceCreate(BaseModel):
    service_id: UUID
    laser_device_key: str | None = None
    pulse_mode: Literal["none", "use_balance", "purchase_pack", "overage"] = "none"
    pulses_used: int | None = Field(default=None, gt=0, le=10_000_000)
    pulse_pack_offer_id: UUID | None = None

    @model_validator(mode="after")
    def validate_pulse_billing(self) -> AdditionalServiceCreate:
        if self.pulse_mode == "none":
            if self.pulse_pack_offer_id is not None:
                raise ValueError("pulse_pack_offer_id requires pulse billing.")
            return self
        if self.pulses_used is None:
            raise ValueError("pulses_used is required when using pulse billing.")
        if self.pulse_mode == "purchase_pack" and self.pulse_pack_offer_id is None:
            raise ValueError("Select a pulse pack for purchase_pack billing.")
        if self.pulse_mode != "purchase_pack" and self.pulse_pack_offer_id is not None:
            raise ValueError("pulse_pack_offer_id is only valid with purchase_pack billing.")
        return self


class AdditionalServiceRead(BaseModel):
    id: UUID
    appointment_id: UUID
    service_id: UUID
    service_name: str
    unit_price_minor: int
    currency: str
    laser_device_key: str | None
    laser_device_name: str | None
    patient_package_id: UUID | None
    billing_context: str
    laser_pulses_used: int | None
    pulse_resolution: str | None
    pulse_resolution_pulse_pack_id: UUID | None
    pulse_overage_unit_price_minor: int | None
    pulse_overage_charge_minor: int

    model_config = ConfigDict(from_attributes=True)


class AppointmentPackagePurchase(BaseModel):
    offer_id: UUID


def _require_local_write(db: Session, workspace_id: UUID) -> None:
    try:
        require_tia_workspace_domain_write(
            db, workspace_id=workspace_id, domain="appointments"
        )
    except ClinicIntegrationAuthorityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


def _raise(exc: Exception) -> None:
    code = (
        status.HTTP_404_NOT_FOUND
        if isinstance(exc, AppointmentCommerceNotFound)
        else status.HTTP_409_CONFLICT
    )
    raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.get(
    "/appointments/{appointment_id}/additional-services",
    response_model=list[AdditionalServiceRead],
)
def get_additional_services(
    appointment_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[AppointmentAdditionalService]:
    try:
        return list_additional_services(
            db, workspace_id=access.workspace.id, appointment_id=appointment_id
        )
    except AppointmentCommerceError as exc:
        _raise(exc)


@router.post(
    "/appointments/{appointment_id}/additional-services",
    response_model=AdditionalServiceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_additional_service(
    appointment_id: UUID,
    payload: AdditionalServiceCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=128)
    ] = None,
) -> AppointmentAdditionalService:
    _require_local_write(db, access.workspace.id)
    try:
        line = add_additional_service(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            service_id=payload.service_id,
            laser_device_key=payload.laser_device_key,
            created_by_user_id=access.user.id,
            pulse_mode=payload.pulse_mode,
            pulses_used=payload.pulses_used,
            pulse_pack_offer_id=payload.pulse_pack_offer_id,
            idempotency_key=idempotency_key,
        )
        db.commit()
        db.refresh(line)
        return line
    except AppointmentCommerceError as exc:
        db.rollback()
        _raise(exc)


@router.delete(
    "/appointments/{appointment_id}/additional-services/{line_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_additional_service(
    appointment_id: UUID,
    line_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    _require_local_write(db, access.workspace.id)
    try:
        remove_additional_service(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            line_id=line_id,
            changed_by_user_id=access.user.id,
        )
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except AppointmentCommerceError as exc:
        db.rollback()
        _raise(exc)


@router.post(
    "/appointments/{appointment_id}/package-offer",
    response_model=PatientPackageRead,
    status_code=status.HTTP_201_CREATED,
)
def buy_package_from_appointment(
    appointment_id: UUID,
    payload: AppointmentPackagePurchase,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=128)
    ] = None,
) -> PatientPackageRead:
    _require_local_write(db, access.workspace.id)
    try:
        package = purchase_package_for_appointment(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            offer_id=payload.offer_id,
            created_by_user_id=access.user.id,
            idempotency_key=idempotency_key,
        )
        db.commit()
        db.refresh(package)
        return package_read(db, package, include_financials=True)
    except AppointmentCommerceError as exc:
        db.rollback()
        _raise(exc)


@router.post(
    "/appointments/{appointment_id}/additional-services/{line_id}/package-offer",
    response_model=PatientPackageRead,
    status_code=status.HTTP_201_CREATED,
)
def buy_package_for_additional_service(
    appointment_id: UUID,
    line_id: UUID,
    payload: AppointmentPackagePurchase,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=128)
    ] = None,
) -> PatientPackageRead:
    _require_local_write(db, access.workspace.id)
    try:
        package = purchase_package_for_additional_service(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            line_id=line_id,
            offer_id=payload.offer_id,
            created_by_user_id=access.user.id,
            idempotency_key=idempotency_key,
        )
        db.commit()
        db.refresh(package)
        return package_read(db, package, include_financials=True)
    except AppointmentCommerceError as exc:
        db.rollback()
        _raise(exc)
