from __future__ import annotations

from collections.abc import Callable, Hashable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.integrations.clinic.base import (
    AppointmentReadRequest,
    AppointmentReadResult,
    AppointmentRecord,
    AvailabilityRequest,
    AvailabilityResult,
    AvailabilitySlot,
    ClinicAdapter,
    ClinicCapabilities,
    ClinicCapability,
    PatientReadRequest,
    PatientRecord,
    PaymentAllocationRecord,
    PaymentReadRequest,
    PaymentReadResult,
    PaymentRecord,
)
from app.integrations.clinic.mapped_sync import schema_fingerprint
from app.integrations.clinic.sync_contract import (
    ClinicRawSyncFetchRequest,
    ClinicRawSyncPage,
    ClinicSyncDomain,
    ClinicSyncFetchRequest,
    ClinicSyncPage,
    ExternalAppointmentSyncRecord,
    ExternalPatientSyncRecord,
    ExternalPaymentAllocationSyncRecord,
    ExternalPaymentSyncRecord,
)
from app.schemas.clinic_connector_mapping import (
    ClinicConnectorColumnSchema,
    ClinicConnectorSchemaSnapshot,
    ClinicConnectorTableSchema,
)


class PrototypeExternalConfigurationError(ValueError):
    """Raised when the Phase 2.6 demo source cannot be normalized safely."""


PatientExternalIdResolver = Callable[[str], str | None]


_BASE_READ_CAPABILITIES = frozenset(
    {
        ClinicCapability.CATALOG_READ,
        ClinicCapability.AVAILABILITY_READ,
        ClinicCapability.APPOINTMENTS_READ,
    }
)


def _required_text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None:
        raise PrototypeExternalConfigurationError(f"Missing external field {key!r}.")
    value = str(value).strip()
    if not value:
        raise PrototypeExternalConfigurationError(f"External field {key!r} cannot be empty.")
    return value


