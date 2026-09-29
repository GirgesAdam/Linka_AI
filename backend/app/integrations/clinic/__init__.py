"""Canonical clinic-system integration boundary used by the Linka runtime."""

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
    ClinicCapabilityNotSupported,
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
from app.integrations.clinic.configuration import (
    ClinicIntegrationConfig,
    get_canonical_entity_id,
    get_clinic_integration_config,
    get_external_entity_id,
)
from app.integrations.clinic.prototype_external import (
    PrototypeExternalClinicAdapter,
    PrototypeExternalConfigurationError,
)
from app.integrations.clinic.registry import (
    ClinicAdapterConfigurationError,
    build_clinic_adapter,
    get_clinic_adapter,
    registered_clinic_adapter_keys,
)
from app.integrations.clinic.sync_contract import (
    ClinicConnectorMappingRuntimeValidator,
    ClinicConnectorReferenceSource,
    ClinicConnectorSchemaSource,
    ClinicRawSyncFetchRequest,
    ClinicRawSyncPage,
    ClinicRawSyncSource,
    ClinicReferenceCandidate,
    ClinicSyncDomain,
    ClinicSyncFetchRequest,
    ClinicSyncPage,
    ClinicSyncSource,
    ExternalAppointmentSyncRecord,
    ExternalPatientSyncRecord,
    ExternalPaymentAllocationSyncRecord,
    ExternalPaymentSyncRecord,
)


def __getattr__(name: str):
    if name == "TiaDatabaseClinicAdapter":
        from app.integrations.clinic.tia_database import TiaDatabaseClinicAdapter

        return TiaDatabaseClinicAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AppointmentMutationResult",
    "AppointmentReadRequest",
    "AppointmentReadResult",
    "AppointmentRecord",
    "AvailabilityRequest",
    "AvailabilityResult",
    "AvailabilitySlot",
    "CancelAppointmentRequest",
    "ClinicActionRequiresHuman",
    "ClinicAdapter",
    "ClinicAdapterConfigurationError",
    "build_clinic_adapter",
    "ClinicIntegrationConfig",
    "ClinicCapabilities",
    "ClinicCapability",
    "ClinicCapabilityNotSupported",
    "ConfirmAppointmentRequest",
    "CreateAppointmentRequest",
    "PatientReadRequest",
    "PatientRecord",
    "PaymentAllocationRecord",
    "PaymentReadRequest",
    "PaymentReadResult",
    "PaymentRecord",
    "RescheduleAppointmentRequest",
    "PrototypeExternalClinicAdapter",
    "PrototypeExternalConfigurationError",
    "TiaDatabaseClinicAdapter",
    "ClinicConnectorSchemaSource",
    "ClinicConnectorMappingRuntimeValidator",
    "ClinicConnectorReferenceSource",
    "ClinicReferenceCandidate",
    "ClinicRawSyncFetchRequest",
    "ClinicRawSyncPage",
    "ClinicRawSyncSource",
    "ClinicSyncDomain",
    "ClinicSyncFetchRequest",
    "ClinicSyncPage",
    "ClinicSyncSource",
    "ExternalAppointmentSyncRecord",
    "ExternalPatientSyncRecord",
    "ExternalPaymentAllocationSyncRecord",
    "ExternalPaymentSyncRecord",
    "get_canonical_entity_id",
    "get_clinic_adapter",
    "get_clinic_integration_config",
    "get_external_entity_id",
    "registered_clinic_adapter_keys",
]
