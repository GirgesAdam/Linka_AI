from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.clinic_inventory import LASER_DEVICE_NAMES
from app.models.service import Service
from app.models.service_package_offer import ServicePackageOffer
from app.schemas.package_offers import ServicePackageOfferRead
from app.services.inventory import InventoryOperationError, configured_device_price
from app.services.patient_packages import create_patient_package


class PackageOfferError(ValueError):
    pass


class PackageOfferNotFound(PackageOfferError):
    pass


def _read_offer(
    db: Session,
    *,
    offer: ServicePackageOffer,
    service: Service | None = None,
) -> ServicePackageOfferRead:
    if service is None:
        service = db.scalar(
            select(Service).where(
                Service.workspace_id == offer.workspace_id,
                Service.id == offer.service_id,
            )
        )
    service_name = service.name if service is not None else "Service"
    if offer.device_key is None:
        standalone = int(service.price_minor or 0) if service is not None else 0
    else:
        try:
            device_price = configured_device_price(
                db,
                workspace_id=offer.workspace_id,
                service_id=offer.service_id,
                device_key=offer.device_key,
            )
            standalone = int(device_price.price_minor or 0) if device_price is not None else 0
        except InventoryOperationError:
            standalone = 0
    full_standalone = standalone * int(offer.sessions_count)
    return ServicePackageOfferRead(
        id=offer.id,
        workspace_id=offer.workspace_id,
        service_id=offer.service_id,
        service_name=service_name,
        device_key=offer.device_key,
        device_name=offer.device_name,
        sessions_count=offer.sessions_count,
        price_minor=int(offer.price_minor),
        currency=offer.currency,
        is_active=bool(offer.is_active),
        standalone_session_price_minor=standalone,
        savings_minor=max(full_standalone - int(offer.price_minor), 0),
        created_at=offer.created_at,
        updated_at=offer.updated_at,
    )


def list_package_offers(
    db: Session,
    *,
    workspace_id: UUID,
    service_id: UUID | None = None,
    active_only: bool = False,
) -> list[ServicePackageOfferRead]:
    stmt = (
        select(ServicePackageOffer, Service)
        .join(
            Service,
            (Service.workspace_id == ServicePackageOffer.workspace_id)
            & (Service.id == ServicePackageOffer.service_id),
        )
        .where(ServicePackageOffer.workspace_id == workspace_id)
    )
    if service_id is not None:
        stmt = stmt.where(ServicePackageOffer.service_id == service_id)
    if active_only:
        stmt = stmt.where(ServicePackageOffer.is_active.is_(True))
    rows = db.execute(
        stmt.order_by(Service.name, ServicePackageOffer.device_key, ServicePackageOffer.sessions_count)
    ).all()
    return [_read_offer(db, offer=offer, service=service) for offer, service in rows]


def upsert_package_offer(
    db: Session,
    *,
    workspace_id: UUID,
    service_id: UUID,
    device_key: str | None,
    sessions_count: int,
    price_minor: int,
    currency: str,
    is_active: bool,
) -> ServicePackageOffer:
    if sessions_count <= 0:
        raise PackageOfferError("Package sessions must be a positive integer.")
    if price_minor < 0:
        raise PackageOfferError("Package price cannot be negative.")
    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace_id,
            Service.id == service_id,
            Service.is_active.is_(True),
        )
    )
    if service is None:
        raise PackageOfferNotFound("Service not found.")

    if service.requires_laser_device:
        if device_key not in LASER_DEVICE_NAMES:
            raise PackageOfferError("A supported laser device is required for this service package.")
        try:
            device_price = configured_device_price(
                db,
                workspace_id=workspace_id,
                service_id=service_id,
                device_key=device_key,
            )
        except InventoryOperationError as exc:
            raise PackageOfferError(str(exc)) from exc
        if device_price is None:
            raise PackageOfferError("Laser device price is required before configuring its packages.")
        device_name = LASER_DEVICE_NAMES[device_key]
        device_predicate = ServicePackageOffer.device_key == device_key
    else:
        if device_key is not None:
            raise PackageOfferError("This service package must not specify a laser device.")
        device_name = None
        device_predicate = ServicePackageOffer.device_key.is_(None)

    row = db.scalar(
        select(ServicePackageOffer).where(
            ServicePackageOffer.workspace_id == workspace_id,
            ServicePackageOffer.service_id == service_id,
            device_predicate,
            ServicePackageOffer.sessions_count == sessions_count,
        )
    )
    if row is None:
        row = ServicePackageOffer(
            workspace_id=workspace_id,
            service_id=service_id,
            device_key=device_key,
            device_name=device_name,
            sessions_count=sessions_count,
            price_minor=price_minor,
            currency=currency.upper(),
            is_active=is_active,
        )
        db.add(row)
    else:
        row.device_name = device_name
        row.price_minor = price_minor
        row.currency = currency.upper()
        row.is_active = is_active
    db.flush()
    return row


