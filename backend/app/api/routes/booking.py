from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess, get_workspace_admin, get_workspace_reader
from app.database.session import get_db
from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    require_tia_workspace_domain_write,
)
from app.models.appointment import Appointment
from app.models.appointment_status_history import AppointmentStatusHistory
from app.models.automation_job import AutomationJob
from app.models.automation_rule import AutomationRule
from app.models.availability_block import AvailabilityBlock
from app.models.availability_block_service import AvailabilityBlockService
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.lead import Lead
from app.models.patient import Patient
from app.models.patient_package import PatientPackage
from app.models.service import Service
from app.models.staff import Staff
from app.models.workspace_member import WORKSPACE_ROLE_ADMIN
from app.schemas.booking import (
    AppointmentAutomationRead,
    AppointmentCancel,
    AppointmentCreate,
    AppointmentEntitySummary,
    AppointmentLaserUsageUpdate,
    AppointmentListScope,
    AppointmentOperationalStatusUpdate,
    AppointmentOperationsRead,
    AppointmentPatientSummary,
    AppointmentRead,
    AppointmentReschedule,
    AppointmentStatus,
    AppointmentStatusHistoryRead,
    AvailabilityBlockCreate,
    AvailabilityBlockRead,
    AvailabilityResponse,
    AvailabilitySlot,
    QuickAppointmentCreate,
)
from app.schemas.patient_packages import (
    PatientPackageCancelRefundCreate,
    PatientPackageCancelRefundRead,
    PatientPackageCreate,
    PatientPackagePaymentCreate,
    PatientPackageRead,
)
from app.services.activity import record_activity_event
from app.services.appointment_operations import (
    AppointmentCancellationOverrideRequired,
    AppointmentOperationError,
    AppointmentOperationForbidden,
    AppointmentOperationNotFound,
    appointment_allowed_actions,
    cancel_appointment_operation,
    cancellation_override_required,
    confirm_appointment_operation,
    reschedule_appointment_operation,
    update_operational_status_operation,
)
from app.services.booking import (
    BookingRuleError,
    SlotCandidate,
    calculate_availability,
    find_exact_slot,
    get_effective_booking_settings,
)
from app.services.inventory import InventoryOperationError, configured_device_price
from app.services.patient_packages import (
    PackageOperationError,
    cancel_patient_package_with_refund,
    create_patient_package,
    list_patient_packages,
    package_read,
    record_package_payment,
    reserve_package_usage,
    validate_package_for_booking,
)
from app.services.pulse_billing import (
    PulseBillingError,
    settle_appointment_pulses,
    validate_pulse_booking,
)

router = APIRouter()


def not_found(entity: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{entity} not found.",
    )


def booking_conflict(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=detail,
    )


def quick_booking_integrity_detail(exc: IntegrityError) -> str:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    if constraint_name in {
        "excl_appointments_doctor_busy_time",
        "excl_appointments_laser_device_busy_time",
    }:
        return (
            "Quick booking scheduling override could not be applied. "
            "The database still enforced a schedule-overlap constraint."
        )
    if constraint_name == "appointment_quick_booking_staff_only":
        return "Quick booking is only available to authenticated clinic staff."
    if constraint_name == "uq_appointments_workspace_idempotency_key":
        return "This quick booking request was already submitted."
    return "Quick appointment data violates a database constraint."


def require_local_appointment_write(db: Session, workspace_id: UUID) -> None:
    try:
        require_tia_workspace_domain_write(
            db,
            workspace_id=workspace_id,
            domain="appointments",
        )
    except ClinicIntegrationAuthorityError as exc:
        raise booking_conflict(str(exc)) from exc


def workspace_timezone(access: WorkspaceAccess, branch: Branch | None = None) -> ZoneInfo:
    name = (branch.timezone if branch and branch.timezone else access.workspace.timezone) or "Africa/Cairo"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Africa/Cairo")


def get_appointment_or_404(db: Session, workspace_id: UUID, appointment_id: UUID) -> Appointment:
    appointment = db.scalar(
        select(Appointment).where(
            Appointment.id == appointment_id,
            Appointment.workspace_id == workspace_id,
        )
    )
    if appointment is None:
        raise not_found("Appointment")
    return appointment


def get_patient_for_booking(db: Session, workspace_id: UUID, patient_id: UUID) -> Patient:
    patient = db.scalar(
        select(Patient).where(
            Patient.id == patient_id,
            Patient.workspace_id == workspace_id,
        )
    )
    if patient is None:
        raise not_found("Patient")
    if patient.status == "blocked":
        raise booking_conflict("Blocked patients cannot receive new appointments.")
    return patient


def validate_lead(
    db: Session,
    workspace_id: UUID,
    patient_id: UUID,
    service_id: UUID,
    lead_id: UUID | None,
) -> Lead | None:
    if lead_id is None:
        return None
    lead = db.scalar(
        select(Lead).where(
            Lead.id == lead_id,
            Lead.workspace_id == workspace_id,
        )
    )
    if lead is None:
        raise not_found("Lead")
    if lead.patient_id != patient_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Lead does not belong to the selected patient.",
        )
    if lead.service_id is not None and lead.service_id != service_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Lead belongs to a different service.",
        )
    return lead


