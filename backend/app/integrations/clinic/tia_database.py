from __future__ import annotations

from collections.abc import Hashable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    require_tia_workspace_domain_write,
)
from app.integrations.clinic.base import (
    AppointmentMutationResult,
    AppointmentReadRequest,
    AppointmentReadResult,
    AppointmentRecord,
    AvailabilityRequest,
    AvailabilityResult,
    AvailabilitySlot,
    CancelAppointmentRequest,
    ClinicActionRequiresHuman,
    ClinicAdapter,
    ClinicCapabilities,
    ClinicCapability,
    ConfirmAppointmentRequest,
    CreateAppointmentRequest,
    PatientReadRequest,
    PatientRecord,
    PaymentAllocationRecord,
    PaymentReadRequest,
    PaymentReadResult,
    PaymentRecord,
    RescheduleAppointmentRequest,
)
from app.models.appointment import ACTIVE_APPOINTMENT_STATUSES, Appointment
from app.models.appointment_status_history import AppointmentStatusHistory
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.lead import Lead
from app.models.patient import Patient
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.models.service import Service
from app.models.staff import Staff
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace
from app.services.activity import record_activity_event
from app.services.appointment_operations import (
    AppointmentCancellationOverrideRequired,
    AppointmentOperationError,
    AppointmentOperationNotFound,
    AppointmentServiceChangeRequiresHuman,
    cancel_appointment_operation,
    confirm_appointment_operation,
    reschedule_appointment_operation,
)
from app.services.booking import (
    BookingRuleError,
    calculate_availability,
    find_exact_slot,
    get_effective_booking_settings,
)
from app.services.patient_packages import (
    PackageOperationError,
    reserve_package_usage,
    validate_package_for_booking,
)

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _price_display(price_minor: int, currency: str) -> str:
    amount = Decimal(price_minor) / Decimal(100)
    if amount == amount.to_integral():
        text = f"{int(amount):,}"
    else:
        text = f"{amount:,.2f}"
    return f"{text} {currency.upper()}"


def _description(value: str | None, *, limit: int = 260) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[:limit] + "…"


def filter_bookable_doctor_rows(
    doctor_rows: Iterable[tuple[Any, Any]],
    *,
    service_ids_by_doctor: dict[UUID, list[str]],
    branch_ids_by_doctor: dict[UUID, list[str]],
    scheduled_branch_ids_by_doctor: dict[UUID, list[str]],
) -> list[tuple[Any, Any]]:
    """Keep doctors connected to a complete active booking graph."""
    eligible: list[tuple[Any, Any]] = []
    for doctor, staff in doctor_rows:
        service_ids = service_ids_by_doctor.get(doctor.id, [])
        branch_ids = set(branch_ids_by_doctor.get(doctor.id, []))
        scheduled_branch_ids = set(scheduled_branch_ids_by_doctor.get(doctor.id, []))
        if service_ids and branch_ids.intersection(scheduled_branch_ids):
            eligible.append((doctor, staff))
    return eligible