def get_active_package_offer(
    db: Session,
    *,
    workspace_id: UUID,
    offer_id: UUID,
    for_update: bool = False,
) -> ServicePackageOffer:
    stmt = select(ServicePackageOffer).where(
        ServicePackageOffer.workspace_id == workspace_id,
        ServicePackageOffer.id == offer_id,
        ServicePackageOffer.is_active.is_(True),
    )
    if for_update:
        stmt = stmt.with_for_update()
    offer = db.scalar(stmt)
    if offer is None:
        raise PackageOfferNotFound("Package offer not found or inactive.")
    return offer


def purchase_package_offer(
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
    actor_type: str = "staff",
    origin_appointment_id: UUID | None = None,
):
    """Create a patient package from one verified offer snapshot.

    Payment amount is recorded but never controls whether package sessions can be
    reserved or consumed. The clinic keeps that operational decision outside Linka.
    """
    offer = get_active_package_offer(
        db,
        workspace_id=workspace_id,
        offer_id=offer_id,
        for_update=True,
    )
    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace_id,
            Service.id == offer.service_id,
            Service.is_active.is_(True),
        )
    )
    if service is None:
        raise PackageOfferNotFound("Package service not found or inactive.")

    if service.requires_laser_device:
        if offer.device_key not in LASER_DEVICE_NAMES:
            raise PackageOfferError("A supported laser device is required for this package offer.")
        try:
            device_price = configured_device_price(
                db,
                workspace_id=workspace_id,
                service_id=offer.service_id,
                device_key=offer.device_key,
            )
        except InventoryOperationError as exc:
            raise PackageOfferError(str(exc)) from exc
        if device_price is None or device_price.price_minor is None:
            raise PackageOfferError("Standalone device price is unavailable for this package offer.")
        standalone_price_minor = int(device_price.price_minor)
        name = f"{service.name} · {offer.device_name} · {offer.sessions_count} sessions"
    else:
        if offer.device_key is not None:
            raise PackageOfferError("Non-laser package offer must not specify a laser device.")
        standalone_price_minor = int(service.price_minor)
        name = f"{service.name} · {offer.sessions_count} sessions"

    return create_patient_package(
        db,
        workspace_id=workspace_id,
        patient_id=patient_id,
        service_id=offer.service_id,
        name=name,
        sessions_purchased=offer.sessions_count,
        sale_price_minor=int(offer.price_minor),
        amount_paid_minor=amount_paid_minor,
        payment_method=payment_method,
        created_by_user_id=created_by_user_id,
        external_reference=external_reference,
        idempotency_key=idempotency_key,
        actor_type=actor_type,
        package_offer_id=offer.id,
        origin_appointment_id=origin_appointment_id,
        laser_device_key=offer.device_key,
        laser_device_name=offer.device_name,
        standalone_session_price_minor_at_purchase=standalone_price_minor,
    )