def add_history(
    db: Session,
    appointment: Appointment,
    changed_by_user_id: UUID | None,
    from_status: str | None,
    to_status: str,
    reason: str | None = None,
    metadata: dict | None = None,
) -> None:
    db.add(
        AppointmentStatusHistory(
            workspace_id=appointment.workspace_id,
            appointment_id=appointment.id,
            changed_by_user_id=changed_by_user_id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            metadata_json=metadata or {},
        )
    )


def slot_to_response(slot: SlotCandidate) -> AvailabilitySlot:
    return AvailabilitySlot(
        branch_id=slot.branch_id,
        doctor_id=slot.doctor_id,
        service_id=slot.service_id,
        start_at=slot.start_at,
        end_at=slot.end_at,
        price_minor=slot.price_minor,
        currency=slot.currency,
        laser_device_key=slot.laser_device_key,
        laser_device_name=slot.laser_device_name,
    )


def make_appointment(
    *,
    access: WorkspaceAccess,
    payload: AppointmentCreate,
    slot: SlotCandidate,
    initial_status: str,
    idempotency_key: str | None,
    rescheduled_from_appointment_id: UUID | None = None,
) -> Appointment:
    now = datetime.now(UTC)
    return Appointment(
        workspace_id=access.workspace.id,
        patient_id=payload.patient_id,
        branch_id=payload.branch_id,
        doctor_id=payload.doctor_id,
        is_quick_booking=False,
        service_id=payload.service_id,
        patient_package_id=payload.patient_package_id,
        lead_id=payload.lead_id,
        created_by_user_id=access.user.id,
        rescheduled_from_appointment_id=rescheduled_from_appointment_id,
        status=initial_status,
        source=payload.source,
        start_at=slot.start_at,
        end_at=slot.end_at,
        busy_start_at=slot.busy_start_at,
        busy_end_at=slot.busy_end_at,
        duration_minutes=slot.duration_minutes,
        price_minor=slot.price_minor,
        currency=slot.currency,
        laser_device_key=slot.laser_device_key,
        laser_device_name=slot.laser_device_name,
        billing_context="pulse_prepaid" if payload.use_pulse_balance else "standard",
        customer_note=payload.customer_note,
        idempotency_key=idempotency_key,
        confirmed_at=now if initial_status == "confirmed" else None,
    )



def _availability_block_read(
    db: Session, block: AvailabilityBlock, *, overlapping_appointments: int = 0
) -> AvailabilityBlockRead:
    service_ids = list(
        db.scalars(
            select(AvailabilityBlockService.service_id).where(
                AvailabilityBlockService.workspace_id == block.workspace_id,
                AvailabilityBlockService.availability_block_id == block.id,
            ).order_by(AvailabilityBlockService.service_id)
        )
    )
    return AvailabilityBlockRead.model_validate(block).model_copy(
        update={
            "service_ids": service_ids,
            "overlapping_appointments": overlapping_appointments,
        }
    )


def _availability_block_duplicate_exists(
    db: Session,
    *,
    workspace_id: UUID,
    branch_id: UUID,
    start_at: datetime,
    end_at: datetime,
    scope: str,
    service_ids: set[UUID],
) -> bool:
    candidates = list(
        db.scalars(
            select(AvailabilityBlock).where(
                AvailabilityBlock.workspace_id == workspace_id,
                AvailabilityBlock.branch_id == branch_id,
                AvailabilityBlock.start_at == start_at,
                AvailabilityBlock.end_at == end_at,
                AvailabilityBlock.scope == scope,
            )
        )
    )
    if scope == "all_services":
        return bool(candidates)
    for candidate in candidates:
        existing = set(
            db.scalars(
                select(AvailabilityBlockService.service_id).where(
                    AvailabilityBlockService.workspace_id == workspace_id,
                    AvailabilityBlockService.availability_block_id == candidate.id,
                )
            )
        )
        if existing == service_ids:
            return True
    return False


@router.get("/availability-blocks", response_model=list[AvailabilityBlockRead])
def list_availability_blocks(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    branch_id: UUID = Query(...),
    date_value: date | None = Query(default=None, alias="date"),
) -> list[AvailabilityBlockRead]:
    branch = db.scalar(
        select(Branch).where(
            Branch.id == branch_id,
            Branch.workspace_id == access.workspace.id,
            Branch.is_active.is_(True),
        )
    )
    if branch is None:
        raise not_found("Branch")
    stmt = select(AvailabilityBlock).where(
        AvailabilityBlock.workspace_id == access.workspace.id,
        AvailabilityBlock.branch_id == branch.id,
    )
    if date_value is not None:
        tz = workspace_timezone(access, branch)
        local_start = datetime.combine(date_value, time.min, tzinfo=tz).astimezone(UTC)
        local_end = (datetime.combine(date_value, time.min, tzinfo=tz) + timedelta(days=1)).astimezone(UTC)
        stmt = stmt.where(
            AvailabilityBlock.start_at < local_end,
            AvailabilityBlock.end_at > local_start,
        )
    rows = list(db.scalars(stmt.order_by(AvailabilityBlock.start_at)))
    return [_availability_block_read(db, row) for row in rows]


