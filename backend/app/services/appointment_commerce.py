from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.doctor_service import DoctorService
from app.models.patient_package import PatientPackage
from app.models.service import Service
from app.services.activity import ActivityActorType, record_activity_event
from app.services.inventory import InventoryOperationError, configured_device_price
from app.services.package_offers import (
    PackageOfferError,
    get_active_package_offer,
    purchase_package_offer,
)
from app.services.patient_packages import (
    PackageOperationError,
    consume_package_usage,
    consume_visit_package_usages,
    reserve_additional_service_package_usage,
    reserve_package_usage,
)
from app.services.payments import (
    get_appointment_payment_summary,
    refresh_appointment_payment_snapshots,
)
from app.services.pulse_billing import (
    PulseBillingError,
    apply_additional_service_pulse_billing,
)

_EDITABLE_VISIT_STATUSES = frozenset(
    {"pending", "confirmed", "checked_in", "in_progress", "completed"}
)


class AppointmentCommerceError(ValueError):
    pass


class AppointmentCommerceNotFound(AppointmentCommerceError):
    pass


def _locked_appointment(
    db: Session, *, workspace_id: UUID, appointment_id: UUID
) -> Appointment:
    appointment = db.scalar(
        select(Appointment)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.id == appointment_id,
        )
        .with_for_update()
    )
    if appointment is None:
        raise AppointmentCommerceNotFound("Appointment not found.")
    return appointment


def _require_editable_visit(appointment: Appointment) -> None:
    if appointment.status not in _EDITABLE_VISIT_STATUSES:
        raise AppointmentCommerceError(
            "Services and packages cannot be changed on a cancelled, no-show, "
            "or rescheduled appointment."
        )


def list_additional_services(
    db: Session, *, workspace_id: UUID, appointment_id: UUID
) -> list[AppointmentAdditionalService]:
    appointment = db.scalar(
        select(Appointment.id).where(
            Appointment.workspace_id == workspace_id,
            Appointment.id == appointment_id,
        )
    )
    if appointment is None:
        raise AppointmentCommerceNotFound("Appointment not found.")
    return list(
        db.scalars(
            select(AppointmentAdditionalService)
            .where(
                AppointmentAdditionalService.workspace_id == workspace_id,
                AppointmentAdditionalService.appointment_id == appointment_id,
            )
            .order_by(
                AppointmentAdditionalService.created_at,
                AppointmentAdditionalService.id,
            )
        ).all()
    )


def add_additional_service(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    service_id: UUID,
    laser_device_key: str | None,
    created_by_user_id: UUID | None,
    actor_type: ActivityActorType = "staff",
    pulse_mode: str = "none",
    pulses_used: int | None = None,
    pulse_pack_offer_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> AppointmentAdditionalService:
    appointment = _locked_appointment(
        db, workspace_id=workspace_id, appointment_id=appointment_id
    )
    _require_editable_visit(appointment)
    if appointment.service_id == service_id:
        raise AppointmentCommerceError(
            "The primary appointment service is already included in this visit."
        )

    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace_id,
            Service.id == service_id,
            Service.is_active.is_(True),
        )
    )
    if service is None:
        raise AppointmentCommerceNotFound("Service not found or inactive.")
    doctor_service = db.scalar(
        select(DoctorService).where(
            DoctorService.workspace_id == workspace_id,
            DoctorService.doctor_id == appointment.doctor_id,
            DoctorService.service_id == service_id,
            DoctorService.is_active.is_(True),
        )
    )
    duplicate = db.scalar(
        select(AppointmentAdditionalService.id).where(
            AppointmentAdditionalService.workspace_id == workspace_id,
            AppointmentAdditionalService.appointment_id == appointment_id,
            AppointmentAdditionalService.service_id == service_id,
        )
    )
    if duplicate is not None:
        raise AppointmentCommerceError(
            "This additional service is already attached to the appointment."
        )

    normalized_device = (laser_device_key or "").strip() or None
    device_key = None
    device_name = None
    if service.requires_laser_device:
        if normalized_device is None:
            raise AppointmentCommerceError(
                "Choose the laser device used for this additional service."
            )
        try:
            device_price = configured_device_price(
                db,
                workspace_id=workspace_id,
                service_id=service.id,
                device_key=normalized_device,
            )
        except InventoryOperationError as exc:
            raise AppointmentCommerceError(str(exc)) from exc
        price_minor = int(device_price.price_minor or 0)
        currency = device_price.currency
        device_key = device_price.device_key
        device_name = device_price.device_name
    else:
        if normalized_device is not None:
            raise AppointmentCommerceError(
                "A laser device cannot be selected for this service."
            )
        price_minor = int(
            doctor_service.custom_price_minor
            if doctor_service is not None and doctor_service.custom_price_minor is not None
            else service.price_minor
        )
        currency = service.currency or appointment.currency

    if currency != appointment.currency:
        raise AppointmentCommerceError(
            "The additional service currency must match the appointment currency."
        )

    line = AppointmentAdditionalService(
        workspace_id=workspace_id,
        appointment_id=appointment.id,
        service_id=service.id,
        service_name=service.name,
        unit_price_minor=price_minor,
        currency=currency,
        laser_device_key=device_key,
        laser_device_name=device_name,
        created_by_user_id=created_by_user_id,
    )
    db.add(line)
    db.flush()

    if pulse_mode != "none":
        try:
            apply_additional_service_pulse_billing(
                db,
                appointment=appointment,
                line=line,
                pulses_used=int(pulses_used or 0),
                mode=pulse_mode,
                offer_id=pulse_pack_offer_id,
                changed_by_user_id=created_by_user_id,
                idempotency_key=idempotency_key,
            )
        except PulseBillingError as exc:
            raise AppointmentCommerceError(str(exc)) from exc

    refresh_appointment_payment_snapshots(
        db, workspace_id=workspace_id, appointment_ids={appointment.id}
    )
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type=actor_type,
        actor_user_id=created_by_user_id,
        action="appointment.additional_service_added",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Additional visit service added",
        metadata={
            "service_id": service.id,
            "price_minor": price_minor,
            "device_key": device_key,
        },
    )
    return line


