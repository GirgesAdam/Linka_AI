from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.clinic_inventory import (
    LASER_DEVICE_KEYS,
    LASER_DEVICE_NAMES,
    AppointmentProductLine,
    ClinicLaserDevice,
    ClinicProduct,
    InventoryItem,
    InventoryUsage,
    ServiceDevicePrice,
)
from app.models.service import Service
from app.schemas.inventory import (
    AppointmentProductLineRead,
    ClinicLaserDeviceRead,
    InventoryItemRead,
    LaserDevicePriceRead,
)


class InventoryOperationError(ValueError):
    pass


class InventoryNotFound(InventoryOperationError):
    pass


class DeviceServiceCompatibilityError(InventoryOperationError):
    """A known laser device is not configured for the requested service."""


def is_laser_service(service: Service) -> bool:
    """Use the explicit service capability flag, never the display name, as truth."""
    return bool(getattr(service, "requires_laser_device", False))


def _device_read(row: ClinicLaserDevice) -> ClinicLaserDeviceRead:
    return ClinicLaserDeviceRead.model_validate(row)


def ensure_legacy_laser_devices(
    db: Session,
    *,
    workspace_id: UUID,
) -> list[ClinicLaserDevice]:
    rows = list(
        db.scalars(
            select(ClinicLaserDevice).where(
                ClinicLaserDevice.workspace_id == workspace_id,
            )
        )
    )
    if rows:
        return rows

    rows = [
        ClinicLaserDevice(
            workspace_id=workspace_id,
            device_key=device_key,
            name=device_name,
            is_active=True,
        )
        for device_key, device_name in LASER_DEVICE_NAMES.items()
    ]
    db.add_all(rows)
    db.flush()
    return rows


def list_clinic_laser_devices(
    db: Session,
    *,
    workspace_id: UUID,
    active_only: bool = False,
) -> list[ClinicLaserDeviceRead]:
    ensure_legacy_laser_devices(db, workspace_id=workspace_id)
    stmt = select(ClinicLaserDevice).where(
        ClinicLaserDevice.workspace_id == workspace_id,
    )
    if active_only:
        stmt = stmt.where(ClinicLaserDevice.is_active.is_(True))
    rows = list(db.scalars(stmt.order_by(ClinicLaserDevice.name, ClinicLaserDevice.id)))
    return [_device_read(row) for row in rows]


def create_clinic_laser_device(
    db: Session,
    *,
    workspace_id: UUID,
    name: str,
) -> ClinicLaserDeviceRead:
    normalized_name = " ".join(name.split())
    ensure_legacy_laser_devices(db, workspace_id=workspace_id)
    rows = list(
        db.scalars(
            select(ClinicLaserDevice).where(
                ClinicLaserDevice.workspace_id == workspace_id,
            )
        )
    )
    same_name = next(
        (row for row in rows if row.name.strip().casefold() == normalized_name.casefold()),
        None,
    )
    if same_name is not None:
        same_name.is_active = True
        same_name.name = normalized_name
        db.flush()
        return _device_read(same_name)

    row = ClinicLaserDevice(
        workspace_id=workspace_id,
        device_key=f"device_{uuid4().hex[:16]}",
        name=normalized_name,
        is_active=True,
    )
    db.add(row)
    db.flush()
    return _device_read(row)


def update_clinic_laser_device(
    db: Session,
    *,
    workspace_id: UUID,
    device_id: UUID,
    name: str | None,
    is_active: bool | None,
) -> ClinicLaserDeviceRead:
    row = db.scalar(
        select(ClinicLaserDevice)
        .where(
            ClinicLaserDevice.workspace_id == workspace_id,
            ClinicLaserDevice.id == device_id,
        )
        .with_for_update()
    )
    if row is None:
        raise InventoryNotFound("Laser device not found.")

    if name is not None:
        normalized_name = " ".join(name.split())
        siblings = list(
            db.scalars(
                select(ClinicLaserDevice).where(
                    ClinicLaserDevice.workspace_id == workspace_id,
                    ClinicLaserDevice.id != row.id,
                )
            )
        )
        if any(
            sibling.name.strip().casefold() == normalized_name.casefold()
            for sibling in siblings
        ):
            raise InventoryOperationError("A laser device with this name already exists.")
        row.name = normalized_name
    if is_active is not None:
        row.is_active = is_active

    db.flush()
    return _device_read(row)