@router.post(
    "/availability-blocks",
    response_model=AvailabilityBlockRead,
    status_code=status.HTTP_201_CREATED,
)
def create_availability_block(
    payload: AvailabilityBlockCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> AvailabilityBlockRead:
    require_local_appointment_write(db, access.workspace.id)
    branch = db.scalar(
        select(Branch)
        .where(
            Branch.id == payload.branch_id,
            Branch.workspace_id == access.workspace.id,
            Branch.is_active.is_(True),
        )
        .with_for_update()
    )
    if branch is None:
        raise not_found("Branch")
    tz = workspace_timezone(access, branch)
    start_at = datetime.combine(payload.date, payload.start_time, tzinfo=tz).astimezone(UTC)
    end_at = datetime.combine(payload.date, payload.end_time, tzinfo=tz).astimezone(UTC)
    if end_at <= start_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Block end must be after start.",
        )
    if start_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Block start must not be in the past.",
        )

    selected_service_ids = set(payload.service_ids)
    if payload.scope == "selected_services":
        valid_service_ids = set(
            db.scalars(
                select(Service.id).where(
                    Service.workspace_id == access.workspace.id,
                    Service.id.in_(selected_service_ids),
                    Service.is_active.is_(True),
                )
            )
        )
        if valid_service_ids != selected_service_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Selected services must be active services in this workspace.",
            )

    if _availability_block_duplicate_exists(
        db,
        workspace_id=access.workspace.id,
        branch_id=branch.id,
        start_at=start_at,
        end_at=end_at,
        scope=payload.scope,
        service_ids=selected_service_ids,
    ):
        raise booking_conflict("An identical availability block already exists.")

    appointment_stmt = select(Appointment.id).where(
        Appointment.workspace_id == access.workspace.id,
        Appointment.branch_id == branch.id,
        Appointment.status.in_(("pending", "confirmed", "checked_in", "in_progress")),
        Appointment.start_at < end_at,
        Appointment.end_at > start_at,
    )
    if payload.scope == "selected_services":
        appointment_stmt = appointment_stmt.where(Appointment.service_id.in_(selected_service_ids))
    existing_count = len(list(db.scalars(appointment_stmt)))

    block = AvailabilityBlock(
        workspace_id=access.workspace.id,
        branch_id=branch.id,
        start_at=start_at,
        end_at=end_at,
        scope=payload.scope,
        reason=payload.reason,
        created_by_user_id=access.user.id,
    )
    db.add(block)
    db.flush()
    if payload.scope == "selected_services":
        db.add_all(
            [
                AvailabilityBlockService(
                    workspace_id=access.workspace.id,
                    availability_block_id=block.id,
                    service_id=service_id,
                )
                for service_id in sorted(selected_service_ids, key=str)
            ]
        )
    db.commit()
    db.refresh(block)
    return _availability_block_read(db, block, overlapping_appointments=existing_count)