class TiaDatabaseClinicAdapter(ClinicAdapter):
    """Clinic adapter backed by Tia's native PostgreSQL/SQLAlchemy schema."""

    def __init__(self, *, db: Session, workspace: Workspace) -> None:
        self.db = db
        self.workspace = workspace

    @property
    def capabilities(self) -> ClinicCapabilities:
        return ClinicCapabilities(
            supported=frozenset(
                {
                    ClinicCapability.CATALOG_READ,
                    ClinicCapability.AVAILABILITY_READ,
                    ClinicCapability.APPOINTMENTS_READ,
                    ClinicCapability.APPOINTMENTS_CREATE,
                    ClinicCapability.APPOINTMENTS_CONFIRM,
                    ClinicCapability.APPOINTMENTS_CANCEL,
                    ClinicCapability.APPOINTMENTS_RESCHEDULE,
                    ClinicCapability.PATIENTS_READ,
                    ClinicCapability.PAYMENTS_READ,
                }
            )
        )

    def _require_local_appointment_write(self) -> None:
        try:
            require_tia_workspace_domain_write(
                self.db,
                workspace_id=self.workspace.id,
                domain="appointments",
            )
        except ClinicIntegrationAuthorityError as exc:
            raise BookingRuleError(str(exc)) from exc

    @staticmethod
    def _native_uuid(value: str | None, field_name: str) -> UUID | None:
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise BookingRuleError(
                f"{field_name} is not a valid identifier for the Tia database integration."
            ) from exc

    def catalog_revision(self) -> Hashable:
        """Return one-round-trip freshness data for all catalog source tables."""
        workspace_id = self.workspace.id
        specs = (
            (Service, Service.workspace_id),
            (Branch, Branch.workspace_id),
            (Doctor, Doctor.workspace_id),
            (Staff, Staff.workspace_id),
            (DoctorService, DoctorService.workspace_id),
            (DoctorBranch, DoctorBranch.workspace_id),
            (BranchWorkingHour, BranchWorkingHour.workspace_id),
            (DoctorWorkingHour, DoctorWorkingHour.workspace_id),
        )
        columns = []
        for index, (model, workspace_column) in enumerate(specs):
            count_sq = (
                select(func.count(model.id))
                .where(workspace_column == workspace_id)
                .scalar_subquery()
                .label(f"c{index}")
            )
            updated_sq = (
                select(func.max(model.updated_at))
                .where(workspace_column == workspace_id)
                .scalar_subquery()
                .label(f"u{index}")
            )
            columns.extend((count_sq, updated_sq))
        row = self.db.execute(select(*columns)).one()
        return (self.workspace.primary_branch_id, *tuple(row))

    def get_availability(self, request: AvailabilityRequest) -> AvailabilityResult:
        """Expose the existing booking engine through the canonical adapter contract."""
        self.require_capability(ClinicCapability.AVAILABILITY_READ)

        branch_id = self._native_uuid(request.branch_id, "branch_id")
        service_id = self._native_uuid(request.service_id, "service_id")
        doctor_id = self._native_uuid(request.doctor_id, "doctor_id")
        exclude_appointment_id = self._native_uuid(
            request.exclude_appointment_id,
            "exclude_appointment_id",
        )
        exclude_appointment_ids = tuple(
            parsed
            for item in request.exclude_appointment_ids
            if (parsed := self._native_uuid(item, "exclude_appointment_ids")) is not None
        )
        assert branch_id is not None
        assert service_id is not None

        # Session.get() reuses SQLAlchemy's identity map when the grounded tool
        # already loaded these rows, preserving the existing no-redundant-read
        # latency optimization. It still performs a normal DB read when needed.
        branch = self.db.get(Branch, branch_id)
        if (
            branch is None
            or branch.workspace_id != self.workspace.id
            or not branch.is_active
        ):
            raise BookingRuleError("Branch not found or inactive.")

        service = self.db.get(Service, service_id)
        if (
            service is None
            or service.workspace_id != self.workspace.id
            or not service.is_active
        ):
            raise BookingRuleError("Service not found or inactive.")

        timezone_name, native_slots = calculate_availability(
            db=self.db,
            workspace=self.workspace,
            branch_id=branch_id,
            service_id=service_id,
            booking_date=request.booking_date,
            doctor_id=doctor_id,
            exclude_appointment_id=exclude_appointment_id,
            exclude_appointment_ids=exclude_appointment_ids,
            now=request.now,
            laser_device_key=request.laser_device_key,
            preloaded_branch=branch,
            preloaded_service=service,
        )

        slot_doctor_ids = {slot.doctor_id for slot in native_slots}
        doctor_names: dict[UUID, str] = {}
        if slot_doctor_ids:
            rows = self.db.execute(
                select(Doctor.id, Staff.first_name, Staff.last_name)
                .join(
                    Staff,
                    (Staff.workspace_id == Doctor.workspace_id)
                    & (Staff.id == Doctor.staff_id),
                )
                .where(
                    Doctor.workspace_id == self.workspace.id,
                    Doctor.id.in_(slot_doctor_ids),
                )
            ).all()
            for doctor_id_value, first_name, last_name in rows:
                full_name = f"{first_name or ''} {last_name or ''}".strip()
                doctor_names[doctor_id_value] = full_name or "الدكتور المتاح"

        slots = tuple(
            AvailabilitySlot(
                branch_id=str(slot.branch_id),
                branch_name=branch.name,
                doctor_id=str(slot.doctor_id),
                doctor_name=doctor_names.get(slot.doctor_id, "الدكتور المتاح"),
                service_id=str(slot.service_id),
                service_name=service.name,
                start_at=slot.start_at,
                end_at=slot.end_at,
                duration_minutes=slot.duration_minutes,
                price_minor=slot.price_minor,
                currency=slot.currency,
                laser_device_key=getattr(slot, "laser_device_key", None),
                laser_device_name=getattr(slot, "laser_device_name", None),
            )
            for slot in native_slots
        )

        return AvailabilityResult(
            timezone=timezone_name,
            branch_id=str(branch.id),
            branch_name=branch.name,
            service_id=str(service.id),
            service_name=service.name,
            service_duration_minutes=service.duration_minutes,
            service_price_minor=service.price_minor,
            service_currency=service.currency,
            slots=slots,
        )

    def _patient_appointment(self, *, patient_id: UUID, appointment_id: UUID) -> Appointment:
        appointment = self.db.scalar(
            select(Appointment).where(
                Appointment.workspace_id == self.workspace.id,
                Appointment.patient_id == patient_id,
                Appointment.id == appointment_id,
            )
        )
        if appointment is None:
            raise ValueError("Appointment not found for this customer.")
        return appointment

    def _appointment_record(self, *, appointment_id: UUID) -> AppointmentRecord:
        row = self.db.execute(
            select(
                Appointment,
                Service.name,
                Branch.name,
                Branch.timezone,
                Staff.first_name,
                Staff.last_name,
            )
            .join(
                Service,
                (Service.workspace_id == Appointment.workspace_id)
                & (Service.id == Appointment.service_id),
            )
            .join(
                Branch,
                (Branch.workspace_id == Appointment.workspace_id)
                & (Branch.id == Appointment.branch_id),
            )
            .join(
                Doctor,
                (Doctor.workspace_id == Appointment.workspace_id)
                & (Doctor.id == Appointment.doctor_id),
            )
            .join(
                Staff,
                (Staff.workspace_id == Doctor.workspace_id)
                & (Staff.id == Doctor.staff_id),
            )
            .where(
                Appointment.workspace_id == self.workspace.id,
                Appointment.id == appointment_id,
            )
        ).first()
        if row is None:
            raise ValueError("Appointment not found.")

        appointment, service_name, branch_name, branch_timezone, first_name, last_name = row
        timezone_name = branch_timezone or self.workspace.timezone
        try:
            ZoneInfo(timezone_name)
        except Exception:
            timezone_name = "UTC"
        doctor_name = f"{first_name or ''} {last_name or ''}".strip() or None
        return AppointmentRecord(
            appointment_id=str(appointment.id),
            patient_id=str(appointment.patient_id),
            status=appointment.status,
            service_id=str(appointment.service_id),
            service_name=service_name,
            branch_id=str(appointment.branch_id),
            branch_name=branch_name,
            doctor_id=str(appointment.doctor_id),
            doctor_name=doctor_name,
            start_at=appointment.start_at,
            end_at=appointment.end_at,
            timezone=timezone_name,
            price_minor=appointment.price_minor,
            currency=appointment.currency,
            payment_status=getattr(appointment, "payment_status", "unknown"),
            amount_paid_minor=getattr(appointment, "amount_paid_minor", None),
            payment_method=getattr(appointment, "payment_method", "unknown"),
            billing_context=getattr(appointment, "billing_context", "standard"),
            package_external_id=getattr(appointment, "package_external_id", None),
            patient_package_id=(
                str(appointment.patient_package_id)
                if getattr(appointment, "patient_package_id", None)
                else None
            ),
            laser_device_key=getattr(appointment, "laser_device_key", None),
            laser_device_name=getattr(appointment, "laser_device_name", None),
            visit_group_id=(
                str(appointment.visit_group_id) if appointment.visit_group_id else None
            ),
        )

    def _add_status_history(
        self,
        appointment: Appointment,
        *,
        from_status: str | None,
        to_status: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.db.add(
            AppointmentStatusHistory(
                workspace_id=self.workspace.id,
                appointment_id=appointment.id,
                changed_by_user_id=None,
                from_status=from_status,
                to_status=to_status,
                reason=reason,
                metadata_json=metadata or {},
            )
        )

    @staticmethod
    def _require_aware_start(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "start_at must include the timezone offset from availability results."
            )
        return value

    def create_appointment(
        self, request: CreateAppointmentRequest
    ) -> AppointmentMutationResult:
        """Create a native Tia appointment while keeping ORM details inside the adapter."""
        self._require_local_appointment_write()
        self.require_capability(ClinicCapability.APPOINTMENTS_CREATE)

        patient_id = self._native_uuid(request.patient_id, "patient_id")
        branch_id = self._native_uuid(request.branch_id, "branch_id")
        service_id = self._native_uuid(request.service_id, "service_id")
        doctor_id = self._native_uuid(request.doctor_id, "doctor_id")
        patient_package_id = self._native_uuid(request.patient_package_id, "patient_package_id") if request.patient_package_id else None
        assert patient_id is not None
        assert branch_id is not None
        assert service_id is not None
        assert doctor_id is not None
        requested_start = self._require_aware_start(request.start_at)

        slot = find_exact_slot(
            db=self.db,
            workspace=self.workspace,
            branch_id=branch_id,
            service_id=service_id,
            doctor_id=doctor_id,
            requested_start_at=requested_start,
            laser_device_key=request.laser_device_key,
        )
        patient_package = None
        if patient_package_id is not None:
            try:
                patient_package = validate_package_for_booking(
                    self.db,
                    workspace_id=self.workspace.id,
                    package_id=patient_package_id,
                    patient_id=patient_id,
                    service_id=service_id,
                    appointment_start_at=slot.start_at,
                    laser_device_key=slot.laser_device_key,
                )
            except PackageOperationError as exc:
                raise BookingRuleError(str(exc)) from exc

        settings = get_effective_booking_settings(self.db, self.workspace.id)
        initial_status = "pending" if settings.require_confirmation else "confirmed"

        lead = self.db.scalar(
            select(Lead)
            .where(
                Lead.workspace_id == self.workspace.id,
                Lead.patient_id == patient_id,
                Lead.status.notin_(("lost", "spam", "won")),
                or_(Lead.service_id == service_id, Lead.service_id.is_(None)),
            )
            .order_by(Lead.created_at.desc())
            .limit(1)
        )

        appointment = Appointment(
            workspace_id=self.workspace.id,
            patient_id=patient_id,
            branch_id=branch_id,
            doctor_id=doctor_id,
            service_id=service_id,
            patient_package_id=patient_package_id,
            lead_id=lead.id if lead else None,
            created_by_user_id=None,
            status=initial_status,
            source="ai",
            start_at=slot.start_at,
            end_at=slot.end_at,
            busy_start_at=slot.busy_start_at,
            busy_end_at=slot.busy_end_at,
            duration_minutes=slot.duration_minutes,
            price_minor=slot.price_minor,
            currency=slot.currency,
            laser_device_key=slot.laser_device_key,
            laser_device_name=slot.laser_device_name,
            customer_note=request.customer_note.strip() or None,
            idempotency_key=(
                "agent:"
                f"{request.operation_id}:book:{patient_id}:{doctor_id}:"
                f"{slot.start_at.isoformat()}"
            )[:128],
            confirmed_at=datetime.now(UTC) if initial_status == "confirmed" else None,
        )
        self.db.add(appointment)
        self.db.flush()
        if patient_package is not None:
            reserve_package_usage(
                self.db,
                appointment=appointment,
                package=patient_package,
                actor_type="ai",
                actor_user_id=None,
            )
        self._add_status_history(
            appointment,
            from_status=None,
            to_status=initial_status,
            reason="appointment_created_by_ai",
        )
        if lead is not None:
            if lead.service_id is None:
                lead.service_id = service_id
            lead.status = "booked"
        record_activity_event(
            self.db,
            workspace_id=self.workspace.id,
            actor_type="ai",
            actor_user_id=None,
            action="appointment.created",
            entity_type="appointment",
            entity_id=appointment.id,
            summary="Appointment created by Tia AI",
            metadata={"status": initial_status, "source": appointment.source},
        )
        self.db.flush()
        return AppointmentMutationResult(
            appointment=self._appointment_record(appointment_id=appointment.id)
        )

    def confirm_appointment(
        self, request: ConfirmAppointmentRequest
    ) -> AppointmentMutationResult:
        self._require_local_appointment_write()
        self.require_capability(ClinicCapability.APPOINTMENTS_CONFIRM)
        patient_id = self._native_uuid(request.patient_id, "patient_id")
        appointment_id = self._native_uuid(request.appointment_id, "appointment_id")
        assert patient_id is not None
        assert appointment_id is not None

        try:
            appointment = confirm_appointment_operation(
                self.db,
                workspace_id=self.workspace.id,
                appointment_id=appointment_id,
                patient_id=patient_id,
                changed_by_user_id=None,
                reason="appointment_confirmed_by_ai",
                actor_type="ai",
            )
        except AppointmentOperationNotFound as exc:
            raise ValueError("Appointment not found for this customer.") from exc
        except AppointmentOperationError as exc:
            raise BookingRuleError(str(exc)) from exc
        return AppointmentMutationResult(
            appointment=self._appointment_record(appointment_id=appointment.id)
        )

    def cancel_appointment(
        self, request: CancelAppointmentRequest
    ) -> AppointmentMutationResult:
        self._require_local_appointment_write()
        self.require_capability(ClinicCapability.APPOINTMENTS_CANCEL)
        patient_id = self._native_uuid(request.patient_id, "patient_id")
        appointment_id = self._native_uuid(request.appointment_id, "appointment_id")
        assert patient_id is not None
        assert appointment_id is not None

        try:
            appointment = cancel_appointment_operation(
                self.db,
                workspace=self.workspace,
                appointment_id=appointment_id,
                patient_id=patient_id,
                changed_by_user_id=None,
                reason=request.reason.strip() or "customer_requested_cancellation",
                override_policy=False,
                actor_is_admin=False,
                actor_type="ai",
                now=request.now,
            )
        except AppointmentCancellationOverrideRequired as exc:
            raise ClinicActionRequiresHuman(
                "Cancellation is inside the clinic notice window and needs staff approval.",
                appointment_id=str(appointment_id),
            ) from exc
        except AppointmentOperationNotFound as exc:
            raise ValueError("Appointment not found for this customer.") from exc
        except AppointmentOperationError as exc:
            raise BookingRuleError(str(exc)) from exc
        return AppointmentMutationResult(
            appointment=self._appointment_record(appointment_id=appointment.id)
        )

    def reschedule_appointment(
        self, request: RescheduleAppointmentRequest
    ) -> AppointmentMutationResult:
        self._require_local_appointment_write()
        self.require_capability(ClinicCapability.APPOINTMENTS_RESCHEDULE)
        patient_id = self._native_uuid(request.patient_id, "patient_id")
        appointment_id = self._native_uuid(request.appointment_id, "appointment_id")
        assert patient_id is not None
        assert appointment_id is not None
        requested_start = self._require_aware_start(request.start_at)
        new_branch_id = self._native_uuid(request.branch_id, "branch_id") if request.branch_id else None
        new_doctor_id = self._native_uuid(request.doctor_id, "doctor_id") if request.doctor_id else None
        new_service_id = self._native_uuid(request.service_id, "service_id") if request.service_id else None
        idempotency_key = (
            f"agent:{request.operation_id}:reschedule:{appointment_id}:"
            f"{new_doctor_id or 'same'}:{new_service_id or 'same'}:"
            f"{request.laser_device_key or 'same'}:{requested_start.isoformat()}"
        )[:128]

        try:
            replacement, previous = reschedule_appointment_operation(
                self.db,
                workspace=self.workspace,
                appointment_id=appointment_id,
                patient_id=patient_id,
                requested_start_at=requested_start,
                branch_id=new_branch_id,
                doctor_id=new_doctor_id,
                service_id=new_service_id,
                laser_device_key=request.laser_device_key,
                changed_by_user_id=None,
                reason=request.reason.strip() or "appointment_rescheduled_by_ai",
                idempotency_key=idempotency_key,
                actor_type="ai",
            )
        except AppointmentServiceChangeRequiresHuman as exc:
            raise ClinicActionRequiresHuman(
                str(exc),
                appointment_id=str(appointment_id),
            ) from exc
        except AppointmentOperationNotFound as exc:
            raise ValueError("Appointment not found for this customer.") from exc
        except AppointmentOperationError as exc:
            raise BookingRuleError(str(exc)) from exc

        return AppointmentMutationResult(
            appointment=self._appointment_record(appointment_id=replacement.id),
            previous_appointment_id=str(previous.id),
        )

    def get_patient(self, request: PatientReadRequest) -> PatientRecord:
        self.require_capability(ClinicCapability.PATIENTS_READ)
        patient_id = self._native_uuid(request.patient_id, "patient_id")
        assert patient_id is not None
        patient = self.db.get(Patient, patient_id)
        if patient is None or patient.workspace_id != self.workspace.id:
            raise ValueError("Patient not found.")
        return PatientRecord(
            patient_id=str(patient.id),
            first_name=patient.first_name,
            last_name=patient.last_name,
            phone=patient.phone,
            gender=getattr(patient, "gender", None),
            birth_date=getattr(patient, "birth_date", None),
            status=patient.status,
            preferred_language=patient.preferred_language,
            source=patient.source,
            source_created_at=getattr(patient, "source_created_at", None),
            updated_at=patient.updated_at,
        )

    def get_patient_payments(self, request: PaymentReadRequest) -> PaymentReadResult:
        self.require_capability(ClinicCapability.PAYMENTS_READ)
        patient_id = self._native_uuid(request.patient_id, "patient_id")
        appointment_id = self._native_uuid(request.appointment_id, "appointment_id")
        assert patient_id is not None
        limit = max(1, min(int(request.limit), 200))

        stmt = select(PaymentTransaction).where(
            PaymentTransaction.workspace_id == self.workspace.id,
            PaymentTransaction.patient_id == patient_id,
        )
        if appointment_id is not None:
            stmt = stmt.join(
                PaymentAllocation,
                (PaymentAllocation.workspace_id == PaymentTransaction.workspace_id)
                & (PaymentAllocation.transaction_id == PaymentTransaction.id),
            ).where(PaymentAllocation.appointment_id == appointment_id)
        rows = list(
            self.db.scalars(
                stmt.order_by(PaymentTransaction.created_at.desc(), PaymentTransaction.id.desc()).limit(limit)
            )
        )

        allocations_by_transaction: dict[UUID, list[PaymentAllocationRecord]] = {}
        transaction_ids = [row.id for row in rows]
        if transaction_ids:
            allocation_rows = self.db.execute(
                select(
                    PaymentAllocation.transaction_id,
                    PaymentAllocation.appointment_id,
                    PaymentAllocation.amount_minor,
                ).where(
                    PaymentAllocation.workspace_id == self.workspace.id,
                    PaymentAllocation.transaction_id.in_(transaction_ids),
                )
            ).all()
            for transaction_id, allocated_appointment_id, amount_minor in allocation_rows:
                allocations_by_transaction.setdefault(transaction_id, []).append(
                    PaymentAllocationRecord(
                        appointment_id=str(allocated_appointment_id),
                        amount_minor=int(amount_minor),
                    )
                )

        return PaymentReadResult(
            transactions=tuple(
                PaymentRecord(
                    transaction_id=str(row.id),
                    patient_id=str(row.patient_id),
                    appointment_id=(str(row.appointment_id) if row.appointment_id is not None else None),
                    transaction_type=row.transaction_type,
                    amount_minor=row.amount_minor,
                    currency=row.currency,
                    payment_method=row.payment_method,
                    source=row.source,
                    created_at=row.created_at,
                    external_reference=row.external_reference,
                    reference_transaction_id=(
                        str(row.reference_transaction_id)
                        if row.reference_transaction_id is not None
                        else None
                    ),
                    allocations=tuple(allocations_by_transaction.get(row.id, [])),
                )
                for row in rows
            )
        )

    def get_patient_appointments(
        self, request: AppointmentReadRequest
    ) -> AppointmentReadResult:
        """Read a patient's appointments without leaking native ORM objects upstream.

        The joined query intentionally replaces the old per-appointment service,
        branch, and doctor lookups. The adapter returns one canonical snapshot per
        appointment so callers never need to know Tia's relational schema.
        """
        self.require_capability(ClinicCapability.APPOINTMENTS_READ)

        patient_id = self._native_uuid(request.patient_id, "patient_id")
        assert patient_id is not None
        limit = max(1, min(int(request.limit), 100))
        now = request.now or datetime.now(UTC)

        stmt = (
            select(
                Appointment,
                Service.name,
                Branch.name,
                Branch.timezone,
                Staff.first_name,
                Staff.last_name,
            )
            .join(
                Service,
                (Service.workspace_id == Appointment.workspace_id)
                & (Service.id == Appointment.service_id),
            )
            .join(
                Branch,
                (Branch.workspace_id == Appointment.workspace_id)
                & (Branch.id == Appointment.branch_id),
            )
            .join(
                Doctor,
                (Doctor.workspace_id == Appointment.workspace_id)
                & (Doctor.id == Appointment.doctor_id),
            )
            .join(
                Staff,
                (Staff.workspace_id == Doctor.workspace_id)
                & (Staff.id == Doctor.staff_id),
            )
            .where(
                Appointment.workspace_id == self.workspace.id,
                Appointment.patient_id == patient_id,
            )
        )
        if not request.include_past:
            stmt = stmt.where(
                Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
                Appointment.start_at >= now - timedelta(hours=6),
            )

        rows = self.db.execute(
            stmt.order_by(Appointment.start_at).limit(limit)
        ).all()

        appointments: list[AppointmentRecord] = []
        for appointment, service_name, branch_name, branch_timezone, first_name, last_name in rows:
            timezone_name = branch_timezone or self.workspace.timezone
            try:
                ZoneInfo(timezone_name)
            except Exception:
                timezone_name = "UTC"

            doctor_name = f"{first_name or ''} {last_name or ''}".strip() or None
            appointments.append(
                AppointmentRecord(
                    appointment_id=str(appointment.id),
                    patient_id=str(appointment.patient_id),
                    status=appointment.status,
                    service_id=str(appointment.service_id),
                    service_name=service_name,
                    branch_id=str(appointment.branch_id),
                    branch_name=branch_name,
                    doctor_id=str(appointment.doctor_id),
                    doctor_name=doctor_name,
                    start_at=appointment.start_at,
                    end_at=appointment.end_at,
                    timezone=timezone_name,
                    price_minor=appointment.price_minor,
                    currency=appointment.currency,
                    payment_status=getattr(appointment, "payment_status", "unknown"),
                    amount_paid_minor=getattr(appointment, "amount_paid_minor", None),
                    payment_method=getattr(appointment, "payment_method", "unknown"),
                    billing_context=getattr(appointment, "billing_context", "standard"),
                    package_external_id=getattr(appointment, "package_external_id", None),
                    patient_package_id=(
                        str(appointment.patient_package_id)
                        if getattr(appointment, "patient_package_id", None)
                        else None
                    ),
                    laser_device_key=getattr(appointment, "laser_device_key", None),
                    laser_device_name=getattr(appointment, "laser_device_name", None),
                    visit_group_id=(
                        str(appointment.visit_group_id)
                        if getattr(appointment, "visit_group_id", None)
                        else None
                    ),
                )
            )

        return AppointmentReadResult(appointments=tuple(appointments))

    def build_catalog(self) -> dict[str, Any]:
        """Build the canonical agent catalog from Tia's native clinic tables."""
        db = self.db
        workspace = self.workspace

        services = list(
            db.scalars(
                select(Service)
                .where(
                    Service.workspace_id == workspace.id,
                    Service.is_active.is_(True),
                )
                .order_by(Service.category, Service.name)
            )
        )
        branches = list(
            db.scalars(
                select(Branch)
                .where(
                    Branch.workspace_id == workspace.id,
                    Branch.is_active.is_(True),
                )
                .order_by(Branch.name)
            )
        )
        doctor_rows = list(
            db.execute(
                select(Doctor, Staff)
                .join(
                    Staff,
                    (Staff.workspace_id == Doctor.workspace_id)
                    & (Staff.id == Doctor.staff_id),
                )
                .where(
                    Doctor.workspace_id == workspace.id,
                    Doctor.is_active.is_(True),
                    Doctor.booking_enabled.is_(True),
                    Staff.is_active.is_(True),
                )
                .order_by(Staff.first_name, Staff.last_name)
            ).all()
        )
        doctor_services = list(
            db.scalars(
                select(DoctorService).where(
                    DoctorService.workspace_id == workspace.id,
                    DoctorService.is_active.is_(True),
                )
            )
        )
        doctor_branches = list(
            db.scalars(
                select(DoctorBranch).where(
                    DoctorBranch.workspace_id == workspace.id,
                    DoctorBranch.is_active.is_(True),
                )
            )
        )
        branch_hours = list(
            db.scalars(
                select(BranchWorkingHour)
                .where(BranchWorkingHour.workspace_id == workspace.id)
                .order_by(
                    BranchWorkingHour.branch_id,
                    BranchWorkingHour.weekday,
                    BranchWorkingHour.start_time,
                )
            )
        )
        doctor_hours = list(
            db.scalars(
                select(DoctorWorkingHour)
                .where(DoctorWorkingHour.workspace_id == workspace.id)
                .order_by(
                    DoctorWorkingHour.doctor_id,
                    DoctorWorkingHour.branch_id,
                    DoctorWorkingHour.weekday,
                    DoctorWorkingHour.start_time,
                )
            )
        )

        active_service_ids = {row.id for row in services}
        active_branch_ids = {row.id for row in branches}

        service_ids_by_doctor: dict[UUID, list[str]] = {}
        for row in doctor_services:
            if row.service_id not in active_service_ids:
                continue
            service_ids_by_doctor.setdefault(row.doctor_id, []).append(str(row.service_id))

        branch_ids_by_doctor: dict[UUID, list[str]] = {}
        for row in doctor_branches:
            if row.branch_id not in active_branch_ids:
                continue
            branch_ids_by_doctor.setdefault(row.doctor_id, []).append(str(row.branch_id))

        scheduled_branch_ids_by_doctor: dict[UUID, list[str]] = {}
        for row in doctor_hours:
            if row.branch_id not in active_branch_ids:
                continue
            scheduled_branch_ids_by_doctor.setdefault(row.doctor_id, []).append(
                str(row.branch_id)
            )

        doctor_rows = filter_bookable_doctor_rows(
            doctor_rows,
            service_ids_by_doctor=service_ids_by_doctor,
            branch_ids_by_doctor=branch_ids_by_doctor,
            scheduled_branch_ids_by_doctor=scheduled_branch_ids_by_doctor,
        )
        eligible_doctor_ids = {doctor.id for doctor, _ in doctor_rows}

        hours_by_branch: dict[UUID, list[dict[str, object]]] = {}
        for row in branch_hours:
            if row.branch_id not in active_branch_ids:
                continue
            hours_by_branch.setdefault(row.branch_id, []).append(
                {
                    "weekday": row.weekday,
                    "weekday_name": _WEEKDAY_NAMES[row.weekday],
                    "start": row.start_time.strftime("%H:%M"),
                    "end": row.end_time.strftime("%H:%M"),
                }
            )

        hours_by_doctor: dict[UUID, list[dict[str, object]]] = {}
        for row in doctor_hours:
            if (
                row.doctor_id not in eligible_doctor_ids
                or row.branch_id not in active_branch_ids
            ):
                continue
            hours_by_doctor.setdefault(row.doctor_id, []).append(
                {
                    "branch_id": str(row.branch_id),
                    "weekday": row.weekday,
                    "weekday_name": _WEEKDAY_NAMES[row.weekday],
                    "start": row.start_time.strftime("%H:%M"),
                    "end": row.end_time.strftime("%H:%M"),
                }
            )

        return {
            "services": [
                {
                    "id": str(row.id),
                    "name": row.name,
                    "category": row.category,
                    "description": _description(row.description),
                    "duration_minutes": row.duration_minutes,
                    "price_minor": row.price_minor,
                    "currency": row.currency,
                    "price": _price_display(row.price_minor, row.currency),
                    "requires_medical_review": row.requires_medical_review,
                }
                for row in services
            ],
            "branches": [
                {
                    "id": str(row.id),
                    "name": row.name,
                    "code": row.code,
                    "phone": row.phone,
                    "address_line1": row.address_line1,
                    "address_line2": row.address_line2,
                    "city": row.city,
                    "state": row.state,
                    "country_code": row.country_code,
                    "timezone": row.timezone,
                    "address": "، ".join(
                        part.strip()
                        for part in (row.address_line1, row.address_line2, row.city)
                        if isinstance(part, str) and part.strip()
                    )
                    or None,
                    "working_hours": hours_by_branch.get(row.id, []),
                }
                for row in branches
            ],
            "doctors": [
                {
                    "id": str(doctor.id),
                    "name": f"{staff.first_name} {staff.last_name}".strip(),
                    "specialization": doctor.specialization,
                    "service_ids": sorted(service_ids_by_doctor.get(doctor.id, [])),
                    "branch_ids": sorted(branch_ids_by_doctor.get(doctor.id, [])),
                    "working_hours": hours_by_doctor.get(doctor.id, []),
                }
                for doctor, staff in doctor_rows
            ],
        }
