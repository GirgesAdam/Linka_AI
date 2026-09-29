from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ClinicSyncDomain(StrEnum):
    """Canonical domains that Phase 6.2c can synchronize into Linka.

    The connector/extractor owns vendor-specific reads and structural mapping.
    The sync engine only consumes these canonical facts.
    """

    PATIENTS = "patients"
    PAYMENTS = "payments"
    APPOINTMENTS = "appointments"


@dataclass(frozen=True)
class ExternalPatientSyncRecord:
    external_id: str
    first_name: str
    last_name: str | None = None
    phone: str | None = None
    gender: str | None = None
    birth_date: date | None = None
    status: str = "active"
    preferred_language: str = "ar"
    source: str = "other"
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None


@dataclass(frozen=True)
class ExternalPaymentAllocationSyncRecord:
    appointment_external_id: str
    amount_minor: int


@dataclass(frozen=True)
class ExternalPaymentSyncRecord:
    external_id: str
    patient_external_id: str
    transaction_type: str
    amount_minor: int
    currency: str
    payment_method: str
    created_at: datetime
    external_reference: str | None = None
    reference_payment_external_id: str | None = None
    allocations: tuple[ExternalPaymentAllocationSyncRecord, ...] = ()


@dataclass(frozen=True)
class ExternalAppointmentSyncRecord:
    external_id: str
    patient_external_id: str
    branch_external_id: str
    service_external_id: str
    doctor_external_id: str
    status: str
    start_at: datetime
    end_at: datetime
    status_at: datetime | None = None
    price_minor: int | None = None
    currency: str | None = None
    source_updated_at: datetime | None = None


ClinicSyncRecord = ExternalPatientSyncRecord | ExternalPaymentSyncRecord | ExternalAppointmentSyncRecord


@dataclass(frozen=True)
class ClinicSyncPage:
    """One deterministic page emitted by a connector/extractor.

    ``cursor`` is the cursor used to fetch this page. ``next_cursor`` becomes
    durable only when every record in the page succeeds. Partial pages keep the
    old checkpoint so a later retry can replay the same page idempotently.
    """

    domain: ClinicSyncDomain
    records: tuple[ClinicSyncRecord, ...]
    cursor: str | None = None
    next_cursor: str | None = None
    source_revision: str | None = None
    has_more: bool = False

@dataclass(frozen=True)
class ClinicSyncFetchRequest:
    """Ask an installed connector for one deterministic canonical sync page."""

    domain: ClinicSyncDomain
    cursor: str | None = None
    limit: int = 100




@dataclass(frozen=True)
class ClinicRawSyncPage:
    """Vendor/raw page used only behind an approved structural mapping.

    ``tables`` may include the paged fact table plus deterministic lookup slices
    needed by joins. Linka core never consumes these rows directly.
    """

    domain: ClinicSyncDomain
    tables: dict[str, tuple[dict[str, Any], ...]]
    schema_fingerprint: str
    cursor: str | None = None
    next_cursor: str | None = None
    source_revision: str | None = None
    has_more: bool = False


@dataclass(frozen=True)
class ClinicRawSyncFetchRequest:
    domain: ClinicSyncDomain
    cursor: str | None = None
    limit: int = 100


@runtime_checkable
class ClinicRawSyncSource(Protocol):
    @property
    def raw_sync_domains(self) -> frozenset[ClinicSyncDomain]: ...

    def fetch_raw_sync_page(self, request: ClinicRawSyncFetchRequest) -> ClinicRawSyncPage: ...


@runtime_checkable
class ClinicConnectorSchemaSource(Protocol):
    def discover_sync_schema(self): ...


@runtime_checkable
class ClinicConnectorSampleSource(Protocol):
    """Optional bounded, ephemeral onboarding samples for structural disambiguation."""

    def sample_sync_schema_rows(
        self, *, rows_per_table: int = 3, max_tables: int = 30
    ) -> dict[str, tuple[dict[str, Any], ...]]: ...


@runtime_checkable
class ClinicConnectorMappingRuntimeValidator(Protocol):
    def validate_sync_mapping_runtime(self, mapping: Any, snapshot: Any) -> None: ...


@dataclass(frozen=True)
class ClinicReferenceCandidate:
    entity_type: str
    external_id: str
    label: str | None = None


@runtime_checkable
class ClinicConnectorReferenceSource(Protocol):
    def list_reference_candidates(
        self, *, mapping: Any, entity_type: str, limit: int = 500
    ) -> tuple[ClinicReferenceCandidate, ...]: ...


@runtime_checkable
class ClinicSyncSource(Protocol):
    """Optional connector capability used by the scheduled sync runtime.

    Vendor-specific extraction and structural transformation stay behind this
    boundary. The runtime receives canonical ``ClinicSyncPage`` objects only.
    """

    @property
    def sync_domains(self) -> frozenset[ClinicSyncDomain]: ...

    def fetch_sync_page(self, request: ClinicSyncFetchRequest) -> ClinicSyncPage: ...