def list_laser_device_prices(db: Session, *, workspace_id: UUID) -> list[LaserDevicePriceRead]:
    services = list(
        db.scalars(
            select(Service).where(
                Service.workspace_id == workspace_id,
                Service.is_active.is_(True),
                Service.requires_laser_device.is_(True),
            ).order_by(Service.name)
        )
    )
    if not services:
        return []
    rows = list(
        db.scalars(
            select(ServiceDevicePrice).where(
                ServiceDevicePrice.workspace_id == workspace_id,
                ServiceDevicePrice.service_id.in_([service.id for service in services]),
                ServiceDevicePrice.is_active.is_(True),
            )
        )
    )
    by_key = {(row.service_id, row.device_key): row for row in rows}
    result: list[LaserDevicePriceRead] = []
    for service in services:
        for device_key in LASER_DEVICE_KEYS:
            row = by_key.get((service.id, device_key))
            result.append(
                LaserDevicePriceRead(
                    service_id=service.id,
                    service_name=service.name,
                    device_key=device_key,
                    device_name=LASER_DEVICE_NAMES[device_key],
                    price_minor=(int(row.price_minor) if row and row.price_minor is not None else None),
                    duration_minutes=(int(row.duration_minutes) if row else None),
                    currency=(row.currency if row else service.currency or "EGP"),
                    configured=bool(
                        row
                        and row.price_minor is not None
                        and row.duration_minutes is not None
                    ),
                )
            )
    return result


def upsert_laser_device_price(
    db: Session,
    *,
    workspace_id: UUID,
    service_id: UUID,
    device_key: str,
    price_minor: int,
    duration_minutes: int | None,
    currency: str,
) -> ServiceDevicePrice:
    if device_key not in LASER_DEVICE_NAMES:
        raise InventoryOperationError("Unsupported laser device.")
    if duration_minutes is not None and (
        duration_minutes <= 0 or duration_minutes > 1440
    ):
        raise InventoryOperationError(
            "Laser device duration must be between 1 and 1440 minutes."
        )
    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace_id,
            Service.id == service_id,
            Service.is_active.is_(True),
        )
    )
    if service is None:
        raise InventoryNotFound("Service not found.")
    if not is_laser_service(service):
        raise InventoryOperationError("Device pricing is only available for services that require a laser device.")
    row = db.scalar(
        select(ServiceDevicePrice).where(
            ServiceDevicePrice.workspace_id == workspace_id,
            ServiceDevicePrice.service_id == service_id,
            ServiceDevicePrice.device_key == device_key,
        )
    )
    resolved_duration_minutes = (
        int(duration_minutes)
        if duration_minutes is not None
        else (
            int(row.duration_minutes)
            if row is not None and row.duration_minutes is not None
            else int(service.duration_minutes)
        )
    )
    if row is None:
        row = ServiceDevicePrice(
            workspace_id=workspace_id,
            service_id=service_id,
            device_key=device_key,
            device_name=LASER_DEVICE_NAMES[device_key],
            price_minor=price_minor,
            duration_minutes=resolved_duration_minutes,
            currency=currency.upper(),
            is_active=True,
        )
        db.add(row)
    else:
        row.device_name = LASER_DEVICE_NAMES[device_key]
        row.price_minor = price_minor
        row.duration_minutes = resolved_duration_minutes
        row.currency = currency.upper()
        row.is_active = True
    db.flush()
    return row


def configured_device_price(
    db: Session,
    *,
    workspace_id: UUID,
    service_id: UUID,
    device_key: str | None,
) -> ServiceDevicePrice | None:
    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace_id,
            Service.id == service_id,
            Service.is_active.is_(True),
        )
    )
    if service is None:
        raise InventoryNotFound("Service not found.")
    if not is_laser_service(service):
        return None
    if not device_key:
        raise InventoryOperationError("Laser device choice is required.")
    if device_key not in LASER_DEVICE_NAMES:
        raise InventoryOperationError("Unsupported laser device.")
    row = db.scalar(
        select(ServiceDevicePrice).where(
            ServiceDevicePrice.workspace_id == workspace_id,
            ServiceDevicePrice.service_id == service_id,
            ServiceDevicePrice.device_key == device_key,
            ServiceDevicePrice.is_active.is_(True),
        )
    )
    if row is None or row.price_minor is None or row.duration_minutes is None:
        raise DeviceServiceCompatibilityError(
            f"Price and duration for {LASER_DEVICE_NAMES[device_key]} are not configured "
            "for this laser service."
        )
    return row