@router.delete("/availability-blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_availability_block(
    block_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    require_local_appointment_write(db, access.workspace.id)
    block = db.scalar(select(AvailabilityBlock).where(
        AvailabilityBlock.id == block_id,
        AvailabilityBlock.workspace_id == access.workspace.id,
    ))
    if block is None:
        raise not_found("Availability block")
    db.delete(block)
    db.commit()
    return None


@router.get("/availability", response_model=AvailabilityResponse)
def get_availability(
    branch_id: UUID,
    service_id: UUID,
    booking_date: Annotated[date, Query(alias="date")],
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    doctor_id: UUID | None = None,
    laser_device_key: str | None = None,
    exclude_appointment_id: UUID | None = None,
    allow_immediate: bool = False,
) -> AvailabilityResponse:
    try:
        timezone_name, slots = calculate_availability(
            db=db,
            workspace=access.workspace,
            branch_id=branch_id,
            service_id=service_id,
            booking_date=booking_date,
            doctor_id=doctor_id,
            laser_device_key=laser_device_key,
            exclude_appointment_id=exclude_appointment_id,
            minimum_notice_minutes_override=0 if allow_immediate else None,
        )
    except BookingRuleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return AvailabilityResponse(
        date=booking_date,
        timezone=timezone_name,
        slots=[slot_to_response(slot) for slot in slots],
    )


@router.post(
    "/appointments",
    response_model=AppointmentRead,
    status_code=status.HTTP_201_CREATED,
)
def create_appointment(
    payload: AppointmentCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128),
    ] = None,
) -> Appointment:
    require_local_appointment_write(db, access.workspace.id)
    if idempotency_key:
        existing = db.scalar(
            select(Appointment).where(
                Appointment.workspace_id == access.workspace.id,
                Appointment.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing

    get_patient_for_booking(db, access.workspace.id, payload.patient_id)
    lead = validate_lead(
        db,
        access.workspace.id,
        payload.patient_id,
        payload.service_id,
        payload.lead_id,
    )

    try:
        configured_device_price(
            db,
            workspace_id=access.workspace.id,
            service_id=payload.service_id,
            device_key=payload.laser_device_key,
        )
        slot = find_exact_slot(
            db=db,
            workspace=access.workspace,
            branch_id=payload.branch_id,
            service_id=payload.service_id,
            doctor_id=payload.doctor_id,
            requested_start_at=payload.start_at,
            laser_device_key=payload.laser_device_key,
            minimum_notice_minutes_override=0 if payload.source == "staff" else None,
        )
    except (BookingRuleError, InventoryOperationError) as exc:
        raise booking_conflict(str(exc)) from exc

    if payload.patient_package_id is not None and payload.use_pulse_balance:
        raise booking_conflict("Choose either a session package or pulse balance, not both.")

    patient_package = None
    if payload.patient_package_id is not None:
        try:
            patient_package = validate_package_for_booking(
                db,
                workspace_id=access.workspace.id,
                package_id=payload.patient_package_id,
                patient_id=payload.patient_id,
                service_id=payload.service_id,
                appointment_start_at=slot.start_at,
                laser_device_key=slot.laser_device_key,
            )
        except PackageOperationError as exc:
            raise booking_conflict(str(exc)) from exc

    if payload.use_pulse_balance:
        try:
            validate_pulse_booking(
                db,
                workspace_id=access.workspace.id,
                patient_id=payload.patient_id,
                device_key=slot.laser_device_key,
                appointment_date=slot.start_at.astimezone(
                    ZoneInfo(access.workspace.timezone or "UTC")
                ).date(),
            )
        except PulseBillingError as exc:
            raise booking_conflict(str(exc)) from exc

    settings = get_effective_booking_settings(db, access.workspace.id)
    initial_status = "pending" if settings.require_confirmation else "confirmed"
    appointment = make_appointment(
        access=access,
        payload=payload,
        slot=slot,
        initial_status=initial_status,
        idempotency_key=idempotency_key,
    )
    try:
        db.add(appointment)
        db.flush()
        if patient_package is not None:
            reserve_package_usage(
                db,
                appointment=appointment,
                package=patient_package,
                actor_type="staff",
                actor_user_id=access.user.id,
            )
        add_history(
            db,
            appointment,
            changed_by_user_id=access.user.id,
            from_status=None,
            to_status=initial_status,
            reason="appointment_created",
        )
        if lead is not None and lead.status not in {"lost", "spam", "won"}:
            if lead.service_id is None:
                lead.service_id = payload.service_id
            lead.status = "booked"
        record_activity_event(
            db,
            workspace_id=access.workspace.id,
            actor_type="staff",
            actor_user_id=access.user.id,
            action="appointment.created",
            entity_type="appointment",
            entity_id=appointment.id,
            summary="Appointment created",
            metadata={
                "status": initial_status,
                "source": appointment.source,
                "patient_package_id": appointment.patient_package_id,
                "laser_device_key": appointment.laser_device_key,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise booking_conflict(
            "The requested slot was booked by another request. Refresh availability and try again."
        ) from exc
    db.refresh(appointment)
    return appointment


@router.post(
    "/appointments/quick",
    response_model=AppointmentRead,
    status_code=status.HTTP_201_CREATED,
)
def create_quick_appointment(
    payload: QuickAppointmentCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> Appointment:
    require_local_appointment_write(db, access.workspace.id)
    if idempotency_key:
        existing = db.scalar(select(Appointment).where(
            Appointment.workspace_id == access.workspace.id,
            Appointment.idempotency_key == idempotency_key,
        ))
        if existing is not None:
            return existing
    get_patient_for_booking(db, access.workspace.id, payload.patient_id)
    branch = db.scalar(select(Branch).where(
        Branch.workspace_id == access.workspace.id, Branch.id == payload.branch_id, Branch.is_active.is_(True)
    ))
    doctor = db.scalar(select(Doctor).where(
        Doctor.workspace_id == access.workspace.id, Doctor.id == payload.doctor_id, Doctor.is_active.is_(True)
    ))
    service = db.scalar(select(Service).where(
        Service.workspace_id == access.workspace.id, Service.id == payload.service_id, Service.is_active.is_(True)
    ))
    if branch is None:
        raise not_found("Branch")
    if doctor is None:
        raise not_found("Doctor")
    if service is None:
        raise not_found("Service")

    if service.requires_laser_device:
        if payload.laser_device_key is None:
            raise booking_conflict("Laser device choice is required for this service.")
        try:
            device_price = configured_device_price(
                db, workspace_id=access.workspace.id, service_id=service.id, device_key=payload.laser_device_key
            )
        except InventoryOperationError as exc:
            raise booking_conflict(str(exc)) from exc
        duration_minutes = int(device_price.duration_minutes)
        price_minor = int(device_price.price_minor or 0)
        currency = device_price.currency
        laser_device_key = device_price.device_key
        laser_device_name = device_price.device_name
    else:
        if payload.laser_device_key is not None:
            raise booking_conflict("A laser device cannot be selected for this service.")
        duration_minutes = int(service.duration_minutes)
        price_minor = int(service.price_minor)
        currency = service.currency
        laser_device_key = None
        laser_device_name = None

    start_at = payload.start_at.astimezone(UTC)
    end_at = start_at + timedelta(minutes=duration_minutes)
    # Quick Booking is an explicit receptionist override path: availability blocks do not apply.
    if payload.patient_package_id is not None and payload.use_pulse_balance:
        raise booking_conflict("Choose either a session package or pulse balance, not both.")
    patient_package = None
    if payload.patient_package_id is not None:
        try:
            patient_package = validate_package_for_booking(
                db, workspace_id=access.workspace.id, package_id=payload.patient_package_id,
                patient_id=payload.patient_id, service_id=service.id,
                appointment_start_at=start_at, laser_device_key=laser_device_key,
            )
        except PackageOperationError as exc:
            raise booking_conflict(str(exc)) from exc

    if payload.use_pulse_balance:
        try:
            validate_pulse_booking(
                db,
                workspace_id=access.workspace.id,
                patient_id=payload.patient_id,
                device_key=laser_device_key,
                appointment_date=start_at.astimezone(
                    ZoneInfo(access.workspace.timezone or "UTC")
                ).date(),
            )
        except PulseBillingError as exc:
            raise booking_conflict(str(exc)) from exc

    appointment = Appointment(
        workspace_id=access.workspace.id, patient_id=payload.patient_id, branch_id=branch.id,
        doctor_id=doctor.id, doctor_assignment_known=True, is_quick_booking=True,
        service_id=service.id, patient_package_id=payload.patient_package_id,
        created_by_user_id=access.user.id, status="confirmed", source="staff",
        start_at=start_at, end_at=end_at, busy_start_at=start_at, busy_end_at=end_at,
        duration_minutes=duration_minutes, price_minor=price_minor, currency=currency,
        laser_device_key=laser_device_key, laser_device_name=laser_device_name,
        billing_context="pulse_prepaid" if payload.use_pulse_balance else "standard",
        customer_note=payload.customer_note, idempotency_key=idempotency_key,
        confirmed_at=datetime.now(UTC),
    )
    try:
        db.add(appointment)
        db.flush()
        if patient_package is not None:
            reserve_package_usage(
                db, appointment=appointment, package=patient_package,
                actor_type="staff", actor_user_id=access.user.id,
            )
        add_history(
            db, appointment, changed_by_user_id=access.user.id, from_status=None,
            to_status="confirmed", reason="quick_appointment_created",
            metadata={"scheduling_override": True},
        )
        record_activity_event(
            db, workspace_id=access.workspace.id, actor_type="staff",
            actor_user_id=access.user.id, action="appointment.quick_created",
            entity_type="appointment", entity_id=appointment.id,
            summary="Quick appointment created",
            metadata={
                "service_id": str(service.id), "doctor_id": str(doctor.id),
                "laser_device_key": laser_device_key,
                "patient_package_id": str(appointment.patient_package_id) if appointment.patient_package_id else None,
                "scheduling_override": True,
            },
        )
        db.commit()
    except PackageOperationError as exc:
        db.rollback()
        raise booking_conflict(str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise booking_conflict(quick_booking_integrity_detail(exc)) from exc
    db.refresh(appointment)
    return appointment


@router.get("/patients/{patient_id}/packages", response_model=list[PatientPackageRead])
def get_patient_packages(
    patient_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    service_id: UUID | None = None,
    usable_only: bool = False,
) -> list[PatientPackageRead]:
    get_patient_for_booking(db, access.workspace.id, patient_id)
    return list_patient_packages(
        db,
        workspace_id=access.workspace.id,
        patient_id=patient_id,
        service_id=service_id,
        usable_only=usable_only,
        include_financials=True,
    )


@router.post(
    "/patient-packages",
    response_model=PatientPackageRead,
    status_code=status.HTTP_201_CREATED,
)
def create_package_sale(
    payload: PatientPackageCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=128)
    ] = None,
) -> PatientPackageRead:
    try:
        package = create_patient_package(
            db,
            workspace_id=access.workspace.id,
            patient_id=payload.patient_id,
            service_id=payload.service_id,
            name=payload.name,
            sessions_purchased=payload.sessions_purchased,
            sale_price_minor=payload.sale_price_minor,
            amount_paid_minor=payload.amount_paid_minor,
            payment_method=payload.payment_method,
            created_by_user_id=access.user.id,
            purchased_at=payload.purchased_at,
            expires_at=payload.expires_at,
            external_reference=payload.external_reference,
            external_id=payload.external_id,
            idempotency_key=idempotency_key,
        )
        db.commit()
        db.refresh(package)
        return package_read(db, package, include_financials=True)
    except PackageOperationError as exc:
        db.rollback()
        raise booking_conflict(str(exc)) from exc


@router.post(
    "/patient-packages/{package_id}/payments",
    response_model=PatientPackageRead,
    status_code=status.HTTP_201_CREATED,
)
def create_package_payment(
    package_id: UUID,
    payload: PatientPackagePaymentCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=128)
    ] = None,
) -> PatientPackageRead:
    try:
        record_package_payment(
            db,
            workspace_id=access.workspace.id,
            package_id=package_id,
            amount_minor=payload.amount_minor,
            payment_method=payload.payment_method,
            external_reference=payload.external_reference,
            created_by_user_id=access.user.id,
            idempotency_key=idempotency_key,
        )
        package = db.get(PatientPackage, package_id)
        if package is None or package.workspace_id != access.workspace.id:
            raise PackageOperationError("Package not found.")
        db.commit()
        db.refresh(package)
        return package_read(db, package, include_financials=True)
    except PackageOperationError as exc:
        db.rollback()
        raise booking_conflict(str(exc)) from exc


@router.post(
    "/patient-packages/{package_id}/cancel-refund",
    response_model=PatientPackageCancelRefundRead,
    status_code=status.HTTP_201_CREATED,
)
def cancel_package_and_refund(
    package_id: UUID,
    payload: PatientPackageCancelRefundCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=128)
    ] = None,
) -> PatientPackageCancelRefundRead:
    try:
        (
            package,
            collected_minor,
            consumed_value_minor,
            settlement_target_minor,
            previously_refunded_minor,
            collected_now_minor,
            refunded_now_minor,
            refund_transactions,
        ) = cancel_patient_package_with_refund(
            db,
            workspace_id=access.workspace.id,
            package_id=package_id,
            reason=payload.reason,
            created_by_user_id=access.user.id,
            standalone_session_price_minor_at_purchase=(
                payload.standalone_session_price_minor_at_purchase
            ),
            settlement_target_minor=payload.settlement_target_minor,
            payment_method=payload.payment_method,
            idempotency_key=idempotency_key,
        )
        db.commit()
        db.refresh(package)
        read = package_read(db, package, include_financials=True)
        return PatientPackageCancelRefundRead(
            package=read,
            collected_minor=collected_minor,
            consumed_sessions=read.sessions_consumed,
            consumed_value_minor=consumed_value_minor,
            settlement_target_minor=settlement_target_minor,
            previously_refunded_minor=previously_refunded_minor,
            collected_now_minor=collected_now_minor,
            refunded_now_minor=refunded_now_minor,
            refund_transaction_ids=[row.id for row in refund_transactions],
        )
    except PackageOperationError as exc:
        db.rollback()
        raise booking_conflict(str(exc)) from exc


