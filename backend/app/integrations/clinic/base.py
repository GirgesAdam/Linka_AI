from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Hashable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class ClinicCapability(StrEnum):
    """Canonical abilities a clinic source can expose to Linka."""

    CATALOG_READ = "catalog.read"
    AVAILABILITY_READ = "availability.read"
    APPOINTMENTS_READ = "appointments.read"
    APPOINTMENTS_CREATE = "appointments.create"
    APPOINTMENTS_CONFIRM = "appointments.confirm"
    APPOINTMENTS_CANCEL = "appointments.cancel"
    APPOINTMENTS_RESCHEDULE = "appointments.reschedule"
    PATIENTS_READ = "patients.read"
    PAYMENTS_READ = "payments.read"


class ClinicCapabilityNotSupported(ValueError):
    """Raised when a clinic integration cannot safely perform an operation."""


class ClinicActionRequiresHuman(ValueError):
    """Raised when clinic policy requires staff approval before a write can continue."""

    def __init__(self, message: str, *, appointment_id: str | None = None) -> None:
        super().__init__(message)
        self.appointment_id = appointment_id


@dataclass(frozen=True)
class ClinicCapabilities:
    supported: frozenset[ClinicCapability]

    def supports(self, capability: ClinicCapability) -> bool:
        return capability in self.supported

    def require(self, capability: ClinicCapability) -> None:
        if not self.supports(capability):
            raise ClinicCapabilityNotSupported(
                f"Clinic integration does not support {capability.value}."
            )

    def as_dict(self) -> dict[str, bool]:
        return {capability.value: self.supports(capability) for capability in ClinicCapability}


@dataclass(frozen=True)
class AvailabilityRequest:
    branch_id: str
    service_id: str
    booking_date: date
    doctor_id: str | None = None
    exclude_appointment_id: str | None = None
    exclude_appointment_ids: tuple[str, ...] = ()
    now: datetime | None = None
    laser_device_key: str | None = None


@dataclass(frozen=True)
class AvailabilitySlot:
    branch_id: str
    branch_name: str | None
    doctor_id: str
    doctor_name: str | None
    service_id: str
    service_name: str | None
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    price_minor: int
    currency: str
    laser_device_key: str | None = None
    laser_device_name: str | None = None


@dataclass(frozen=True)
class AvailabilityResult:
    timezone: str
    branch_id: str
    branch_name: str | None
    service_id: str
    service_name: str | None
    service_duration_minutes: int | None
    service_price_minor: int | None
    service_currency: str | None
    slots: tuple[AvailabilitySlot, ...]


@dataclass(frozen=True)
class PatientReadRequest:
    patient_id: str


@dataclass(frozen=True)
class PatientRecord:
    patient_id: str
    first_name: str
    last_name: str | None = None
    phone: str | None = None
    gender: str | None = None
    birth_date: date | None = None
    status: str = "active"
    preferred_language: str = "ar"
    source: str = "other"
    source_created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class PaymentReadRequest:
    patient_id: str
    appointment_id: str | None = None
    limit: int = 100


@dataclass(frozen=True)
class PaymentAllocationRecord:
    appointment_id: str
    amount_minor: int


@dataclass(frozen=True)
class PaymentRecord:
    transaction_id: str
    patient_id: str
    appointment_id: str | None
    transaction_type: str
    amount_minor: int
    currency: str
    payment_method: str
    source: str
    created_at: datetime
    external_reference: str | None = None
    reference_transaction_id: str | None = None
    allocations: tuple[PaymentAllocationRecord, ...] = ()


@dataclass(frozen=True)
class PaymentReadResult:
    transactions: tuple[PaymentRecord, ...]


@dataclass(frozen=True)
class AppointmentReadRequest:
    patient_id: str
    include_past: bool = False
    limit: int = 30
    now: datetime | None = None


@dataclass(frozen=True)
class AppointmentRecord:
    appointment_id: str
    patient_id: str
    status: str
    service_id: str
    service_name: str | None
    branch_id: str
    branch_name: str | None
    doctor_id: str
    doctor_name: str | None
    start_at: datetime
    end_at: datetime
    timezone: str
    price_minor: int
    currency: str
    payment_status: str = "unknown"
    amount_paid_minor: int | None = None
    payment_method: str = "unknown"
    billing_context: str = "standard"
    package_external_id: str | None = None
    patient_package_id: str | None = None
    laser_device_key: str | None = None
    laser_device_name: str | None = None
    visit_group_id: str | None = None


@dataclass(frozen=True)
class AppointmentReadResult:
    appointments: tuple[AppointmentRecord, ...]


@dataclass(frozen=True)
class CreateAppointmentRequest:
    patient_id: str
    branch_id: str
    service_id: str
    doctor_id: str
    start_at: datetime
    operation_id: str
    customer_note: str = ""
    patient_package_id: str | None = None
    laser_device_key: str | None = None


@dataclass(frozen=True)
class ConfirmAppointmentRequest:
    patient_id: str
    appointment_id: str
    operation_id: str


@dataclass(frozen=True)
class CancelAppointmentRequest:
    patient_id: str
    appointment_id: str
    operation_id: str
    reason: str = "customer_requested"
    now: datetime | None = None


@dataclass(frozen=True)
class RescheduleAppointmentRequest:
    patient_id: str
    appointment_id: str
    start_at: datetime
    operation_id: str
    branch_id: str | None = None
    doctor_id: str | None = None
    service_id: str | None = None
    laser_device_key: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class AppointmentMutationResult:
    appointment: AppointmentRecord
    previous_appointment_id: str | None = None


class ClinicAdapter(ABC):
    """Canonical boundary between Linka's agent and a clinic's source system."""

    @property
    def cache_namespace(self) -> str:
        return self.__class__.__name__

    @property
    @abstractmethod
    def capabilities(self) -> ClinicCapabilities:
        raise NotImplementedError

    def require_capability(self, capability: ClinicCapability) -> None:
        self.capabilities.require(capability)

    def catalog_revision(self) -> Hashable | None:
        return None

    @abstractmethod
    def build_catalog(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_availability(self, request: AvailabilityRequest) -> AvailabilityResult:
        raise NotImplementedError

    @abstractmethod
    def get_patient_appointments(
        self, request: AppointmentReadRequest
    ) -> AppointmentReadResult:
        raise NotImplementedError

    def get_patient(self, request: PatientReadRequest) -> PatientRecord:
        self.require_capability(ClinicCapability.PATIENTS_READ)
        raise NotImplementedError

    def get_patient_payments(self, request: PaymentReadRequest) -> PaymentReadResult:
        self.require_capability(ClinicCapability.PAYMENTS_READ)
        raise NotImplementedError

    def create_appointment(
        self, request: CreateAppointmentRequest
    ) -> AppointmentMutationResult:
        self.require_capability(ClinicCapability.APPOINTMENTS_CREATE)
        raise NotImplementedError

    def confirm_appointment(
        self, request: ConfirmAppointmentRequest
    ) -> AppointmentMutationResult:
        self.require_capability(ClinicCapability.APPOINTMENTS_CONFIRM)
        raise NotImplementedError

    def cancel_appointment(
        self, request: CancelAppointmentRequest
    ) -> AppointmentMutationResult:
        self.require_capability(ClinicCapability.APPOINTMENTS_CANCEL)
        raise NotImplementedError

    def reschedule_appointment(
        self, request: RescheduleAppointmentRequest
    ) -> AppointmentMutationResult:
        self.require_capability(ClinicCapability.APPOINTMENTS_RESCHEDULE)
        raise NotImplementedError