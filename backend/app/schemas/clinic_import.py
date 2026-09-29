from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ClinicImportDocument(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    format: Literal["csv", "xlsx"]
    content_base64: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Document name cannot be empty.")
        return value


class ServiceSheetMapping(BaseModel):
    sheet: str
    external_id: str | None = None
    name: str
    duration_minutes: str | None = None
    default_duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    price: str | None = None
    default_price: float = Field(default=0, ge=0)
    # Backward-compatible only. Imported clinics are normalized to EGP.
    currency: str | None = None
    default_currency: str = Field(default="EGP", min_length=3, max_length=3)
    category: str | None = None


class BranchSheetMapping(BaseModel):
    sheet: str
    external_id: str | None = None
    name: str
    city: str | None = None
    address: str | None = None
    timezone: str | None = None
    default_timezone: str | None = None


class DoctorSheetMapping(BaseModel):
    sheet: str
    external_id: str | None = None
    name: str
    # Despite the legacy names, these columns may contain either stable source IDs
    # or human-readable names. Preview resolves them against the imported catalog.
    service_external_ids: str | None = None
    branch_external_ids: str | None = None
    specialization: str | None = None
    delimiter: str = Field(default="|", min_length=1, max_length=4)


class BranchHoursSheetMapping(BaseModel):
    sheet: str
    branch_external_id: str
    weekday: str
    start_time: str
    end_time: str


class DoctorHoursSheetMapping(BaseModel):
    sheet: str
    doctor_external_id: str
    branch_external_id: str
    weekday: str
    start_time: str
    end_time: str


# Integration-facing lifecycle is deliberately smaller than Linka's internal
# operational appointment state machine.
ImportAppointmentLifecycle = Literal[
    "scheduled",
    "completed",
    "cancelled",
    "no_show",
    "unknown",
]

# Backward-compatible status values accepted from mappings created before v0.21.2.1.
AppointmentStatusInput = Literal[
    "scheduled",
    "completed",
    "cancelled",
    "no_show",
    "unknown",
    "pending",
    "confirmed",
    "checked_in",
    "in_progress",
    "rescheduled",
]

# Kept as an import alias for Phase 3.3 schemas/tests; new code should use
# ImportAppointmentLifecycle.
CanonicalAppointmentStatus = ImportAppointmentLifecycle

PaymentStatus = Literal["unknown", "unpaid", "partial", "paid", "refunded"]
PaymentMethod = Literal["unknown", "cash", "card", "bank_transfer", "wallet", "other"]
BillingContext = Literal["standard", "package_prepaid", "pulse_prepaid"]
PackageStatus = Literal["active", "expired", "cancelled"]
PackageUsageStatus = Literal["reserved", "consumed", "released"]
AppointmentSource = Literal["ai", "staff", "whatsapp", "instagram", "website", "phone", "walk_in", "facebook", "email", "other"]
ReferenceKind = Literal["service", "branch", "doctor"]
AppointmentTimePrecision = Literal["exact", "date_only"]


class AppointmentSheetMapping(BaseModel):
    sheet: str
    external_id: str | None = None
    patient_external_id: str | None = None
    patient_name: str
    patient_phone: str | None = None
    # Legacy field names; source values may be IDs or display names.
    service_external_id: str
    branch_external_id: str | None = None
    # Optional source-level branch reference when one appointment sheet/file belongs to one branch.
    # This is a branch catalog value, not a source column name.
    default_branch_external_id: str | None = None
    doctor_external_id: str | None = None

    # Supported time shapes:
    # 1) one datetime cell (`start_at`), optionally plus `end_at`
    # 2) separate date + time cells, optionally plus `end_time`
    # 3) date-only (previewed but not booking-safe until exact time is supplied)
    start_at: str | None = None
    appointment_date: str | None = None
    appointment_time: str | None = None
    end_at: str | None = None
    end_time: str | None = None

    status: str | None = None
    status_map: dict[str, AppointmentStatusInput] = Field(default_factory=dict)
    default_status: AppointmentStatusInput = "unknown"

    payment_status: str | None = None
    payment_status_map: dict[str, PaymentStatus] = Field(default_factory=dict)
    default_payment_status: PaymentStatus = "unknown"
    amount_paid: str | None = None
    payment_method: str | None = None
    payment_method_map: dict[str, PaymentMethod] = Field(default_factory=dict)
    default_payment_method: PaymentMethod = "unknown"

    # Optional financial context embedded in the appointment export. A prepaid package
    # means the session is already covered but must not create appointment revenue.
    payment_context: str | None = None
    payment_context_map: dict[str, BillingContext] = Field(default_factory=dict)
    default_payment_context: BillingContext = "standard"
    package_external_id: str | None = None
    payment_reference: str | None = None
    refund_amount: str | None = None
    refund_reason: str | None = None
    refunded_at: str | None = None

    source: str | None = None
    source_map: dict[str, AppointmentSource] = Field(default_factory=dict)
    default_source: AppointmentSource = "other"

    default_timezone: str | None = None

    @model_validator(mode="after")
    def validate_time_shape(self):
        if not self.start_at and not self.appointment_date:
            raise ValueError(
                "appointments require either start_at or appointment_date mapping."
            )
        if self.appointment_time and not self.appointment_date:
            raise ValueError("appointment_time requires appointment_date.")
        if self.end_time and not self.appointment_date:
            raise ValueError("end_time requires appointment_date.")
        return self



class PaymentSheetMapping(BaseModel):
    """Optional standalone payment source.

    Clinics are not required to have a payment sheet; embedded appointment payment
    fields remain supported. When present, a standalone payment is an immutable
    patient-level financial fact and appointment linkage is optional/explicit.
    """

    sheet: str
    external_id: str
    patient_external_id: str | None = None
    patient_phone: str | None = None
    appointment_external_id: str | None = None
    package_external_id: str | None = None
    amount: str
    payment_method: str | None = None
    payment_method_map: dict[str, PaymentMethod] = Field(default_factory=dict)
    default_payment_method: PaymentMethod = "unknown"
    payment_reference: str | None = None
    paid_at: str
    default_timezone: str | None = None

    @model_validator(mode="after")
    def validate_identity_shape(self):
        if not (
            self.patient_external_id
            or self.patient_phone
            or self.appointment_external_id
            or self.package_external_id
        ):
            raise ValueError(
                "standalone payments require patient_external_id, patient_phone, appointment_external_id, or package_external_id."
            )
        return self


class PaymentAllocationSheetMapping(BaseModel):
    """Optional explicit distribution of a standalone payment across appointments."""

    sheet: str
    payment_external_id: str
    appointment_external_id: str
    amount: str


class PackageSheetMapping(BaseModel):
    """Optional prepaid package-sale source."""

    sheet: str
    external_id: str
    patient_external_id: str | None = None
    patient_phone: str | None = None
    service_external_id: str
    name: str
    sessions_purchased: str
    sale_price: str | None = None
    standalone_session_price_at_purchase: str | None = None
    sold_at: str
    expires_at: str | None = None
    status: str | None = None
    status_map: dict[str, PackageStatus] = Field(default_factory=dict)
    default_status: PackageStatus = "active"
    default_timezone: str | None = None

    @model_validator(mode="after")
    def validate_patient_identity(self):
        if not (self.patient_external_id or self.patient_phone):
            raise ValueError("packages require patient_external_id or patient_phone.")
        return self


class PackageUsageSheetMapping(BaseModel):
    """Optional explicit package-session usage source."""

    sheet: str
    external_id: str | None = None
    package_external_id: str
    appointment_external_id: str
    sessions_used: str | None = None
    default_sessions_used: int = Field(default=1, ge=1, le=100)
    used_at: str | None = None
    default_timezone: str | None = None


class ClinicImportReferenceMappingOverride(BaseModel):
    """Administrator-confirmed mapping from one raw source label to a canonical imported entity.

    The mapping is intentionally value-based and global to the current import batch. It does not
    mutate patient/entity identity and is never inferred by the deterministic import engine.
    """

    reference_kind: ReferenceKind
    source_value: str = Field(min_length=1, max_length=255)
    target_external_id: str = Field(min_length=1, max_length=255)

    @field_validator("source_value", "target_external_id")
    @classmethod
    def clean_reference_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reference mapping values cannot be empty.")
        return value


class ClinicImportWorkingIntervalOverride(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: str
    end_time: str

    @model_validator(mode="after")
    def validate_interval(self):
        from datetime import time

        try:
            start = time.fromisoformat(self.start_time)
            end = time.fromisoformat(self.end_time)
        except ValueError as exc:
            raise ValueError("Override working-hour times must use HH:MM or HH:MM:SS.") from exc
        if end <= start:
            raise ValueError("Override working-hour end time must be after start time.")
        return self


class ClinicImportDoctorHoursOverride(BaseModel):
    doctor_external_id: str = Field(min_length=1)
    branch_external_id: str = Field(min_length=1)
    schedule: list[ClinicImportWorkingIntervalOverride] = Field(default_factory=list, max_length=14)


class ClinicImportDoctorBranchReference(BaseModel):
    doctor_external_id: str = Field(min_length=1)
    branch_external_id: str = Field(min_length=1)


class ClinicImportOverrides(BaseModel):
    """Deterministic onboarding fixes applied before preview validation.

    Most values come from administrator-confirmed missing-data fixes. Onboarding
    may also add system-owned quarantine exclusions for ambiguous row/entity facts
    so safe clinic data can import without materializing unverified financial truth.
    """

    service_durations: dict[str, int] = Field(default_factory=dict)
    doctor_service_assignments: dict[str, list[str]] = Field(default_factory=dict)
    doctor_branch_assignments: dict[str, list[str]] = Field(default_factory=dict)
    doctor_active_status: dict[str, bool] = Field(default_factory=dict)
    branch_hours: dict[str, list[ClinicImportWorkingIntervalOverride]] = Field(default_factory=dict)
    doctor_hours: list[ClinicImportDoctorHoursOverride] = Field(default_factory=list, max_length=500)
    appointment_status_map: dict[str, ImportAppointmentLifecycle] = Field(default_factory=dict)
    appointment_payment_status_map: dict[str, PaymentStatus] = Field(default_factory=dict)
    appointment_payment_method_map: dict[str, PaymentMethod] = Field(default_factory=dict)
    appointment_source_map: dict[str, AppointmentSource] = Field(default_factory=dict)
    reference_mappings: list[ClinicImportReferenceMappingOverride] = Field(default_factory=list, max_length=1000)
    # Import-repair overrides are administrator-approved corrections applied only to
    # the current import batch. They let Linka repair contradictory legacy exports
    # without forcing the clinic to edit Excel/CSV files and re-upload them.
    package_patient_assignments: dict[str, str] = Field(default_factory=dict)
    package_service_assignments: dict[str, str] = Field(default_factory=dict)
    package_usage_assignments: dict[str, str] = Field(default_factory=dict)
    excluded_package_usage_appointments: list[str] = Field(default_factory=list, max_length=5000)
    # System-owned quarantine used by onboarding for row/entity-level conflicts that
    # are unsafe to materialize but should not block the rest of the clinic import.
    # The original source stays unchanged; deferred facts are surfaced after setup.
    deferred_package_external_ids: list[str] = Field(default_factory=list, max_length=5000)
    deferred_payment_external_ids: list[str] = Field(default_factory=list, max_length=10000)
    # Appointment rows whose catalog references are still genuinely ambiguous after
    # Linka's fast alias pass. They are preserved in the post-setup data-issue inbox
    # and omitted from the current materialization so onboarding can finish without
    # inventing a service/branch/doctor relationship.
    deferred_appointment_external_ids: list[str] = Field(default_factory=list, max_length=20000)
    # A doctor/branch pair can be imported without current availability when there
    # is no trustworthy schedule evidence. This keeps onboarding non-blocking while
    # booking for that pair stays disabled until the administrator sets hours later.
    deferred_doctor_hour_pairs: list[ClinicImportDoctorBranchReference] = Field(
        default_factory=list, max_length=2000
    )
    # Operational catalog gaps with no trustworthy historical evidence are allowed
    # to materialize without booking availability. They are persisted as post-setup
    # data issues instead of blocking onboarding.
    deferred_branch_hour_external_ids: list[str] = Field(default_factory=list, max_length=1000)
    deferred_doctor_service_external_ids: list[str] = Field(default_factory=list, max_length=2000)
    deferred_doctor_branch_external_ids: list[str] = Field(default_factory=list, max_length=2000)
    confirm_no_existing_appointments: bool | None = None

    @field_validator("service_durations")
    @classmethod
    def validate_service_durations(cls, value: dict[str, int]) -> dict[str, int]:
        for key, minutes in value.items():
            if not str(key).strip():
                raise ValueError("service_durations keys cannot be empty.")
            if minutes < 1 or minutes > 1440:
                raise ValueError("service durations must be between 1 and 1440 minutes.")
        return value


StructuralJoinType = Literal["left", "inner"]
StructuralJoinCardinality = Literal["one", "many"]
StructuralFieldKind = Literal["column", "coalesce", "literal", "concat"]
StructuralAggregateOperation = Literal["sum", "count", "min", "max"]
StructuralUnmappedPolicy = Literal["keep", "default", "error"]
StructuralScalar = str | int | float | bool | None


class StructuralJoinKey(BaseModel):
    left: str = Field(min_length=1, max_length=255)
    right: str = Field(min_length=1, max_length=255)


class StructuralJoinMapping(BaseModel):
    sheet: str = Field(min_length=1, max_length=255)
    alias: str = Field(min_length=1, max_length=80)
    on: list[StructuralJoinKey] = Field(min_length=1, max_length=8)
    how: StructuralJoinType = "left"
    cardinality: StructuralJoinCardinality = "one"


class StructuralFieldMapping(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: StructuralFieldKind = "column"
    source: str | None = Field(default=None, max_length=255)
    sources: list[str] = Field(default_factory=list, max_length=12)
    value: StructuralScalar = None
    separator: str = Field(default=" ", max_length=16)
    enum_map: dict[str, StructuralScalar] = Field(default_factory=dict, max_length=100)
    enum_case_sensitive: bool = False
    unmapped: StructuralUnmappedPolicy = "keep"
    default: StructuralScalar = None

    @model_validator(mode="after")
    def validate_expression(self):
        if self.kind == "column":
            if not self.source or self.sources:
                raise ValueError("column structural fields require source and no sources list.")
        elif self.kind in {"coalesce", "concat"}:
            if not self.sources or self.source:
                raise ValueError(f"{self.kind} structural fields require sources and no source field.")
        elif self.kind == "literal":
            if self.source or self.sources:
                raise ValueError("literal structural fields cannot reference source columns.")
        return self


class StructuralAggregateMapping(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    operation: StructuralAggregateOperation
    source: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_aggregate(self):
        if self.operation != "count" and not self.source:
            raise ValueError(f"{self.operation} aggregate requires a source column.")
        return self


class StructuralTransformMapping(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    source_sheet: str = Field(min_length=1, max_length=255)
    source_alias: str = Field(default="source", min_length=1, max_length=80)
    joins: list[StructuralJoinMapping] = Field(default_factory=list, max_length=12)
    fields: list[StructuralFieldMapping] = Field(min_length=1, max_length=80)
    group_by: list[str] = Field(default_factory=list, max_length=40)
    aggregates: list[StructuralAggregateMapping] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def validate_output_shape(self):
        aliases = [self.source_alias, *(join.alias for join in self.joins)]
        if len(set(aliases)) != len(aliases):
            raise ValueError("structural transform aliases must be unique.")
        output_names = [field.name for field in self.fields] + [
            aggregate.name for aggregate in self.aggregates
        ]
        if len(set(output_names)) != len(output_names):
            raise ValueError("structural transform output field names must be unique.")
        field_names = {field.name for field in self.fields}
        if self.aggregates:
            if not self.group_by:
                raise ValueError("structural transforms with aggregates require group_by fields.")
            unknown = [name for name in self.group_by if name not in field_names]
            if unknown:
                raise ValueError(f"group_by references unknown output fields: {unknown!r}.")
            non_grouped = field_names - set(self.group_by)
            if non_grouped:
                raise ValueError(
                    "all non-aggregate structural fields must be included in group_by when aggregates are used."
                )
        elif self.group_by:
            raise ValueError("group_by is only valid when aggregates are configured.")
        return self


class ClinicImportMapping(BaseModel):
    services: ServiceSheetMapping
    branches: BranchSheetMapping
    doctors: DoctorSheetMapping | None = None
    branch_hours: BranchHoursSheetMapping | None = None
    doctor_hours: DoctorHoursSheetMapping | None = None
    # Backward-compatible primary appointment source. Older saved mappings use this field.
    appointments: AppointmentSheetMapping | None = None
    # Additional appointment exports may use completely different schemas (for example,
    # a legacy booking system plus a current one). Each source gets its own mapping.
    appointment_sources: list[AppointmentSheetMapping] = Field(default_factory=list, max_length=100)
    # Optional financial sources. Most clinics can keep payment facts embedded in
    # appointment exports; these sections are used only when separate financial
    # tables/files actually exist.
    payments: PaymentSheetMapping | None = None
    payment_allocations: PaymentAllocationSheetMapping | None = None
    packages: PackageSheetMapping | None = None
    package_usages: PackageUsageSheetMapping | None = None
    transformations: list[StructuralTransformMapping] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_mapping_shape(self):
        names = [item.name for item in self.transformations]
        if len(set(names)) != len(names):
            raise ValueError("structural transform names must be unique.")

        appointment_mappings = [
            item for item in [self.appointments, *self.appointment_sources] if item is not None
        ]
        sheets = [item.sheet for item in appointment_mappings]
        if len(set(sheets)) != len(sheets):
            raise ValueError(
                "Each appointment source sheet can be mapped only once. Remove duplicate appointment mappings."
            )
        return self


class ClinicImportPreviewRequest(BaseModel):
    documents: list[ClinicImportDocument] = Field(min_length=1, max_length=100)
    mapping: ClinicImportMapping
    confirm_no_existing_appointments: bool = False
    overrides: ClinicImportOverrides = Field(default_factory=ClinicImportOverrides)


class ClinicImportApplyRequest(ClinicImportPreviewRequest):
    source_label: str = Field(default="tabular_import", min_length=1, max_length=120)


class ClinicImportIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    path: str
    message: str
    # Aggregated preview issues represent one review decision, not one source row.
    occurrence_count: int = Field(default=1, ge=1)
    source_value: str | None = None
    reference_kind: ReferenceKind | None = None
    source_sheets: list[str] = Field(default_factory=list, max_length=100)
    example_paths: list[str] = Field(default_factory=list, max_length=5)
    # Optional repair metadata prepared by the deterministic preview. The frontend
    # renders this as simple administrator choices; every submitted fix is validated
    # again by the onboarding service before it becomes an import override.
    repair_group: str | None = None
    repair_title: str | None = None
    repair_detail: str | None = None
    repair_options: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    package_external_id: str | None = None
    appointment_external_id: str | None = None
    patient_external_id: str | None = None
    service_external_id: str | None = None


class ClinicImportCapabilities(BaseModel):
    catalog_read: bool
    availability_read: bool
    appointments_read: bool = False
    appointments_create: bool = False
    appointments_confirm: bool = False
    appointments_cancel: bool = False
    appointments_reschedule: bool = False


class NormalizedServiceImport(BaseModel):
    external_id: str
    external_id_generated: bool = False
    name: str
    duration_minutes: int | None
    price_minor: int
    currency: Literal["EGP"] = "EGP"
    category: str | None = None


class NormalizedBranchImport(BaseModel):
    external_id: str
    external_id_generated: bool = False
    name: str
    is_placeholder: bool = False
    city: str | None = None
    address: str | None = None
    timezone: str | None = None


class NormalizedDoctorImport(BaseModel):
    external_id: str
    external_id_generated: bool = False
    name: str
    is_placeholder: bool = False
    specialization: str | None = None
    service_external_ids: list[str]
    branch_external_ids: list[str]
    is_active: bool = True


class NormalizedAppointmentImport(BaseModel):
    external_id: str
    external_id_generated: bool = False
    patient_external_id: str
    patient_external_id_generated: bool = False
    patient_name: str
    patient_phone: str | None = None
    service_external_id: str
    branch_external_id: str
    doctor_external_id: str
    branch_assignment_known: bool = True
    doctor_assignment_known: bool = True
    appointment_date: date
    start_at: datetime | None = None
    end_at: datetime | None = None
    time_precision: AppointmentTimePrecision
    status: ImportAppointmentLifecycle = "unknown"
    payment_status: PaymentStatus = "unknown"
    amount_paid_minor: int | None = None
    payment_method: PaymentMethod = "unknown"
    billing_context: BillingContext = "standard"
    package_external_id: str | None = None
    payment_external_reference: str | None = None
    refund_amount_minor: int | None = None
    refund_reason: str | None = None
    refunded_at: datetime | None = None
    source: AppointmentSource = "other"


class NormalizedPaymentImport(BaseModel):
    external_id: str
    patient_external_id: str | None = None
    patient_phone: str | None = None
    appointment_external_id: str | None = None
    package_external_id: str | None = None
    amount_minor: int
    currency: Literal["EGP"] = "EGP"
    payment_method: PaymentMethod = "unknown"
    external_reference: str | None = None
    paid_at: datetime


class NormalizedPaymentAllocationImport(BaseModel):
    payment_external_id: str
    appointment_external_id: str
    amount_minor: int


class NormalizedPackageImport(BaseModel):
    external_id: str
    patient_external_id: str | None = None
    patient_phone: str | None = None
    service_external_id: str
    name: str
    sessions_purchased: int
    sale_price_minor: int = 0
    standalone_session_price_minor_at_purchase: int | None = None
    currency: Literal["EGP"] = "EGP"
    purchased_at: datetime
    expires_at: date | None = None
    status: PackageStatus = "active"


class NormalizedPackageUsageImport(BaseModel):
    external_id: str | None = None
    package_external_id: str
    appointment_external_id: str
    sessions_used: int = 1
    used_at: datetime | None = None


class NormalizedWorkingHourImport(BaseModel):
    owner_external_id: str
    branch_external_id: str | None = None
    weekday: int
    start_time: str
    end_time: str


class ClinicImportPreviewResponse(BaseModel):
    services: list[NormalizedServiceImport]
    branches: list[NormalizedBranchImport]
    doctors: list[NormalizedDoctorImport]
    branch_hours: list[NormalizedWorkingHourImport]
    doctor_hours: list[NormalizedWorkingHourImport]
    appointments: list[NormalizedAppointmentImport]
    payments: list[NormalizedPaymentImport] = Field(default_factory=list)
    payment_allocations: list[NormalizedPaymentAllocationImport] = Field(default_factory=list)
    packages: list[NormalizedPackageImport] = Field(default_factory=list)
    package_usages: list[NormalizedPackageUsageImport] = Field(default_factory=list)
    issues: list[ClinicImportIssue]
    capabilities: ClinicImportCapabilities
    can_apply: bool
    source_summary: dict[str, Any]


class ClinicImportApplyResponse(BaseModel):
    imported_services: int
    imported_branches: int
    imported_doctors: int
    service_links: int
    branch_links: int
    doctor_links: int
    branch_working_hours: int
    doctor_working_hours: int
    imported_patients: int
    imported_appointments: int
    patient_links: int
    appointment_links: int
    imported_payments: int = 0
    imported_payment_allocations: int = 0
    imported_packages: int = 0
    imported_package_usages: int = 0
    capabilities: ClinicImportCapabilities