@router.get("/appointments", response_model=list[AppointmentRead])
def list_appointments(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    patient_id: UUID | None = None,
    doctor_id: UUID | None = None,
    branch_id: UUID | None = None,
    appointment_date: Annotated[date | None, Query(alias="date")] = None,
    appointment_status: Annotated[AppointmentStatus | None, Query(alias="status")] = None,
    scope: AppointmentListScope = "all",
    start_from: datetime | None = None,
    start_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Appointment]:
    stmt = select(Appointment).where(Appointment.workspace_id == access.workspace.id)
    if patient_id:
        stmt = stmt.where(Appointment.patient_id == patient_id)
    if doctor_id:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if branch_id:
        stmt = stmt.where(Appointment.branch_id == branch_id)
    if appointment_status:
        stmt = stmt.where(Appointment.status == appointment_status)
    now = datetime.now(UTC)
    if appointment_date is not None:
        branch = None
        if branch_id is not None:
            branch = db.scalar(
                select(Branch).where(
                    Branch.workspace_id == access.workspace.id,
                    Branch.id == branch_id,
                )
            )
        tz = workspace_timezone(access, branch)
        local_start = datetime.combine(appointment_date, time.min, tzinfo=tz)
        local_end = local_start + timedelta(days=1)
        stmt = stmt.where(
            Appointment.start_at >= local_start.astimezone(UTC),
            Appointment.start_at < local_end.astimezone(UTC),
        )
    elif scope == "today":
        tz = workspace_timezone(access)
        local_now = now.astimezone(tz)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end = local_start + timedelta(days=1)
        stmt = stmt.where(
            Appointment.start_at >= local_start.astimezone(UTC),
            Appointment.start_at < local_end.astimezone(UTC),
        )
    elif scope == "upcoming":
        stmt = stmt.where(
            Appointment.start_at >= now,
            Appointment.status.in_(("pending", "confirmed")),
        )
    elif scope == "past":
        stmt = stmt.where(Appointment.start_at < now)
    if start_from:
        if start_from.tzinfo is None or start_from.utcoffset() is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="start_from must include a timezone offset.",
            )
        stmt = stmt.where(Appointment.start_at >= start_from.astimezone(UTC))
    if start_to:
        if start_to.tzinfo is None or start_to.utcoffset() is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="start_to must include a timezone offset.",
            )
        stmt = stmt.where(Appointment.start_at < start_to.astimezone(UTC))
    stmt = stmt.order_by(Appointment.start_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt))


