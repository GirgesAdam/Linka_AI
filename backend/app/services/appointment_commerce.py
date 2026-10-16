from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.doctor_service import DoctorService
from app.models.patient_package import PatientPackage
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.activity import ActivityActorType, record_activity_event
from app.services.booking import BookingRuleError, find_exact_slot
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


def _active_service(
    db: Session, *, workspace_id: UUID, service_id: UUID
) -> Service:
    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace_id,
            Service.id == service_id,
            Service.is_active.is_(True),
        )
    )
    if service is None:
        raise AppointmentCommerceNotFound("Service not found or inactive.")
    return service


def _require_same_operational_category(
    db: Session,
    *,
    workspace_id: UUID,
    appointment: Appointment,
    candidate: Service,
) -> str:
    primary = _active_service(
        db, workspace_id=workspace_id, service_id=appointment.service_id
    )
    primary_category = (primary.operational_category or "").strip().casefold()
    candidate_category = (candidate.operational_category or "").strip().casefold()
    if (
        not primary_category
        or not candidate_category
        or primary_category != candidate_category
    ):
        raise AppointmentCommerceError(
            "Cross-category services require separate appointments."
        )
    return primary_category


def _effective_service_duration(
    db: Session,
    *,
    workspace_id: UUID,
    service: Service,
    laser_device_key: str | None,
) -> int:
    if not service.requires_laser_device:
        duration = int(service.duration_minutes or 0)
        if duration <= 0:
            raise AppointmentCommerceError("Service duration must be positive.")
        return duration
    device_key = (laser_device_key or "").strip() or None
    if device_key is None:
        raise AppointmentCommerceError(
            "A configured laser device is required for this visit service."
        )
    try:
        device_price = configured_device_price(
            db,
            workspace_id=workspace_id,
            service_id=service.id,
            device_key=device_key,
        )
    except InventoryOperationError as exc:
        raise AppointmentCommerceError(str(exc)) from exc
    duration = int(device_price.duration_minutes or 0)
    if duration <= 0:
        raise AppointmentCommerceError("Configured laser device duration must be positive.")
    return duration


def _current_additional_lines(
    db: Session, *, workspace_id: UUID, appointment_id: UUID
) -> list[AppointmentAdditionalService]:
    return list(
        db.scalars(
            select(AppointmentAdditionalService).where(
                AppointmentAdditionalService.workspace_id == workspace_id,
                AppointmentAdditionalService.appointment_id == appointment_id,
            )
        )
    )


def _visit_duration_authority(
    db: Session,
    *,
    workspace_id: UUID,
    appointment: Appointment,
    extra_service: Service | None = None,
    extra_device_key: str | None = None,
    excluded_line_id: UUID | None = None,
) -> tuple[int, Service, str | None]:
    candidates: list[tuple[int, Service, str | None]] = []
    primary = _active_service(
        db, workspace_id=workspace_id, service_id=appointment.service_id
    )
    candidates.append(
        (
            _effective_service_duration(
                db,
                workspace_id=workspace_id,
                service=primary,
                laser_device_key=appointment.laser_device_key,
            ),
            primary,
            appointment.laser_device_key,
        )
    )
    for line in _current_additional_lines(
        db, workspace_id=workspace_id, appointment_id=appointment.id
    ):
        if excluded_line_id is not None and line.id == excluded_line_id:
            continue
        service = _active_service(
            db, workspace_id=workspace_id, service_id=line.service_id
        )
        _require_same_operational_category(
            db,
            workspace_id=workspace_id,
            appointment=appointment,
            candidate=service,
        )
        candidates.append(
            (
                _effective_service_duration(
                    db,
                    workspace_id=workspace_id,
                    service=service,
                    laser_device_key=line.laser_device_key,
                ),
                service,
                line.laser_device_key,
            )
        )
    if extra_service is not None:
        candidates.append(
            (
                _effective_service_duration(
                    db,
                    workspace_id=workspace_id,
                    service=extra_service,
                    laser_device_key=extra_device_key,
                ),
                extra_service,
                extra_device_key,
            )
        )
    return max(candidates, key=lambda item: item[0])


def _apply_visit_duration(appointment: Appointment, duration_minutes: int) -> None:
    duration = int(duration_minutes)
    if duration <= 0:
        raise AppointmentCommerceError("Appointment duration must be positive.")
    old_end = appointment.end_at
    old_busy_end = appointment.busy_end_at
    after_buffer = max(old_busy_end - old_end, timedelta())
    appointment.duration_minutes = duration
    appointment.end_at = appointment.start_at + timedelta(minutes=duration)
    appointment.busy_end_at = appointment.end_at + after_buffer


def _ensure_duration_expansion_available(
    db: Session,
    *,
    workspace_id: UUID,
    appointment: Appointment,
    duration_service: Service,
    duration_device_key: str | None,
) -> None:
    if appointment.status == "completed":
        return
    workspace = db.scalar(
        select(Workspace).where(Workspace.id == workspace_id)
    )
    if workspace is None:
        raise AppointmentCommerceNotFound("Workspace not found.")
    try:
        find_exact_slot(
            db,
            workspace=workspace,
            branch_id=appointment.branch_id,
            service_id=duration_service.id,
            doctor_id=appointment.doctor_id,
            requested_start_at=appointment.start_at,
            exclude_appointment_id=appointment.id,
            laser_device_key=duration_device_key,
            minimum_notice_minutes_override=0,
        )
    except BookingRuleError as exc:
        raise AppointmentCommerceError(
            "The additional service would extend the appointment into an unavailable time window."
        ) from exc


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

    service = _active_service(
        db, workspace_id=workspace_id, service_id=service_id
    )
    _require_same_operational_category(
        db,
        workspace_id=workspace_id,
        appointment=appointment,
        candidate=service,
    )
    doctor_service = db.scalar(
        select(DoctorService).where(
            DoctorService.workspace_id == workspace_id,
            DoctorService.doctor_id == appointment.doctor_id,
            DoctorService.service_id == service_id,
            DoctorService.is_active.is_(True),
        )
    )
    if doctor_service is None:
        raise AppointmentCommerceError(
            "The appointment doctor cannot perform this additional service."
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
    if service.requires_laser_device and device_key != appointment.laser_device_key:
        raise AppointmentCommerceError(
            "Same-category laser services in one appointment must use the same laser device."
        )

    new_duration, duration_service, duration_device_key = _visit_duration_authority(
        db,
        workspace_id=workspace_id,
        appointment=appointment,
        extra_service=service,
        extra_device_key=device_key,
    )
    if new_duration > int(appointment.duration_minutes):
        _ensure_duration_expansion_available(
            db,
            workspace_id=workspace_id,
            appointment=appointment,
            duration_service=duration_service,
            duration_device_key=duration_device_key,
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
    if int(appointment.duration_minutes) != new_duration:
        _apply_visit_duration(appointment, new_duration)
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
    new_duration, _duration_service, _duration_device_key = _visit_duration_authority(
        db,
        workspace_id=workspace_id,
        appointment=appointment,
        excluded_line_id=line.id,
    )
    if int(appointment.duration_minutes) != new_duration:
        _apply_visit_duration(appointment, new_duration)
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