def list_products(db: Session, *, workspace_id: UUID, active_only: bool = True) -> list[ClinicProduct]:
    stmt = select(ClinicProduct).where(ClinicProduct.workspace_id == workspace_id)
    if active_only:
        stmt = stmt.where(ClinicProduct.is_active.is_(True))
    return list(db.scalars(stmt.order_by(ClinicProduct.name)))


def create_product(
    db: Session,
    *,
    workspace_id: UUID,
    name: str,
    description: str | None,
    quantity_on_hand: int,
) -> ClinicProduct:
    existing = db.scalar(
        select(ClinicProduct).where(
            ClinicProduct.workspace_id == workspace_id,
            ClinicProduct.name == name,
        )
    )
    if existing is not None:
        existing.is_active = True
        existing.quantity_on_hand = quantity_on_hand
        if description is not None:
            existing.description = description
        db.flush()
        return existing
    row = ClinicProduct(
        workspace_id=workspace_id,
        name=name,
        description=description,
        quantity_on_hand=quantity_on_hand,
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def set_product_quantity(
    db: Session,
    *,
    workspace_id: UUID,
    product_id: UUID,
    quantity_on_hand: int,
) -> ClinicProduct:
    row = db.scalar(
        select(ClinicProduct).where(
            ClinicProduct.workspace_id == workspace_id,
            ClinicProduct.id == product_id,
            ClinicProduct.is_active.is_(True),
        ).with_for_update()
    )
    if row is None:
        raise InventoryNotFound("Product not found.")
    row.quantity_on_hand = quantity_on_hand
    db.flush()
    return row


def list_appointment_products(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
) -> list[AppointmentProductLineRead]:
    rows = list(
        db.scalars(
            select(AppointmentProductLine).where(
                AppointmentProductLine.workspace_id == workspace_id,
                AppointmentProductLine.appointment_id == appointment_id,
            ).order_by(AppointmentProductLine.created_at, AppointmentProductLine.id)
        )
    )
    return [
        AppointmentProductLineRead(
            id=row.id,
            appointment_id=row.appointment_id,
            product_id=row.product_id,
            product_name=row.product_name,
            quantity=row.quantity,
            unit_price_minor=row.unit_price_minor,
            currency=row.currency,
            total_minor=int(row.quantity) * int(row.unit_price_minor),
        )
        for row in rows
    ]


def appointment_products_total_minor(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
) -> int:
    return sum(
        int(row.quantity) * int(row.unit_price_minor)
        for row in db.scalars(
            select(AppointmentProductLine).where(
                AppointmentProductLine.workspace_id == workspace_id,
                AppointmentProductLine.appointment_id == appointment_id,
            )
        )
    )


def add_appointment_product(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    product_id: UUID,
    quantity: int,
    unit_price_minor: int,
    created_by_user_id: UUID | None,
) -> AppointmentProductLine:
    appointment = db.scalar(
        select(Appointment).where(
            Appointment.workspace_id == workspace_id,
            Appointment.id == appointment_id,
        )
    )
    if appointment is None:
        raise InventoryNotFound("Appointment not found.")
    if appointment.status in {"cancelled", "no_show", "rescheduled"}:
        raise InventoryOperationError("Products cannot be added to this appointment status.")
    product = db.scalar(
        select(ClinicProduct).where(
            ClinicProduct.workspace_id == workspace_id,
            ClinicProduct.id == product_id,
            ClinicProduct.is_active.is_(True),
        ).with_for_update()
    )
    if product is None:
        raise InventoryNotFound("Product not found.")
    if int(product.quantity_on_hand) < quantity:
        raise InventoryOperationError("Product quantity exceeds the available stock.")
    product.quantity_on_hand = int(product.quantity_on_hand) - quantity
    row = AppointmentProductLine(
        workspace_id=workspace_id,
        appointment_id=appointment_id,
        product_id=product.id,
        product_name=product.name,
        quantity=quantity,
        unit_price_minor=unit_price_minor,
        currency=appointment.currency,
        created_by_user_id=created_by_user_id,
    )
    db.add(row)
    db.flush()
    return row


def delete_appointment_product(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    line_id: UUID,
) -> None:
    row = db.scalar(
        select(AppointmentProductLine).where(
            AppointmentProductLine.workspace_id == workspace_id,
            AppointmentProductLine.appointment_id == appointment_id,
            AppointmentProductLine.id == line_id,
        )
    )
    if row is None:
        raise InventoryNotFound("Appointment product line not found.")
    product = db.scalar(
        select(ClinicProduct).where(
            ClinicProduct.workspace_id == workspace_id,
            ClinicProduct.id == row.product_id,
        ).with_for_update()
    )
    if product is not None:
        product.quantity_on_hand = int(product.quantity_on_hand) + int(row.quantity)
    db.delete(row)
    db.flush()


def _inventory_read(row: InventoryItem) -> InventoryItemRead:
    return InventoryItemRead(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        category=row.category,
        quantity_ml=Decimal(row.quantity_ml),
        low_stock_threshold_ml=(Decimal(row.low_stock_threshold_ml) if row.low_stock_threshold_ml is not None else None),
        notes=row.notes,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def list_inventory_items(db: Session, *, workspace_id: UUID) -> list[InventoryItemRead]:
    rows = list(
        db.scalars(
            select(InventoryItem).where(
                InventoryItem.workspace_id == workspace_id,
                InventoryItem.is_active.is_(True),
            ).order_by(InventoryItem.name)
        )
    )
    return [_inventory_read(row) for row in rows]


def create_inventory_item(
    db: Session,
    *,
    workspace_id: UUID,
    name: str,
    quantity_ml: Decimal,
    low_stock_threshold_ml: Decimal | None,
    notes: str | None,
) -> InventoryItemRead:
    row = InventoryItem(
        workspace_id=workspace_id,
        name=" ".join(name.split()),
        category="injectable",
        quantity_ml=quantity_ml,
        # Legacy storage keeps this non-null column; new inventory semantics are mL-only.
        concentration_mg_per_ml=Decimal("1"),
        low_stock_threshold_ml=low_stock_threshold_ml,
        notes=notes,
        is_active=True,
    )
    db.add(row)
    db.flush()
    return _inventory_read(row)


def add_inventory_stock(
    db: Session,
    *,
    workspace_id: UUID,
    item_id: UUID,
    quantity_ml: Decimal,
) -> InventoryItemRead:
    row = db.scalar(
        select(InventoryItem).where(
            InventoryItem.workspace_id == workspace_id,
            InventoryItem.id == item_id,
            InventoryItem.is_active.is_(True),
        ).with_for_update()
    )
    if row is None:
        raise InventoryNotFound("Inventory item not found.")
    row.quantity_ml = Decimal(row.quantity_ml) + quantity_ml
    db.flush()
    return _inventory_read(row)


def record_inventory_usage(
    db: Session,
    *,
    workspace_id: UUID,
    item_id: UUID,
    used_ml: Decimal,
    note: str | None,
    created_by_user_id: UUID | None,
) -> tuple[InventoryUsage, InventoryItemRead]:
    row = db.scalar(
        select(InventoryItem).where(
            InventoryItem.workspace_id == workspace_id,
            InventoryItem.id == item_id,
            InventoryItem.is_active.is_(True),
        ).with_for_update()
    )
    if row is None:
        raise InventoryNotFound("Inventory item not found.")
    if used_ml > Decimal(row.quantity_ml):
        raise InventoryOperationError("Used amount exceeds the remaining stock.")
    row.quantity_ml = Decimal(row.quantity_ml) - used_ml
    usage = InventoryUsage(
        workspace_id=workspace_id,
        inventory_item_id=row.id,
        appointment_id=None,
        # Keep legacy mg column populated without exposing concentration semantics.
        used_mg=used_ml,
        used_ml=used_ml,
        note=note,
        created_by_user_id=created_by_user_id,
    )
    db.add(usage)
    db.flush()
    return usage, _inventory_read(row)
