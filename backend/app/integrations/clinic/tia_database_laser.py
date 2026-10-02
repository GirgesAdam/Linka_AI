from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from sqlalchemy import func, select

from app.integrations.clinic.base import (
    AppointmentMutationResult,
    AppointmentRecord,
    AvailabilityRequest,
    AvailabilityResult,
    AvailabilitySlot,
    CreateAppointmentRequest,
    RescheduleAppointmentRequest,
)
from app.integrations.clinic.tia_database import TiaDatabaseClinicAdapter
from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.clinic_inventory import ClinicLaserDevice, ServiceDevicePrice
from app.models.doctor import Doctor
from app.models.service import Service
from app.models.staff import Staff
from app.services.booking import (
    BookingCompatibilityError,
    BookingRuleError,
    calculate_availability,
    find_exact_slot,
)
from app.services.inventory import (
    DeviceServiceCompatibilityError,
    InventoryOperationError,
    configured_device_price,
    list_laser_device_prices,
)
from app.services.laser_booking_context import current_laser_device_key
from app.services.laser_slot_metadata import encode_laser_slot_branch


class TiaDatabaseLaserClinicAdapter(TiaDatabaseClinicAdapter):
    """Native adapter where a laser device is an independent booking resource.

    A laser slot exists only when the selected doctor and the selected device are
    both free for the same busy interval. Different devices can therefore run in
    parallel with different doctors, while one device can never be double-booked.
    """

    def catalog_revision(self):
        base = super().catalog_revision()
        price_revision = self.db.execute(
            select(
                func.count(ServiceDevicePrice.id),
                func.max(ServiceDevicePrice.updated_at),
            ).where(ServiceDevicePrice.workspace_id == self.workspace.id)
        ).one()
        device_revision = self.db.execute(
            select(
                func.count(ClinicLaserDevice.id),
                func.max(ClinicLaserDevice.updated_at),
            ).where(ClinicLaserDevice.workspace_id == self.workspace.id)
        ).one()
        suffix = (*tuple(price_revision), *tuple(device_revision))
        return (*base, *suffix) if isinstance(base, tuple) else (base, *suffix)

    def build_catalog(self):
        catalog = super().build_catalog()
        prices = list_laser_device_prices(self.db, workspace_id=self.workspace.id)
        by_service: dict[str, list[dict[str, object]]] = {}
        for item in prices:
            by_service.setdefault(str(item.service_id), []).append(
                {
                    "device_key": item.device_key,
                    "device_name": item.device_name,
                    "price_minor": item.price_minor,
                    "duration_minutes": item.duration_minutes,
                    "currency": item.currency,
                    "configured": item.configured,
                }
            )
        services = catalog.get("services")
        if isinstance(services, list):
            for service in services:
                if not isinstance(service, dict):
                    continue
                service_id = str(service.get("service_id") or service.get("id") or "")
                variants = by_service.get(service_id, [])
                service["requires_laser_device"] = bool(variants)
                if variants:
                    service["laser_devices"] = variants
        return catalog

    def _device_price(self, *, service_id: UUID, device_key: str | None):
        try:
            return configured_device_price(
                self.db,
                workspace_id=self.workspace.id,
                service_id=service_id,
                device_key=device_key,
            )
        except DeviceServiceCompatibilityError as exc:
            raise BookingCompatibilityError(str(exc), dimension="device") from exc
        except InventoryOperationError as exc:
            raise BookingRuleError(str(exc)) from exc

    def _configured_devices(self, *, service_id: UUID) -> list[ServiceDevicePrice]:
        return list(
            self.db.scalars(
                select(ServiceDevicePrice)
                .join(
                    ClinicLaserDevice,
                    (ClinicLaserDevice.workspace_id == ServiceDevicePrice.workspace_id)
                    & (ClinicLaserDevice.device_key == ServiceDevicePrice.device_key),
                )
                .where(
                    ServiceDevicePrice.workspace_id == self.workspace.id,
                    ServiceDevicePrice.service_id == service_id,
                    ServiceDevicePrice.is_active.is_(True),
                    ServiceDevicePrice.price_minor.is_not(None),
                    ServiceDevicePrice.duration_minutes.is_not(None),
                    ClinicLaserDevice.is_active.is_(True),
                )
                .order_by(ClinicLaserDevice.name, ServiceDevicePrice.device_key)
            )
        )

    def get_availability(self, request: AvailabilityRequest) -> AvailabilityResult:
        service_id = self._native_uuid(request.service_id, "service_id")
        branch_id = self._native_uuid(request.branch_id, "branch_id")
        doctor_id = self._native_uuid(request.doctor_id, "doctor_id")
        exclude_id = self._native_uuid(request.exclude_appointment_id, "exclude_appointment_id")
        assert service_id is not None
        assert branch_id is not None

        service = self.db.get(Service, service_id)
        if service is None or service.workspace_id != self.workspace.id or not service.is_active:
            raise BookingRuleError("Service not found or inactive.")
        if not bool(getattr(service, "requires_laser_device", False)):
            return super().get_availability(request)

        branch = self.db.get(Branch, branch_id)
        if branch is None or branch.workspace_id != self.workspace.id or not branch.is_active:
            raise BookingRuleError("Branch not found or inactive.")

        selected_device = request.laser_device_key or current_laser_device_key()
        if selected_device:
            device_prices = [self._device_price(service_id=service_id, device_key=selected_device)]
        else:
            device_prices = self._configured_devices(service_id=service_id)
        device_prices = [item for item in device_prices if item is not None]
        if not device_prices:
            raise BookingRuleError("Laser device prices are not configured for this service.")

        native_rows: list[tuple[object, ServiceDevicePrice]] = []
        timezone_name = branch.timezone or self.workspace.timezone
        for device_price in device_prices:
            timezone_name, slots = calculate_availability(
                db=self.db,
                workspace=self.workspace,
                branch_id=branch_id,
                service_id=service_id,
                booking_date=request.booking_date,
                doctor_id=doctor_id,
                exclude_appointment_id=exclude_id,
                now=request.now,
                preloaded_branch=branch,
                preloaded_service=service,
                laser_device_key=device_price.device_key,
            )
            native_rows.extend((slot, device_price) for slot in slots)

        doctor_ids = {slot.doctor_id for slot, _ in native_rows}
        doctor_names: dict[UUID, str] = {}
        if doctor_ids:
            rows = self.db.execute(
                select(Doctor.id, Staff.first_name, Staff.last_name)
                .join(
                    Staff,
                    (Staff.workspace_id == Doctor.workspace_id)
                    & (Staff.id == Doctor.staff_id),
                )
                .where(
                    Doctor.workspace_id == self.workspace.id,
                    Doctor.id.in_(doctor_ids),
                )
            ).all()
            for doctor_id_value, first_name, last_name in rows:
                name = f"{first_name or ''} {last_name or ''}".strip()
                doctor_names[doctor_id_value] = name or "الدكتور المتاح"

        slots = tuple(
            AvailabilitySlot(
                branch_id=str(slot.branch_id),
                branch_name=encode_laser_slot_branch(
                    branch.name,
                    device_key=device_price.device_key,
                    device_name=device_price.device_name,
                ),
                doctor_id=str(slot.doctor_id),
                doctor_name=doctor_names.get(slot.doctor_id, "الدكتور المتاح"),
                service_id=str(slot.service_id),
                service_name=service.name,
                start_at=slot.start_at,
                end_at=slot.end_at,
                duration_minutes=slot.duration_minutes,
                price_minor=int(device_price.price_minor or 0),
                currency=device_price.currency,
                laser_device_key=device_price.device_key,
                laser_device_name=device_price.device_name,
            )
            for slot, device_price in sorted(
                native_rows,
                key=lambda row: (row[0].start_at, row[1].device_key, str(row[0].doctor_id)),
            )
        )
        one_device = device_prices[0] if len(device_prices) == 1 else None
        return AvailabilityResult(
            timezone=timezone_name,
            branch_id=str(branch.id),
            branch_name=branch.name,
            service_id=str(service.id),
            service_name=service.name,
            service_duration_minutes=(
                int(one_device.duration_minutes) if one_device is not None else None
            ),
            service_price_minor=(int(one_device.price_minor or 0) if one_device else None),
            service_currency=(one_device.currency if one_device else service.currency),
            slots=slots,
        )

    def _appointment_record(self, *, appointment_id: UUID) -> AppointmentRecord:
        record = super()._appointment_record(appointment_id=appointment_id)
        appointment = self.db.get(Appointment, appointment_id)
        if appointment is None or appointment.workspace_id != self.workspace.id:
            return record
        return replace(
            record,
            branch_name=encode_laser_slot_branch(
                record.branch_name,
                device_key=appointment.laser_device_key,
                device_name=appointment.laser_device_name,
            ),
            laser_device_key=appointment.laser_device_key,
            laser_device_name=appointment.laser_device_name,
        )

    def create_appointment(self, request: CreateAppointmentRequest) -> AppointmentMutationResult:
        service_id = self._native_uuid(request.service_id, "service_id")
        branch_id = self._native_uuid(request.branch_id, "branch_id")
        doctor_id = self._native_uuid(request.doctor_id, "doctor_id")
        assert service_id is not None
        assert branch_id is not None
        assert doctor_id is not None
        service = self.db.get(Service, service_id)
        if service is None or service.workspace_id != self.workspace.id:
            raise BookingRuleError("Service not found.")
        if not bool(getattr(service, "requires_laser_device", False)):
            return super().create_appointment(request)

        device_key = request.laser_device_key or current_laser_device_key()
        device_price = self._device_price(service_id=service_id, device_key=device_key)
        assert device_price is not None
        find_exact_slot(
            db=self.db,
            workspace=self.workspace,
            branch_id=branch_id,
            service_id=service_id,
            doctor_id=doctor_id,
            requested_start_at=request.start_at,
            laser_device_key=device_price.device_key,
        )
        result = super().create_appointment(
            replace(request, laser_device_key=device_price.device_key)
        )
        appointment_id = UUID(result.appointment.appointment_id)
        appointment = self.db.get(Appointment, appointment_id)
        if appointment is None:
            raise BookingRuleError("Appointment disappeared before laser device persistence.")
        appointment.laser_device_key = device_price.device_key
        appointment.laser_device_name = device_price.device_name
        appointment.price_minor = int(device_price.price_minor or 0)
        appointment.currency = device_price.currency
        self.db.flush()
        return AppointmentMutationResult(
            appointment=self._appointment_record(appointment_id=appointment.id)
        )

    def reschedule_appointment(self, request: RescheduleAppointmentRequest) -> AppointmentMutationResult:
        appointment_id = self._native_uuid(request.appointment_id, "appointment_id")
        patient_id = self._native_uuid(request.patient_id, "patient_id")
        assert appointment_id is not None
        assert patient_id is not None
        current = self._patient_appointment(patient_id=patient_id, appointment_id=appointment_id)
        target_service_id = self._native_uuid(request.service_id, "service_id") or current.service_id
        target_branch_id = self._native_uuid(request.branch_id, "branch_id") or current.branch_id
        target_doctor_id = self._native_uuid(request.doctor_id, "doctor_id") or current.doctor_id
        service = self.db.get(Service, target_service_id)
        if service is None or service.workspace_id != self.workspace.id:
            raise BookingRuleError("Replacement service not found.")

        device_price = None
        if bool(getattr(service, "requires_laser_device", False)):
            device_key = current_laser_device_key() or current.laser_device_key
            device_price = self._device_price(service_id=target_service_id, device_key=device_key)
            assert device_price is not None
            find_exact_slot(
                db=self.db,
                workspace=self.workspace,
                branch_id=target_branch_id,
                service_id=target_service_id,
                doctor_id=target_doctor_id,
                requested_start_at=request.start_at,
                exclude_appointment_id=current.id,
                laser_device_key=device_price.device_key,
            )

        effective_request = replace(
            request,
            laser_device_key=(device_price.device_key if device_price is not None else None),
        )
        result = super().reschedule_appointment(effective_request)
        replacement_id = UUID(result.appointment.appointment_id)
        replacement = self.db.get(Appointment, replacement_id)
        if replacement is None:
            raise BookingRuleError("Replacement appointment not found.")
        if device_price is None:
            replacement.laser_device_key = None
            replacement.laser_device_name = None
        else:
            replacement.laser_device_key = device_price.device_key
            replacement.laser_device_name = device_price.device_name
            replacement.price_minor = int(device_price.price_minor or 0)
            replacement.currency = device_price.currency
        self.db.flush()
        return AppointmentMutationResult(
            appointment=self._appointment_record(appointment_id=replacement.id),
            previous_appointment_id=result.previous_appointment_id,
        )
