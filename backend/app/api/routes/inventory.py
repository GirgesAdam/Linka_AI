from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess, get_workspace_admin, get_workspace_reader
from app.database.session import get_db
from app.models.appointment import Appointment
from app.schemas.inventory import (
    AppointmentProductCreate,
    AppointmentProductLineRead,
    ClinicLaserDeviceCreate,
    ClinicLaserDeviceRead,
    ClinicLaserDeviceUpdate,
    ClinicProductCreate,
    ClinicProductQuantityUpdate,
    ClinicProductRead,
    InventoryItemAdjust,
    InventoryItemCreate,
    InventoryItemRead,
    InventoryUsageCreate,
    InventoryUsageRead,
    LaserDevicePriceRead,
    LaserDevicePriceUpsert,
)
from app.services.inventory import (
    InventoryNotFound,
    InventoryOperationError,
    add_appointment_product,
    add_inventory_stock,
    create_clinic_laser_device,
    create_inventory_item,
    create_product,
    deactivate_laser_device_price,
    delete_appointment_product,
    list_appointment_products,
    list_clinic_laser_devices,
    list_inventory_items,
    list_laser_device_prices,
    list_products,
    record_inventory_usage,
    set_product_quantity,
    update_clinic_laser_device,
    upsert_laser_device_price,
)
from app.services.payments import refresh_appointment_payment_snapshots

router = APIRouter()


def _raise(exc: Exception) -> None:
    code = status.HTTP_404_NOT_FOUND if isinstance(exc, InventoryNotFound) else status.HTTP_409_CONFLICT
    raise HTTPException(status_code=code, detail=str(exc)) from exc


def _locked_appointment(db: Session, *, workspace_id: UUID, appointment_id: UUID) -> Appointment:
    appointment = db.scalar(
        select(Appointment).where(
            Appointment.workspace_id == workspace_id,
            Appointment.id == appointment_id,
        ).with_for_update()
    )
    if appointment is None:
        raise InventoryNotFound("Appointment not found.")
    return appointment