@router.get("/appointments/{appointment_id}", response_model=AppointmentRead)
def get_appointment(
    appointment_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Appointment:
    return get_appointment_or_404(db, access.workspace.id, appointment_id)


@router.get(
    "/appointments/{appointment_id}/history",
    response_model=list[AppointmentStatusHistoryRead],
)
def get_appointment_history(
    appointment_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[AppointmentStatusHistory]:
    get_appointment_or_404(db, access.workspace.id, appointment_id)
    return list(
        db.scalars(
            select(AppointmentStatusHistory)
            .where(
                AppointmentStatusHistory.workspace_id == access.workspace.id,
                AppointmentStatusHistory.appointment_id == appointment_id,
            )
            .order_by(AppointmentStatusHistory.created_at)
        )
    )


@router.get("/appointments/{appointment_id}/operations", response_model=AppointmentOperationsRead)
def get_appointment_operations(
    appointment_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> AppointmentOperationsRead:
    row = db.execute(
        select(Appointment, Patient, Branch, Service, Doctor, Staff)
        .join(
            Patient,
            and_(
                Patient.workspace_id == Appointment.workspace_id,
                Patient.id == Appointment.patient_id,
            ),
        )
        .join(
            Branch,
            and_(
                Branch.workspace_id == Appointment.workspace_id,
                Branch.id == Appointment.branch_id,
            ),
        )
        .join(
            Service,
            and_(
                Service.workspace_id == Appointment.workspace_id,
                Service.id == Appointment.service_id,
            ),
        )
        .join(
            Doctor,
            and_(
                Doctor.workspace_id == Appointment.workspace_id,
                Doctor.id == Appointment.doctor_id,
            ),
        )
        .join(
            Staff,
            and_(
                Staff.workspace_id == Doctor.workspace_id,
                Staff.id == Doctor.staff_id,
            ),
        )
        .where(
            Appointment.workspace_id == access.workspace.id,
            Appointment.id == appointment_id,
        )
    ).one_or_none()
    if row is None:
        raise not_found("Appointment")

    appointment, patient, branch, service, doctor, staff = row
    history = list(
        db.scalars(
            select(AppointmentStatusHistory)
            .where(
                AppointmentStatusHistory.workspace_id == access.workspace.id,
                AppointmentStatusHistory.appointment_id == appointment.id,
            )
            .order_by(AppointmentStatusHistory.created_at)
        )
    )
    automation_rows = list(
        db.execute(
            select(AutomationJob, AutomationRule)
            .join(
                AutomationRule,
                and_(
                    AutomationRule.workspace_id == AutomationJob.workspace_id,
                    AutomationRule.id == AutomationJob.rule_id,
                ),
            )
            .where(
                AutomationJob.workspace_id == access.workspace.id,
                AutomationJob.appointment_id == appointment.id,
                AutomationJob.job_kind == "appointment_rule",
            )
            .order_by(AutomationJob.scheduled_for, AutomationJob.created_at)
        ).all()
    )

    now = datetime.now(UTC)
    settings = get_effective_booking_settings(db, access.workspace.id)
    override_required = cancellation_override_required(
        appointment_status=appointment.status,
        start_at=appointment.start_at,
        cancellation_notice_minutes=settings.cancellation_notice_minutes,
        now=now,
    )
    patient_name = f"{patient.first_name or ''} {patient.last_name or ''}".strip() or patient.first_name or "Ø§Ù„Ø¹Ù…ÙŠÙ„"
    doctor_name = f"{staff.first_name or ''} {staff.last_name or ''}".strip() or "Ø§Ù„Ø¯ÙƒØªÙˆØ±"

    return AppointmentOperationsRead(
        appointment=AppointmentRead.model_validate(appointment),
        patient=AppointmentPatientSummary(id=patient.id, name=patient_name, phone=patient.phone),
        branch=AppointmentEntitySummary(id=branch.id, name=branch.name),
        service=AppointmentEntitySummary(id=service.id, name=service.name),
        doctor=AppointmentEntitySummary(id=doctor.id, name=doctor_name),
        timezone=workspace_timezone(access, branch).key,
        history=[AppointmentStatusHistoryRead.model_validate(item) for item in history],
        automations=[
            AppointmentAutomationRead(
                id=job.id,
                rule_key=rule.key,
                rule_name=rule.name,
                status=job.status,
                scheduled_for=job.scheduled_for,
                attempts=job.attempts,
                last_error=job.last_error,
            )
            for job, rule in automation_rows
        ],
        allowed_actions=list(
            appointment_allowed_actions(
                appointment_status=appointment.status,
                start_at=appointment.start_at,
                now=now,
            )
        ),
        cancellation_override_required=override_required,
        can_override_cancellation_policy=(access.membership.role == WORKSPACE_ROLE_ADMIN),
    )


@router.put("/appointments/{appointment_id}/laser-usage", response_model=AppointmentRead)
def update_appointment_laser_usage(
    appointment_id: UUID,
    payload: AppointmentLaserUsageUpdate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Appointment:
    require_local_appointment_write(db, access.workspace.id)
    appointment = db.scalar(
        select(Appointment).where(
            Appointment.workspace_id == access.workspace.id,
            Appointment.id == appointment_id,
        ).with_for_update()
    )
    if appointment is None:
        raise not_found("Appointment")
    if appointment.laser_device_key is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Laser pulses can only be recorded for a laser appointment.")
    previous = appointment.laser_pulses_used
    try:
        if appointment.billing_context == "pulse_prepaid":
            settle_appointment_pulses(
                db,
                workspace_id=access.workspace.id,
                appointment_id=appointment.id,
                pulses_used=payload.pulses_used,
                changed_by_user_id=access.user.id,
            )
        else:
            appointment.laser_pulses_used = payload.pulses_used
            record_activity_event(
                db, workspace_id=access.workspace.id, actor_type="staff",
                actor_user_id=access.user.id, action="appointment.laser_usage_updated",
                entity_type="appointment", entity_id=appointment.id,
                summary="Laser pulse usage updated",
                metadata={"previous_pulses_used": previous, "pulses_used": payload.pulses_used},
            )
        db.commit()
    except PulseBillingError as exc:
        db.rollback()
        raise booking_conflict(str(exc)) from exc
    db.refresh(appointment)
    return appointment


@router.post("/appointments/{appointment_id}/confirm", response_model=AppointmentRead)
def confirm_appointment(
    appointment_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Appointment:
    require_local_appointment_write(db, access.workspace.id)
    try:
        appointment = confirm_appointment_operation(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            changed_by_user_id=access.user.id,
            reason="appointment_confirmed",
        )
        db.commit()
    except AppointmentOperationNotFound as exc:
        db.rollback()
        raise not_found("Appointment") from exc
    except AppointmentOperationError as exc:
        db.rollback()
        raise booking_conflict(str(exc)) from exc
    db.refresh(appointment)
    return appointment


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentRead)
def cancel_appointment(
    appointment_id: UUID,
    payload: AppointmentCancel,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Appointment:
    require_local_appointment_write(db, access.workspace.id)
    try:
        appointment = cancel_appointment_operation(
            db,
            workspace=access.workspace,
            appointment_id=appointment_id,
            changed_by_user_id=access.user.id,
            reason=payload.reason,
            override_policy=payload.override_policy,
            actor_is_admin=(access.membership.role == WORKSPACE_ROLE_ADMIN),
        )
        db.commit()
    except AppointmentOperationNotFound as exc:
        db.rollback()
        raise not_found("Appointment") from exc
    except AppointmentOperationForbidden as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except AppointmentCancellationOverrideRequired as exc:
        db.rollback()
        raise booking_conflict(str(exc)) from exc
    except AppointmentOperationError as exc:
        db.rollback()
        raise booking_conflict(str(exc)) from exc
    db.refresh(appointment)
    return appointment


@router.post("/appointments/{appointment_id}/reschedule", response_model=AppointmentRead)
def reschedule_appointment(
    appointment_id: UUID,
    payload: AppointmentReschedule,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128),
    ] = None,
) -> Appointment:
    require_local_appointment_write(db, access.workspace.id)
    try:
        replacement, _ = reschedule_appointment_operation(
            db,
            workspace=access.workspace,
            appointment_id=appointment_id,
            requested_start_at=payload.start_at,
            changed_by_user_id=access.user.id,
            branch_id=payload.branch_id,
            doctor_id=payload.doctor_id,
            reason=payload.reason or "appointment_rescheduled",
            idempotency_key=idempotency_key,
            minimum_notice_minutes_override=0,
        )
        db.commit()
    except AppointmentOperationNotFound as exc:
        db.rollback()
        raise not_found("Appointment") from exc
    except AppointmentOperationError as exc:
        db.rollback()
        raise booking_conflict(str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise booking_conflict(
            "The requested replacement slot was booked by another request. Refresh availability and try again."
        ) from exc
    db.refresh(replacement)
    return replacement


@router.post("/appointments/{appointment_id}/status", response_model=AppointmentRead)
def update_operational_status(
    appointment_id: UUID,
    payload: AppointmentOperationalStatusUpdate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Appointment:
    require_local_appointment_write(db, access.workspace.id)
    try:
        appointment = update_operational_status_operation(
            db,
            workspace_id=access.workspace.id,
            appointment_id=appointment_id,
            target_status=payload.status,
            changed_by_user_id=access.user.id,
            reason=payload.reason,
        )
        db.commit()
    except AppointmentOperationNotFound as exc:
        db.rollback()
        raise not_found("Appointment") from exc
    except AppointmentOperationError as exc:
        db.rollback()
        raise booking_conflict(str(exc)) from exc
    db.refresh(appointment)
    return appointment