def _optional_text(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _int_value(row: dict[str, Any], key: str) -> int:
    raw = _required_text(row, key)
    try:
        value = int(raw)
    except ValueError as exc:
        raise PrototypeExternalConfigurationError(
            f"External field {key!r} must be an integer."
        ) from exc
    if value <= 0:
        raise PrototypeExternalConfigurationError(
            f"External field {key!r} must be greater than zero."
        )
    return value


def _minor_units(row: dict[str, Any], key: str) -> int:
    raw = _required_text(row, key).replace(",", "")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise PrototypeExternalConfigurationError(
            f"External field {key!r} must be a numeric amount."
        ) from exc
    if amount < 0:
        raise PrototypeExternalConfigurationError(
            f"External field {key!r} cannot be negative."
        )
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _parse_datetime(value: str, *, timezone_name: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PrototypeExternalConfigurationError(
            f"Invalid external datetime value: {value!r}."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed


def _list_of_text(row: dict[str, Any], key: str) -> list[str]:
    value = row.get(key, [])
    if isinstance(value, str):
        # This deliberately mirrors a common spreadsheet/export convention.
        value = [item for item in value.split("|")]
    if not isinstance(value, list):
        raise PrototypeExternalConfigurationError(
            f"External field {key!r} must be a list or pipe-delimited string."
        )
    return [str(item).strip() for item in value if str(item).strip()]


class PrototypeExternalClinicAdapter(ClinicAdapter):
    """Read-only proof that Linka can run over a non-Linka clinic schema.

    The source payload intentionally uses vendor/spreadsheet-style field names
    such as ``Treatment Code``, ``Clinic Location``, ``Doctor Key`` and
    ``Booking Ref``. The rest of Linka only receives canonical catalog,
    availability and appointment DTOs from :class:`ClinicAdapter`.

    This adapter is intentionally a Phase 2.6 prototype. It reads a fixture-like
    external payload from integration config so we can prove the boundary
    without pretending that one generic HTTP schema will fit every clinic.
    Production connectors will replace the payload loader, not the canonical
    contract exposed here.
    """

    def __init__(
        self,
        *,
        workspace_timezone: str,
        external_clinic_id: str | None,
        config: dict[str, Any],
        resolve_patient_external_id: PatientExternalIdResolver,
    ) -> None:
        self.workspace_timezone = workspace_timezone or "UTC"
        self.external_clinic_id = external_clinic_id
        self.config = dict(config or {})
        self.resolve_patient_external_id = resolve_patient_external_id

        raw = self.config.get("prototype_dataset")
        if not isinstance(raw, dict):
            raise PrototypeExternalConfigurationError(
                "prototype_external requires config.prototype_dataset."
            )
        self.raw = raw

        timezone_name = str(raw.get("Clinic Timezone") or self.workspace_timezone).strip()
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise PrototypeExternalConfigurationError(
                f"Unknown clinic timezone {timezone_name!r}."
            ) from exc
        self.timezone = timezone_name

    @property
    def cache_namespace(self) -> str:
        clinic_id = self.external_clinic_id or "unscoped"
        return f"prototype_external:{clinic_id}"

    @property
    def capabilities(self) -> ClinicCapabilities:
        supported = set(_BASE_READ_CAPABILITIES)
        if "Patients Sheet" in self.raw:
            supported.add(ClinicCapability.PATIENTS_READ)
        if "Payments Sheet" in self.raw:
            supported.add(ClinicCapability.PAYMENTS_READ)
        return ClinicCapabilities(frozenset(supported))

    def catalog_revision(self) -> Hashable | None:
        revision = self.raw.get("Source Revision")
        if revision is None:
            return None
        return str(revision)

    def _rows(self, key: str) -> list[dict[str, Any]]:
        value = self.raw.get(key, [])
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise PrototypeExternalConfigurationError(
                f"External dataset section {key!r} must be a list of objects."
            )
        return list(value)

    def _index_rows(self, section: str, id_field: str) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for row in self._rows(section):
            external_id = _required_text(row, id_field)
            if external_id in index:
                raise PrototypeExternalConfigurationError(
                    f"Duplicate external id {external_id!r} in {section!r}."
                )
            index[external_id] = row
        return index

    def _service_index(self) -> dict[str, dict[str, Any]]:
        return self._index_rows("Treatments Sheet", "Treatment Code")

    def _branch_index(self) -> dict[str, dict[str, Any]]:
        return self._index_rows("Locations Sheet", "Branch Ref")

    def _doctor_index(self) -> dict[str, dict[str, Any]]:
        return self._index_rows("Doctors Sheet", "Doctor Key")

    def _canonical_status(self, raw_status: str) -> str:
        normalized = raw_status.strip().lower()
        canonical = {
            "pending",
            "confirmed",
            "checked_in",
            "in_progress",
            "completed",
            "cancelled",
            "no_show",
            "rescheduled",
        }
        if normalized in canonical:
            return normalized

        configured = self.raw.get("Status Map", {})
        if not isinstance(configured, dict):
            raise PrototypeExternalConfigurationError(
                "External field 'Status Map' must be an object when provided."
            )
        mapped = configured.get(raw_status)
        if mapped is None:
            mapped = configured.get(normalized)
        if mapped is None:
            raise PrototypeExternalConfigurationError(
                f"External appointment status {raw_status!r} has no canonical mapping."
            )
        mapped = str(mapped).strip().lower()
        if mapped not in canonical:
            raise PrototypeExternalConfigurationError(
                f"External status mapping {mapped!r} is not a canonical appointment status."
            )
        return mapped

    def _external_patient_id(self, canonical_patient_id: str) -> str:
        external_patient_id = self.resolve_patient_external_id(canonical_patient_id)
        if not external_patient_id:
            raise ValueError(
                "This Linka patient is not linked to an external clinic patient id."
            )
        return external_patient_id

    @staticmethod
    def _canonical_patient_status(raw_status: str) -> str:
        normalized = raw_status.strip().lower()
        mapping = {
            "active": "active",
            "inactive": "inactive",
            "blocked": "blocked",
            "enabled": "active",
            "disabled": "inactive",
        }
        if normalized not in mapping:
            raise PrototypeExternalConfigurationError(
                f"External patient status {raw_status!r} has no canonical mapping."
            )
        return mapping[normalized]

    @staticmethod
    def _canonical_patient_source(raw_source: str | None) -> str:
        normalized = (raw_source or "other").strip().lower().replace("-", "_").replace(" ", "_")
        mapping = {
            "whatsapp": "whatsapp",
            "instagram": "instagram",
            "facebook": "facebook",
            "website": "website",
            "web": "website",
            "referral": "referral",
            "walk_in": "walk_in",
            "campaign": "campaign",
            "phone": "phone",
            "other": "other",
        }
        return mapping.get(normalized, "other")

    @staticmethod
    def _canonical_payment_type(raw_type: str) -> str:
        normalized = raw_type.strip().lower()
        mapping = {
            "payment": "payment",
            "charge": "payment",
            "receipt": "payment",
            "refund": "refund",
            "reversal": "refund",
        }
        if normalized not in mapping:
            raise PrototypeExternalConfigurationError(
                f"External payment type {raw_type!r} has no canonical mapping."
            )
        return mapping[normalized]

    @staticmethod
    def _canonical_payment_method(raw_method: str | None) -> str:
        normalized = (raw_method or "unknown").strip().lower().replace(" ", "_")
        mapping = {
            "cash": "cash",
            "card": "card",
            "credit_card": "card",
            "debit_card": "card",
            "bank": "bank_transfer",
            "bank_transfer": "bank_transfer",
            "wallet": "wallet",
            "online": "online",
            "other": "other",
            "unknown": "unknown",
        }
        return mapping.get(normalized, "other")

    @property
    def sync_domains(self) -> frozenset[ClinicSyncDomain]:
        supported: set[ClinicSyncDomain] = set()
        if "Patients Sheet" in self.raw:
            supported.add(ClinicSyncDomain.PATIENTS)
        if "Bookings Sheet" in self.raw:
            supported.add(ClinicSyncDomain.APPOINTMENTS)
        if "Payments Sheet" in self.raw:
            supported.add(ClinicSyncDomain.PAYMENTS)
        return frozenset(supported)

    def _sync_revision(self) -> str | None:
        revision = self.raw.get("Source Revision")
        if revision is None:
            return None
        value = str(revision).strip()
        return value or None

    def _decode_sync_cursor(self, domain: ClinicSyncDomain, cursor: str | None) -> int:
        if cursor is None:
            return 0
        parts = cursor.split(":")
        if len(parts) != 3 or parts[0] != "v1" or parts[1] != domain.value:
            raise PrototypeExternalConfigurationError("External sync cursor is invalid for this domain.")
        try:
            offset = int(parts[2])
        except ValueError as exc:
            raise PrototypeExternalConfigurationError("External sync cursor offset is invalid.") from exc
        if offset < 0:
            raise PrototypeExternalConfigurationError("External sync cursor offset cannot be negative.")
        return offset

    def _encode_sync_cursor(self, domain: ClinicSyncDomain, offset: int) -> str:
        return f"v1:{domain.value}:{offset}"

    def _patient_sync_records(self) -> list[ExternalPatientSyncRecord]:
        records: list[ExternalPatientSyncRecord] = []
        for row in self._rows("Patients Sheet"):
            updated_text = _optional_text(row, "Updated ISO")
            records.append(
                ExternalPatientSyncRecord(
                    external_id=_required_text(row, "Client Ref"),
                    first_name=_required_text(row, "Given Name"),
                    last_name=_optional_text(row, "Family Name"),
                    phone=_optional_text(row, "Mobile"),
                    status=self._canonical_patient_status(_optional_text(row, "State") or "active"),
                    preferred_language=_optional_text(row, "Language") or "ar",
                    source=self._canonical_patient_source(_optional_text(row, "Acquisition")),
                    source_updated_at=(
                        _parse_datetime(updated_text, timezone_name=self.timezone)
                        if updated_text
                        else None
                    ),
                )
            )
        records.sort(key=lambda item: item.external_id)
        return records

    def _appointment_sync_records(self) -> list[ExternalAppointmentSyncRecord]:
        services = self._service_index()
        records: list[ExternalAppointmentSyncRecord] = []
        terminal_statuses = {"completed", "cancelled", "no_show"}
        for row in self._rows("Bookings Sheet"):
            service_id = _required_text(row, "Treatment Code")
            service = services.get(service_id)
            if service is None:
                raise PrototypeExternalConfigurationError(
                    "External booking references an unknown service."
                )
            status = self._canonical_status(_required_text(row, "Status"))
            start_at = _parse_datetime(
                _required_text(row, "Start ISO"), timezone_name=self.timezone
            )
            end_text = _optional_text(row, "End ISO")
            end_at = (
                _parse_datetime(end_text, timezone_name=self.timezone)
                if end_text
                else start_at + timedelta(minutes=_int_value(service, "Minutes"))
            )
            status_text = _optional_text(row, "Status ISO")
            if status in terminal_statuses and not status_text:
                status_text = _optional_text(row, "Updated ISO")
            price_text = _optional_text(row, "Price")
            updated_text = _optional_text(row, "Updated ISO")
            records.append(
                ExternalAppointmentSyncRecord(
                    external_id=_required_text(row, "Booking Ref"),
                    patient_external_id=_required_text(row, "Client Ref"),
                    branch_external_id=_required_text(row, "Branch Ref"),
                    service_external_id=service_id,
                    doctor_external_id=_required_text(row, "Doctor Key"),
                    status=status,
                    start_at=start_at,
                    end_at=end_at,
                    status_at=(
                        _parse_datetime(status_text, timezone_name=self.timezone)
                        if status_text
                        else None
                    ),
                    price_minor=(
                        _minor_units(row, "Price") if price_text is not None else None
                    ),
                    currency=(
                        (_optional_text(row, "Currency") or "EGP").upper()
                        if price_text is not None
                        else None
                    ),
                    source_updated_at=(
                        _parse_datetime(updated_text, timezone_name=self.timezone)
                        if updated_text
                        else None
                    ),
                )
            )
        records.sort(key=lambda item: item.external_id)
        return records

    def _payment_sync_records(self) -> list[ExternalPaymentSyncRecord]:
        allocation_rows = self._rows("Payment Allocations Sheet") if "Payment Allocations Sheet" in self.raw else []
        allocation_totals: dict[str, dict[str, int]] = {}
        for row in allocation_rows:
            payment_id = _required_text(row, "Payment Ref")
            booking_id = _required_text(row, "Booking Ref")
            amount_minor = _minor_units(row, "Amount")
            if amount_minor <= 0:
                raise PrototypeExternalConfigurationError(
                    "External payment allocation amount must be greater than zero."
                )
            per_payment = allocation_totals.setdefault(payment_id, {})
            per_payment[booking_id] = per_payment.get(booking_id, 0) + amount_minor

        allocations_by_payment = {
            payment_id: [
                ExternalPaymentAllocationSyncRecord(
                    appointment_external_id=booking_id,
                    amount_minor=amount_minor,
                )
                for booking_id, amount_minor in sorted(per_booking.items())
            ]
            for payment_id, per_booking in allocation_totals.items()
        }

        records: list[ExternalPaymentSyncRecord] = []
        for row in self._rows("Payments Sheet"):
            payment_id = _required_text(row, "Payment Ref")
            transaction_type = self._canonical_payment_type(_required_text(row, "Transaction Kind"))
            reference_id = _optional_text(row, "Original Payment Ref")
            if transaction_type == "refund" and not reference_id:
                raise PrototypeExternalConfigurationError(
                    "External refund row must reference its original payment."
                )
            if transaction_type == "payment":
                reference_id = None
            amount_minor = _minor_units(row, "Amount")
            if amount_minor <= 0:
                raise PrototypeExternalConfigurationError(
                    "External payment amount must be greater than zero."
                )
            explicit_allocations = allocations_by_payment.get(payment_id)
            booking_ref = _optional_text(row, "Booking Ref")
            if explicit_allocations is not None:
                allocations = tuple(
                    sorted(explicit_allocations, key=lambda item: item.appointment_external_id)
                )
            elif booking_ref:
                allocations = (
                    ExternalPaymentAllocationSyncRecord(
                        appointment_external_id=booking_ref,
                        amount_minor=amount_minor,
                    ),
                )
            else:
                allocations = ()
            records.append(
                ExternalPaymentSyncRecord(
                    external_id=payment_id,
                    patient_external_id=_required_text(row, "Client Ref"),
                    transaction_type=transaction_type,
                    amount_minor=amount_minor,
                    currency=(_optional_text(row, "Currency") or "EGP").upper(),
                    payment_method=self._canonical_payment_method(_optional_text(row, "Method")),
                    created_at=_parse_datetime(
                        _required_text(row, "Created ISO"), timezone_name=self.timezone
                    ),
                    external_reference=_optional_text(row, "Provider Ref"),
                    reference_payment_external_id=reference_id,
                    allocations=allocations,
                )
            )
        records.sort(key=lambda item: item.external_id)
        return records

    def fetch_sync_page(self, request: ClinicSyncFetchRequest) -> ClinicSyncPage:
        if request.domain not in self.sync_domains:
            raise PrototypeExternalConfigurationError(
                f"External connector does not expose the {request.domain.value} sync domain."
            )
        limit = max(1, min(int(request.limit), 500))
        offset = self._decode_sync_cursor(request.domain, request.cursor)
        if request.domain == ClinicSyncDomain.PATIENTS:
            records = self._patient_sync_records()
        elif request.domain == ClinicSyncDomain.APPOINTMENTS:
            records = self._appointment_sync_records()
        else:
            records = self._payment_sync_records()
        page_records = records[offset : offset + limit]
        next_offset = offset + len(page_records)
        has_more = next_offset < len(records)
        next_cursor = self._encode_sync_cursor(request.domain, next_offset) if has_more else None
        return ClinicSyncPage(
            domain=request.domain,
            records=tuple(page_records),
            cursor=request.cursor,
            next_cursor=next_cursor,
            source_revision=self._sync_revision(),
            has_more=has_more,
        )

    @property
    def raw_sync_domains(self) -> frozenset[ClinicSyncDomain]:
        return self.sync_domains

    @staticmethod
    def _schema_kind(values: list[Any]) -> str:
        present = [value for value in values if value is not None and str(value).strip()]
        if not present:
            return "unknown"
        if all(isinstance(value, bool) for value in present):
            return "boolean"
        if all(isinstance(value, int) and not isinstance(value, bool) for value in present):
            return "integer"
        if all(isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) for value in present):
            return "decimal"
        texts = [str(value).strip() for value in present]
        parsed_datetimes = 0
        for text in texts:
            candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
            try:
                datetime.fromisoformat(candidate)
            except ValueError:
                continue
            parsed_datetimes += 1
        if parsed_datetimes == len(texts):
            return "datetime"
        return "text"

    def discover_sync_schema(self) -> ClinicConnectorSchemaSnapshot:
        tables: list[ClinicConnectorTableSchema] = []
        relationships = {
            ("Bookings Sheet", "Client Ref"): ("Patients Sheet", "Client Ref"),
            ("Payments Sheet", "Client Ref"): ("Patients Sheet", "Client Ref"),
            ("Payments Sheet", "Booking Ref"): ("Bookings Sheet", "Booking Ref"),
            ("Payment Allocations Sheet", "Payment Ref"): ("Payments Sheet", "Payment Ref"),
            ("Payment Allocations Sheet", "Booking Ref"): ("Bookings Sheet", "Booking Ref"),
        }
        primary_keys = {
            ("Patients Sheet", "Client Ref"),
            ("Bookings Sheet", "Booking Ref"),
            ("Payments Sheet", "Payment Ref"),
            ("Treatments Sheet", "Treatment Code"),
            ("Locations Sheet", "Branch Ref"),
            ("Doctors Sheet", "Doctor Key"),
        }
        for table_name, raw_rows in sorted(self.raw.items()):
            if not table_name.endswith("Sheet") or not isinstance(raw_rows, list):
                continue
            rows = [dict(row) for row in raw_rows if isinstance(row, dict)]
            columns = sorted({str(column) for row in rows for column in row})
            if not columns:
                continue
            column_models = []
            for column in columns:
                ref = relationships.get((table_name, column))
                values = [row.get(column) for row in rows[:500]]
                present = [value for value in values if value is not None and str(value).strip()]
                distinct_count = len({str(value).strip() for value in present})
                unique_ratio = (distinct_count / len(present)) if present else 0.0
                phone_like = sum(
                    1
                    for value in present
                    if len([ch for ch in str(value) if ch.isdigit()]) >= 8
                    and all(ch.isdigit() or ch in "+- ()" for ch in str(value))
                )
                identifier_like = sum(
                    1
                    for value in present
                    if len(str(value).strip()) <= 80 and " " not in str(value).strip()
                )
                is_pk = (table_name, column) in primary_keys
                column_models.append(
                    ClinicConnectorColumnSchema(
                        name=column,
                        kind=self._schema_kind(values),
                        nullable=not is_pk,
                        primary_key=is_pk,
                        references_table=ref[0] if ref else None,
                        references_column=ref[1] if ref else None,
                        distinct_count=distinct_count,
                        unique_ratio=unique_ratio,
                        phone_like_ratio=(phone_like / len(present)) if present else 0.0,
                        identifier_like_ratio=(identifier_like / len(present)) if present else 0.0,
                    )
                )
            tables.append(
                ClinicConnectorTableSchema(
                    name=table_name,
                    columns=column_models,
                    estimated_row_count=len(rows),
                )
            )
        if not tables:
            raise PrototypeExternalConfigurationError("External connector schema contains no readable tables.")
        return ClinicConnectorSchemaSnapshot(revision=self._sync_revision(), tables=tables)

    def fetch_raw_sync_page(self, request: ClinicRawSyncFetchRequest) -> ClinicRawSyncPage:
        if request.domain not in self.raw_sync_domains:
            raise PrototypeExternalConfigurationError(
                f"External connector does not expose the {request.domain.value} raw sync domain."
            )
        main_sheet = {
            ClinicSyncDomain.PATIENTS: "Patients Sheet",
            ClinicSyncDomain.APPOINTMENTS: "Bookings Sheet",
            ClinicSyncDomain.PAYMENTS: "Payments Sheet",
        }[request.domain]
        limit = max(1, min(int(request.limit), 500))
        offset = self._decode_sync_cursor(request.domain, request.cursor)
        main_rows = [dict(row) for row in self._rows(main_sheet)]
        page_rows = main_rows[offset : offset + limit]
        next_offset = offset + len(page_rows)
        has_more = next_offset < len(main_rows)
        tables: dict[str, tuple[dict[str, Any], ...]] = {}
        for table_name, raw_rows in self.raw.items():
            if not table_name.endswith("Sheet") or not isinstance(raw_rows, list):
                continue
            chosen = page_rows if table_name == main_sheet else [dict(row) for row in raw_rows if isinstance(row, dict)]
            tables[table_name] = tuple(chosen)
        snapshot = self.discover_sync_schema()
        return ClinicRawSyncPage(
            domain=request.domain,
            tables=tables,
            schema_fingerprint=schema_fingerprint(snapshot),
            cursor=request.cursor,
            next_cursor=(self._encode_sync_cursor(request.domain, next_offset) if has_more else None),
            source_revision=self._sync_revision(),
            has_more=has_more,
        )

    def build_catalog(self) -> dict[str, Any]:
        self.require_capability(ClinicCapability.CATALOG_READ)
        services = self._service_index()
        branches = self._branch_index()
        doctors = self._doctor_index()

        canonical_services = []
        for service_id, row in services.items():
            currency = (_optional_text(row, "Currency") or "EGP").upper()
            price_minor = _minor_units(row, "Price")
            canonical_services.append(
                {
                    "id": service_id,
                    "name": _required_text(row, "Treatment"),
                    "category": _optional_text(row, "Category"),
                    "description": _optional_text(row, "Description"),
                    "duration_minutes": _int_value(row, "Minutes"),
                    "price_minor": price_minor,
                    "currency": currency,
                    "price": f"{Decimal(price_minor) / Decimal(100):,.2f} {currency}",
                    "requires_medical_review": bool(row.get("Medical Review Required", False)),
                }
            )

        canonical_branches = []
        for branch_id, row in branches.items():
            address_parts = [
                _optional_text(row, "Address"),
                _optional_text(row, "City"),
            ]
            canonical_branches.append(
                {
                    "id": branch_id,
                    "name": _required_text(row, "Clinic Location"),
                    "code": _optional_text(row, "Branch Code") or branch_id,
                    "city": _optional_text(row, "City"),
                    "address": "، ".join(part for part in address_parts if part) or None,
                    "working_hours": [],
                }
            )

        canonical_doctors = []
        for doctor_id, row in doctors.items():
            service_ids = [
                item for item in _list_of_text(row, "Treatments") if item in services
            ]
            branch_ids = [
                item for item in _list_of_text(row, "Locations") if item in branches
            ]
            # A doctor without both assignments is not bookable, matching the
            # canonical contract enforced by the native adapter.
            if not service_ids or not branch_ids:
                continue
            canonical_doctors.append(
                {
                    "id": doctor_id,
                    "name": _required_text(row, "Doctor Display"),
                    "specialization": _optional_text(row, "Specialty"),
                    "service_ids": sorted(service_ids),
                    "branch_ids": sorted(branch_ids),
                    "working_hours": [],
                }
            )

        canonical_services.sort(key=lambda item: (item.get("category") or "", item["name"]))
        canonical_branches.sort(key=lambda item: item["name"])
        canonical_doctors.sort(key=lambda item: item["name"])
        return {
            "services": canonical_services,
            "branches": canonical_branches,
            "doctors": canonical_doctors,
        }

    def get_availability(self, request: AvailabilityRequest) -> AvailabilityResult:
        self.require_capability(ClinicCapability.AVAILABILITY_READ)
        services = self._service_index()
        branches = self._branch_index()
        doctors = self._doctor_index()

        service = services.get(request.service_id)
        if service is None:
            raise ValueError("Service not found in the external clinic source.")
        branch = branches.get(request.branch_id)
        if branch is None:
            raise ValueError("Branch not found in the external clinic source.")
        if request.doctor_id is not None and request.doctor_id not in doctors:
            raise ValueError("Doctor not found in the external clinic source.")

        duration = _int_value(service, "Minutes")
        price_minor = _minor_units(service, "Price")
        currency = (_optional_text(service, "Currency") or "EGP").upper()
        branch_name = _required_text(branch, "Clinic Location")
        service_name = _required_text(service, "Treatment")
        clinic_tz = ZoneInfo(self.timezone)

        slots: list[AvailabilitySlot] = []
        for row in self._rows("Free Slots Feed"):
            if _required_text(row, "Branch Ref") != request.branch_id:
                continue
            if _required_text(row, "Treatment Code") != request.service_id:
                continue
            doctor_id = _required_text(row, "Doctor Key")
            if request.doctor_id is not None and doctor_id != request.doctor_id:
                continue
            doctor = doctors.get(doctor_id)
            if doctor is None:
                continue
            start_at = _parse_datetime(
                _required_text(row, "Start ISO"),
                timezone_name=self.timezone,
            )
            if start_at.astimezone(clinic_tz).date() != request.booking_date:
                continue
            if request.now is not None and start_at <= request.now:
                continue
            end_at = start_at + timedelta(minutes=duration)
            slots.append(
                AvailabilitySlot(
                    branch_id=request.branch_id,
                    branch_name=branch_name,
                    doctor_id=doctor_id,
                    doctor_name=_required_text(doctor, "Doctor Display"),
                    service_id=request.service_id,
                    service_name=service_name,
                    start_at=start_at,
                    end_at=end_at,
                    duration_minutes=duration,
                    price_minor=price_minor,
                    currency=currency,
                )
            )

        slots.sort(key=lambda slot: (slot.start_at, slot.doctor_name or "", slot.doctor_id))
        return AvailabilityResult(
            timezone=self.timezone,
            branch_id=request.branch_id,
            branch_name=branch_name,
            service_id=request.service_id,
            service_name=service_name,
            service_duration_minutes=duration,
            service_price_minor=price_minor,
            service_currency=currency,
            slots=tuple(slots),
        )

    def get_patient(self, request: PatientReadRequest) -> PatientRecord:
        self.require_capability(ClinicCapability.PATIENTS_READ)
        external_patient_id = self._external_patient_id(request.patient_id)
        row = self._index_rows("Patients Sheet", "Client Ref").get(external_patient_id)
        if row is None:
            raise ValueError("Patient not found in the external clinic source.")
        updated_text = _optional_text(row, "Updated ISO")
        return PatientRecord(
            patient_id=request.patient_id,
            first_name=_required_text(row, "Given Name"),
            last_name=_optional_text(row, "Family Name"),
            phone=_optional_text(row, "Mobile"),
            status=self._canonical_patient_status(_optional_text(row, "State") or "active"),
            preferred_language=_optional_text(row, "Language") or "ar",
            source=self._canonical_patient_source(_optional_text(row, "Acquisition")),
            updated_at=(
                _parse_datetime(updated_text, timezone_name=self.timezone)
                if updated_text
                else None
            ),
        )

    def get_patient_payments(self, request: PaymentReadRequest) -> PaymentReadResult:
        self.require_capability(ClinicCapability.PAYMENTS_READ)
        external_patient_id = self._external_patient_id(request.patient_id)
        limit = max(1, min(int(request.limit), 200))
        transactions: list[PaymentRecord] = []
        for row in self._rows("Payments Sheet"):
            if _required_text(row, "Client Ref") != external_patient_id:
                continue
            appointment_id = _optional_text(row, "Booking Ref")
            if request.appointment_id and appointment_id != request.appointment_id:
                continue
            transaction_type = self._canonical_payment_type(
                _required_text(row, "Transaction Kind")
            )
            reference_id = _optional_text(row, "Original Payment Ref")
            if transaction_type == "refund" and not reference_id:
                raise PrototypeExternalConfigurationError(
                    "External refund row must reference its original payment."
                )
            if transaction_type == "payment":
                reference_id = None
            amount_minor = _minor_units(row, "Amount")
            if amount_minor <= 0:
                raise PrototypeExternalConfigurationError(
                    "External payment amount must be greater than zero."
                )
            transactions.append(
                PaymentRecord(
                    transaction_id=_required_text(row, "Payment Ref"),
                    patient_id=request.patient_id,
                    appointment_id=appointment_id,
                    transaction_type=transaction_type,
                    amount_minor=amount_minor,
                    currency=(_optional_text(row, "Currency") or "EGP").upper(),
                    payment_method=self._canonical_payment_method(
                        _optional_text(row, "Method")
                    ),
                    source="integration",
                    created_at=_parse_datetime(
                        _required_text(row, "Created ISO"),
                        timezone_name=self.timezone,
                    ),
                    external_reference=_optional_text(row, "Provider Ref"),
                    reference_transaction_id=reference_id,
                    allocations=(
                        (PaymentAllocationRecord(appointment_id=appointment_id, amount_minor=amount_minor),)
                        if appointment_id
                        else ()
                    ),
                )
            )
        transactions.sort(key=lambda item: (item.created_at, item.transaction_id), reverse=True)
        return PaymentReadResult(transactions=tuple(transactions[:limit]))

    def get_patient_appointments(
        self, request: AppointmentReadRequest
    ) -> AppointmentReadResult:
        self.require_capability(ClinicCapability.APPOINTMENTS_READ)
        external_patient_id = self._external_patient_id(request.patient_id)

        services = self._service_index()
        branches = self._branch_index()
        doctors = self._doctor_index()
        now = request.now or datetime.now(UTC)

        appointments: list[AppointmentRecord] = []
        for row in self._rows("Bookings Sheet"):
            if _required_text(row, "Client Ref") != external_patient_id:
                continue
            service_id = _required_text(row, "Treatment Code")
            branch_id = _required_text(row, "Branch Ref")
            doctor_id = _required_text(row, "Doctor Key")
            service = services.get(service_id)
            branch = branches.get(branch_id)
            doctor = doctors.get(doctor_id)
            if service is None or branch is None or doctor is None:
                raise PrototypeExternalConfigurationError(
                    "External booking references an unknown service, branch, or doctor."
                )
            start_at = _parse_datetime(
                _required_text(row, "Start ISO"),
                timezone_name=self.timezone,
            )
            duration = _int_value(service, "Minutes")
            end_at = start_at + timedelta(minutes=duration)
            if not request.include_past and end_at <= now:
                continue
            appointments.append(
                AppointmentRecord(
                    appointment_id=_required_text(row, "Booking Ref"),
                    patient_id=request.patient_id,
                    status=self._canonical_status(_required_text(row, "Status")),
                    service_id=service_id,
                    service_name=_required_text(service, "Treatment"),
                    branch_id=branch_id,
                    branch_name=_required_text(branch, "Clinic Location"),
                    doctor_id=doctor_id,
                    doctor_name=_required_text(doctor, "Doctor Display"),
                    start_at=start_at,
                    end_at=end_at,
                    timezone=self.timezone,
                    price_minor=_minor_units(service, "Price"),
                    currency=(_optional_text(service, "Currency") or "EGP").upper(),
                )
            )

        appointments.sort(key=lambda item: item.start_at)
        return AppointmentReadResult(appointments=tuple(appointments[: request.limit]))