def remove_additional_service(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    line_id: UUID,
    changed_by_user_id: UUID | None,
) -> None:
    appointment = _locked_appointment(
        db, workspace_id=workspace_id, appointment_id=appointment_id
    )
    _require_editable_visit(appointment)
    line = db.scalar(
        select(AppointmentAdditionalService)
        .where(
            AppointmentAdditionalService.workspace_id == workspace_id,
            AppointmentAdditionalService.appointment_id == appointment_id,
            AppointmentAdditionalService.id == line_id,
        )
        .with_for_update()
    )
    if line is None:
        raise AppointmentCommerceNotFound("Additional service not found.")
    if line.patient_package_id is not None:
        raise AppointmentCommerceError(
            "A package-backed additional service cannot be removed from the visit."
        )
    if line.billing_context == "pulse_prepaid":
        raise AppointmentCommerceError(
            "A pulse-billed additional service cannot be removed until its billing is corrected."
        )
    service_id = line.service_id
    db.delete(line)
    db.flush()
    refresh_appointment_payment_snapshots(
        db, workspace_id=workspace_id, appointment_ids={appointment.id}
    )
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=changed_by_user_id,
        action="appointment.additional_service_removed",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Additional visit service removed",
        metadata={"service_id": service_id},
    )


def purchase_package_for_appointment(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    offer_id: UUID,
    created_by_user_id: UUID | None,
    idempotency_key: str | None,
) -> PatientPackage:
    appointment = _locked_appointment(
        db, workspace_id=workspace_id, appointment_id=appointment_id
    )
    _require_editable_visit(appointment)

    if appointment.patient_package_id is not None:
        if idempotency_key:
            existing = db.scalar(
                select(PatientPackage).where(
                    PatientPackage.workspace_id == workspace_id,
                    PatientPackage.id == appointment.patient_package_id,
                    PatientPackage.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing
        raise AppointmentCommerceError(
            "This appointment is already linked to a package."
        )

    try:
        offer = get_active_package_offer(
            db, workspace_id=workspace_id, offer_id=offer_id, for_update=True
        )
    except PackageOfferError as exc:
        raise AppointmentCommerceError(str(exc)) from exc
    if offer.service_id != appointment.service_id:
        raise AppointmentCommerceError(
            "The selected package is for a different service."
        )
    if offer.device_key != appointment.laser_device_key:
        raise AppointmentCommerceError(
            "The selected package is for a different laser device."
        )

    payment_summary = get_appointment_payment_summary(
        db, workspace_id=workspace_id, appointment_id=appointment.id
    )
    other_visit_due = max(
        int(payment_summary.price_minor) - int(appointment.price_minor),
        0,
    )
    if int(payment_summary.net_paid_minor) > other_visit_due:
        raise AppointmentCommerceError(
            "Refund or reconcile the payment already applied to this service before "
            "converting this session to a package."
        )

    try:
        package = purchase_package_offer(
            db,
            workspace_id=workspace_id,
            patient_id=appointment.patient_id,
            offer_id=offer.id,
            amount_paid_minor=0,
            payment_method="unknown",
            created_by_user_id=created_by_user_id,
            external_reference=None,
            idempotency_key=idempotency_key,
            actor_type="staff",
            origin_appointment_id=appointment.id,
        )
        reserve_package_usage(
            db,
            appointment=appointment,
            package=package,
            actor_type="staff",
            actor_user_id=created_by_user_id,
        )
        if appointment.status == "completed":
            consume_package_usage(
                db,
                appointment=appointment,
                actor_type="staff",
                actor_user_id=created_by_user_id,
            )
    except (PackageOfferError, PackageOperationError) as exc:
        raise AppointmentCommerceError(str(exc)) from exc

    refresh_appointment_payment_snapshots(
        db, workspace_id=workspace_id, appointment_ids={appointment.id}
    )
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=created_by_user_id,
        action="appointment.converted_to_package",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="Appointment converted to package usage",
        metadata={"package_id": package.id, "offer_id": offer.id},
    )
    return package


def purchase_package_for_additional_service(
    db: Session,
    *,
    workspace_id: UUID,
    appointment_id: UUID,
    line_id: UUID,
    offer_id: UUID,
    created_by_user_id: UUID | None,
    idempotency_key: str | None,
) -> PatientPackage:
    appointment = _locked_appointment(
        db, workspace_id=workspace_id, appointment_id=appointment_id
    )
    _require_editable_visit(appointment)
    line = db.scalar(
        select(AppointmentAdditionalService)
        .where(
            AppointmentAdditionalService.workspace_id == workspace_id,
            AppointmentAdditionalService.appointment_id == appointment.id,
            AppointmentAdditionalService.id == line_id,
        )
        .with_for_update()
    )
    if line is None:
        raise AppointmentCommerceNotFound("Additional service not found.")

    if line.billing_context == "pulse_prepaid":
        raise AppointmentCommerceError(
            "A pulse-billed additional service cannot be converted to a package."
        )
    if line.patient_package_id is not None:
        if idempotency_key:
            existing = db.scalar(
                select(PatientPackage).where(
                    PatientPackage.workspace_id == workspace_id,
                    PatientPackage.id == line.patient_package_id,
                    PatientPackage.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing
        raise AppointmentCommerceError(
            "This additional service is already linked to a package."
        )

    payment_summary = get_appointment_payment_summary(
        db, workspace_id=workspace_id, appointment_id=appointment.id
    )
    other_visit_due = max(
        int(payment_summary.price_minor) - int(line.unit_price_minor),
        0,
    )
    if int(payment_summary.net_paid_minor) > other_visit_due:
        raise AppointmentCommerceError(
            "Refund or reconcile the payment already applied to this additional "
            "service before converting it to a package."
        )

    try:
        offer = get_active_package_offer(
            db, workspace_id=workspace_id, offer_id=offer_id, for_update=True
        )
    except PackageOfferError as exc:
        raise AppointmentCommerceError(str(exc)) from exc
    if offer.service_id != line.service_id:
        raise AppointmentCommerceError(
            "The selected package is for a different service."
        )
    if offer.device_key != line.laser_device_key:
        raise AppointmentCommerceError(
            "The selected package is for a different laser device."
        )

    try:
        package = purchase_package_offer(
            db,
            workspace_id=workspace_id,
            patient_id=appointment.patient_id,
            offer_id=offer.id,
            amount_paid_minor=0,
            payment_method="unknown",
            created_by_user_id=created_by_user_id,
            external_reference=None,
            idempotency_key=idempotency_key,
            actor_type="staff",
            origin_appointment_id=appointment.id,
        )
        reserve_additional_service_package_usage(
            db,
            appointment=appointment,
            line=line,
            package=package,
            actor_type="staff",
            actor_user_id=created_by_user_id,
        )
        if appointment.status == "completed":
            consume_visit_package_usages(
                db,
                appointment=appointment,
                actor_type="staff",
                actor_user_id=created_by_user_id,
            )
    except (PackageOfferError, PackageOperationError) as exc:
        raise AppointmentCommerceError(str(exc)) from exc

    refresh_appointment_payment_snapshots(
        db, workspace_id=workspace_id, appointment_ids={appointment.id}
    )
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=created_by_user_id,
        action="appointment.additional_service_converted_to_package",
        entity_type="appointment_additional_service",
        entity_id=line.id,
        summary="Additional visit service converted to package usage",
        metadata={"package_id": package.id, "offer_id": offer.id},
    )
    return package