@router.get("/products", response_model=list[ClinicProductRead])
def products(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[ClinicProductRead]:
    return [ClinicProductRead.model_validate(row) for row in list_products(db, workspace_id=access.workspace.id)]


@router.post("/products", response_model=ClinicProductRead, status_code=status.HTTP_201_CREATED)
def add_product(
    payload: ClinicProductCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicProductRead:
    row = create_product(
        db,
        workspace_id=access.workspace.id,
        name=payload.name,
        description=payload.description,
        quantity_on_hand=payload.quantity_on_hand,
    )
    db.commit()
    db.refresh(row)
    return ClinicProductRead.model_validate(row)


@router.put("/products/{product_id}/quantity", response_model=ClinicProductRead)
def update_product_quantity(
    product_id: UUID,
    payload: ClinicProductQuantityUpdate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicProductRead:
    try:
        row = set_product_quantity(
            db,
            workspace_id=access.workspace.id,
            product_id=product_id,
            quantity_on_hand=payload.quantity_on_hand,
        )
        db.commit()
        db.refresh(row)
        return ClinicProductRead.model_validate(row)
    except (InventoryNotFound, InventoryOperationError) as exc:
        db.rollback()
        _raise(exc)


@router.get("/appointments/{appointment_id}/products", response_model=list[AppointmentProductLineRead])
def appointment_products(
    appointment_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[AppointmentProductLineRead]:
    return list_appointment_products(db, workspace_id=access.workspace.id, appointment_id=appointment_id)


@router.post(
    "/appointments/{appointment_id}/products",
    response_model=list[AppointmentProductLineRead],
    status_code=status.HTTP_201_CREATED,
)
def add_product_to_appointment(
    appointment_id: UUID,
    payload: AppointmentProductCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[AppointmentProductLineRead]:
    try:
        appointment = _locked_appointment(
            db, workspace_id=access.workspace.id, appointment_id=appointment_id
        )
        line = add_appointment_product(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            product_id=payload.product_id,
            quantity=payload.quantity,
            unit_price_minor=payload.unit_price_minor,
            created_by_user_id=access.user.id,
        )
        appointment.price_minor = int(appointment.price_minor) + int(line.quantity) * int(line.unit_price_minor)
        refresh_appointment_payment_snapshots(
            db,
            workspace_id=access.workspace.id,
            appointment_ids={appointment_id},
        )
        db.commit()
    except (InventoryNotFound, InventoryOperationError) as exc:
        db.rollback()
        _raise(exc)
    return list_appointment_products(db, workspace_id=access.workspace.id, appointment_id=appointment_id)


@router.delete("/appointments/{appointment_id}/products/{line_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_product_from_appointment(
    appointment_id: UUID,
    line_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    try:
        existing = list_appointment_products(
            db, workspace_id=access.workspace.id, appointment_id=appointment_id
        )
        line = next((item for item in existing if item.id == line_id), None)
        if line is None:
            raise InventoryNotFound("Appointment product line not found.")
        appointment = _locked_appointment(
            db, workspace_id=access.workspace.id, appointment_id=appointment_id
        )
        delete_appointment_product(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            line_id=line_id,
        )
        appointment.price_minor = max(int(appointment.price_minor) - int(line.total_minor), 0)
        refresh_appointment_payment_snapshots(
            db,
            workspace_id=access.workspace.id,
            appointment_ids={appointment_id},
        )
        db.commit()
    except (InventoryNotFound, InventoryOperationError) as exc:
        db.rollback()
        _raise(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/items", response_model=list[InventoryItemRead])
def inventory_items(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[InventoryItemRead]:
    return list_inventory_items(db, workspace_id=access.workspace.id)


@router.post("/items", response_model=InventoryItemRead, status_code=status.HTTP_201_CREATED)
def add_inventory_item(
    payload: InventoryItemCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> InventoryItemRead:
    try:
        result = create_inventory_item(
            db,
            workspace_id=access.workspace.id,
            name=payload.name,
            quantity_ml=payload.quantity_ml,
            low_stock_threshold_ml=payload.low_stock_threshold_ml,
            notes=payload.notes,
        )
        db.commit()
        return result
    except InventoryOperationError as exc:
        db.rollback()
        _raise(exc)


@router.post("/items/{item_id}/stock", response_model=InventoryItemRead)
def increase_inventory_stock(
    item_id: UUID,
    payload: InventoryItemAdjust,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> InventoryItemRead:
    try:
        result = add_inventory_stock(
            db,
            workspace_id=access.workspace.id,
            item_id=item_id,
            quantity_ml=payload.quantity_ml,
        )
        db.commit()
        return result
    except (InventoryNotFound, InventoryOperationError) as exc:
        db.rollback()
        _raise(exc)


@router.post("/items/{item_id}/usage", response_model=InventoryUsageRead, status_code=status.HTTP_201_CREATED)
def use_inventory_item(
    item_id: UUID,
    payload: InventoryUsageCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> InventoryUsageRead:
    try:
        usage, _item = record_inventory_usage(
            db,
            workspace_id=access.workspace.id,
            item_id=item_id,
            used_ml=payload.used_ml,
            note=payload.note,
            created_by_user_id=access.user.id,
        )
        db.commit()
        db.refresh(usage)
        return InventoryUsageRead(
            id=usage.id,
            inventory_item_id=usage.inventory_item_id,
            used_ml=usage.used_ml,
            note=usage.note,
            created_at=usage.created_at,
        )
    except (InventoryNotFound, InventoryOperationError) as exc:
        db.rollback()
        _raise(exc)


@router.get("/laser-devices", response_model=list[ClinicLaserDeviceRead])
def laser_devices(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[ClinicLaserDeviceRead]:
    return list_clinic_laser_devices(
        db,
        workspace_id=access.workspace.id,
        active_only=False,
    )


@router.post(
    "/laser-devices",
    response_model=ClinicLaserDeviceRead,
    status_code=status.HTTP_201_CREATED,
)
def add_laser_device(
    payload: ClinicLaserDeviceCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicLaserDeviceRead:
    try:
        result = create_clinic_laser_device(
            db,
            workspace_id=access.workspace.id,
            name=payload.name,
        )
        db.commit()
        return result
    except InventoryOperationError as exc:
        db.rollback()
        _raise(exc)


@router.patch("/laser-devices/{device_id}", response_model=ClinicLaserDeviceRead)
def edit_laser_device(
    device_id: UUID,
    payload: ClinicLaserDeviceUpdate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicLaserDeviceRead:
    try:
        result = update_clinic_laser_device(
            db,
            workspace_id=access.workspace.id,
            device_id=device_id,
            name=payload.name,
            is_active=payload.is_active,
        )
        db.commit()
        return result
    except (InventoryNotFound, InventoryOperationError) as exc:
        db.rollback()
        _raise(exc)


@router.delete("/laser-prices/{service_id}/{device_key}", status_code=status.HTTP_204_NO_CONTENT)
def remove_laser_price(
    service_id: UUID,
    device_key: str,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    deactivate_laser_device_price(
        db,
        workspace_id=access.workspace.id,
        service_id=service_id,
        device_key=device_key,
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/laser-prices", response_model=list[LaserDevicePriceRead])
def laser_prices(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[LaserDevicePriceRead]:
    return list_laser_device_prices(db, workspace_id=access.workspace.id)


@router.put("/laser-prices", response_model=list[LaserDevicePriceRead])
def set_laser_price(
    payload: LaserDevicePriceUpsert,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[LaserDevicePriceRead]:
    try:
        upsert_laser_device_price(
            db,
            workspace_id=access.workspace.id,
            service_id=payload.service_id,
            device_key=payload.device_key,
            price_minor=payload.price_minor,
            duration_minutes=payload.duration_minutes,
            currency=payload.currency,
        )
        db.commit()
    except (InventoryNotFound, InventoryOperationError) as exc:
        db.rollback()
        _raise(exc)
    return list_laser_device_prices(db, workspace_id=access.workspace.id)
