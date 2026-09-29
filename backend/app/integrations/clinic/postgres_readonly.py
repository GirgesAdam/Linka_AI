from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.integrations.clinic.base import (
    AppointmentMutationResult,
    AppointmentReadRequest,
    AppointmentReadResult,
    AvailabilityRequest,
    AvailabilityResult,
    CancelAppointmentRequest,
    ClinicAdapter,
    ClinicCapabilities,
    ClinicCapabilityNotSupported,
    ConfirmAppointmentRequest,
    CreateAppointmentRequest,
    PatientReadRequest,
    PatientRecord,
    PaymentReadRequest,
    PaymentReadResult,
    RescheduleAppointmentRequest,
)
from app.integrations.clinic.mapped_sync import (
    schema_fingerprint,
    sync_mapping_extraction_plan,
    sync_mapping_required_raw_columns,
)
from app.integrations.clinic.structural_transform import apply_structural_transforms
from app.integrations.clinic.sync_contract import (
    ClinicRawSyncFetchRequest,
    ClinicRawSyncPage,
    ClinicReferenceCandidate,
    ClinicSyncDomain,
)
from app.integrations.secrets import SecretResolutionError, resolve_secret_ref
from app.schemas.clinic_connector_mapping import (
    ClinicConnectorColumnSchema,
    ClinicConnectorSchemaSnapshot,
    ClinicConnectorTableSchema,
    ClinicSyncMapping,
)


class PostgresReadonlyConnectorError(RuntimeError):
    """Raised when the generic PostgreSQL connector cannot operate safely."""


class PostgresReadonlyConnectorConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemas: list[str] = Field(default_factory=lambda: ["public"], min_length=1, max_length=12)
    include_tables: list[str] | None = Field(default=None, max_length=200)
    connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    statement_timeout_ms: int = Field(default=10_000, ge=1_000, le=60_000)
    lookup_table_row_limit: int = Field(default=5_000, ge=100, le=50_000)
    max_page_size: int = Field(default=500, ge=10, le=2_000)

    @field_validator("schemas")
    @classmethod
    def validate_schemas(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            name = str(item).strip()
            if not name or len(name) > 63 or "\x00" in name:
                raise ValueError("PostgreSQL schema names must be non-empty identifiers.")
            if name == "information_schema" or name.startswith("pg_"):
                raise ValueError("System PostgreSQL schemas cannot be exposed as clinic data.")
            normalized.append(name)
        if len(normalized) != len(set(normalized)):
            raise ValueError("PostgreSQL connector schemas must be unique.")
        return normalized

    @field_validator("include_tables")
    @classmethod
    def validate_include_tables(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [str(item).strip() for item in value if str(item).strip()]
        if not normalized:
            raise ValueError("include_tables cannot be empty when provided.")
        if len(normalized) != len(set(normalized)):
            raise ValueError("include_tables must not contain duplicates.")
        return normalized


@dataclass(frozen=True)
class _ColumnMeta:
    name: str
    raw_type: str
    kind: str
    nullable: bool
    primary_key: bool = False
    pk_position: int | None = None
    references_table: str | None = None
    references_column: str | None = None


@dataclass(frozen=True)
class _TableMeta:
    display_name: str
    schema_name: str
    table_name: str
    columns: tuple[_ColumnMeta, ...]
    estimated_row_count: int | None = None

    @property
    def primary_key(self) -> tuple[_ColumnMeta, ...]:
        return tuple(
            sorted(
                (column for column in self.columns if column.primary_key),
                key=lambda item: item.pk_position or 0,
            )
        )

    @property
    def column_names(self) -> frozenset[str]:
        return frozenset(column.name for column in self.columns)


_SUPPORTED_CURSOR_TYPES = {
    "int2",
    "int4",
    "int8",
    "smallint",
    "integer",
    "bigint",
    "numeric",
    "decimal",
    "float4",
    "float8",
    "real",
    "double precision",
    "uuid",
    "text",
    "varchar",
    "bpchar",
    "char",
    "date",
    "timestamp",
    "timestamptz",
    "timestamp without time zone",
    "timestamp with time zone",
}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _qualified_table(table: _TableMeta) -> str:
    return f"{_quote_identifier(table.schema_name)}.{_quote_identifier(table.table_name)}"


def _kind_for_postgres(*, data_type: str, udt_name: str) -> str:
    raw = (udt_name or data_type or "").lower()
    logical = (data_type or "").lower()
    if raw in {"int2", "int4", "int8"} or logical in {"smallint", "integer", "bigint"}:
        return "integer"
    if raw in {"numeric", "float4", "float8", "money"} or logical in {
        "numeric",
        "decimal",
        "real",
        "double precision",
    }:
        return "decimal"
    if raw == "bool" or logical == "boolean":
        return "boolean"
    if logical == "date":
        return "date"
    if "timestamp" in logical or raw in {"timestamp", "timestamptz"}:
        return "datetime"
    if "time" in logical or raw in {"time", "timetz"}:
        return "time"
    if raw in {"json", "jsonb"} or logical in {"json", "jsonb"}:
        return "json"
    if raw in {"text", "varchar", "bpchar", "char", "uuid", "citext"} or logical in {
        "text",
        "character varying",
        "character",
        "uuid",
    }:
        return "text"
    return "unknown"


def _normalize_dsn(value: str) -> str:
    raw = value.strip()
    if raw.startswith("postgresql+psycopg://"):
        raw = "postgresql://" + raw[len("postgresql+psycopg://") :]
    parsed = urlsplit(raw)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise PostgresReadonlyConnectorError(
            "PostgreSQL connector secret must resolve to a PostgreSQL DSN."
        )
    if parsed.fragment:
        raise PostgresReadonlyConnectorError("PostgreSQL connector DSN cannot contain a fragment.")
    return urlunsplit(parsed)


def _cursor_json_value(value: Any) -> str:
    if value is None:
        raise PostgresReadonlyConnectorError("PostgreSQL primary-key cursor cannot contain null values.")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _stable_row_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return str(value)


def _sort_rows(rows: list[dict[str, Any]], columns: set[str]) -> None:
    ordered = sorted(columns)
    rows.sort(key=lambda row: tuple(_stable_row_value(row.get(column)) for column in ordered))


def _decode_typed_cursor_value(raw_type: str, value: str) -> Any:
    normalized = raw_type.lower()
    if normalized in {"int2", "int4", "int8", "smallint", "integer", "bigint"}:
        return int(value)
    if normalized in {"numeric", "decimal"}:
        return Decimal(value)
    if normalized in {"float4", "float8", "real", "double precision"}:
        return float(value)
    if normalized == "uuid":
        return UUID(value)
    if normalized == "date":
        return date.fromisoformat(value)
    if normalized in {"timestamp", "timestamptz", "timestamp without time zone", "timestamp with time zone"}:
        return datetime.fromisoformat(value)
    return value


class PostgresReadonlyClinicAdapter(ClinicAdapter):
    """Generic read-only PostgreSQL connector for clinic-owned source databases.

    It never accepts arbitrary SQL. Table/column identifiers come from PostgreSQL
    catalog discovery and an approved typed mapping. Values are always bound
    parameters, and every source transaction is explicitly read-only.
    """

    def __init__(
        self,
        *,
        secret_ref: str | None,
        config: dict[str, Any] | None,
        native_delegate: ClinicAdapter | None = None,
    ) -> None:
        self.native_delegate = native_delegate
        self._raw_config = dict(config or {})
        self._approved_mapping_raw = self._raw_config.get("approved_sync_mapping")
        try:
            self.config = PostgresReadonlyConnectorConfig.model_validate(self._raw_config)
        except (TypeError, ValueError) as exc:
            raise PostgresReadonlyConnectorError("PostgreSQL connector config is invalid.") from exc
        try:
            self._dsn = _normalize_dsn(resolve_secret_ref(secret_ref))
        except (SecretResolutionError, PostgresReadonlyConnectorError) as exc:
            raise PostgresReadonlyConnectorError(str(exc)) from exc

    @property
    def capabilities(self) -> ClinicCapabilities:
        # Hybrid mode may deliberately keep booking/catalog ownership in Linka while
        # patients/payments (or other selected domains) sync from PostgreSQL.
        if self.native_delegate is not None:
            return self.native_delegate.capabilities
        return ClinicCapabilities(frozenset())

    @property
    def raw_sync_domains(self) -> frozenset[ClinicSyncDomain]:
        return frozenset(
            {ClinicSyncDomain.PATIENTS, ClinicSyncDomain.APPOINTMENTS, ClinicSyncDomain.PAYMENTS}
        )

    def _native(self) -> ClinicAdapter:
        if self.native_delegate is None:
            raise ClinicCapabilityNotSupported(
                "Generic PostgreSQL connector is sync-only in external_api mode."
            )
        return self.native_delegate

    def build_catalog(self) -> dict[str, Any]:
        return self._native().build_catalog()

    def get_availability(self, request: AvailabilityRequest) -> AvailabilityResult:
        return self._native().get_availability(request)

    def get_patient_appointments(self, request: AppointmentReadRequest) -> AppointmentReadResult:
        return self._native().get_patient_appointments(request)

    def get_patient(self, request: PatientReadRequest) -> PatientRecord:
        return self._native().get_patient(request)

    def get_patient_payments(self, request: PaymentReadRequest) -> PaymentReadResult:
        return self._native().get_patient_payments(request)

    def create_appointment(self, request: CreateAppointmentRequest) -> AppointmentMutationResult:
        return self._native().create_appointment(request)

    def confirm_appointment(self, request: ConfirmAppointmentRequest) -> AppointmentMutationResult:
        return self._native().confirm_appointment(request)

    def cancel_appointment(self, request: CancelAppointmentRequest) -> AppointmentMutationResult:
        return self._native().cancel_appointment(request)

    def reschedule_appointment(self, request: RescheduleAppointmentRequest) -> AppointmentMutationResult:
        return self._native().reschedule_appointment(request)

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - dependency is required in deployed backend
            raise PostgresReadonlyConnectorError(
                "psycopg is required for the PostgreSQL clinic connector."
            ) from exc
        try:
            return psycopg.connect(
                self._dsn,
                connect_timeout=self.config.connect_timeout_seconds,
                row_factory=dict_row,
            )
        except Exception as exc:
            raise PostgresReadonlyConnectorError(
                "Could not connect to the external PostgreSQL clinic database."
            ) from exc

    @contextmanager
    def _read_cursor(self):
        connection = self._connect()
        try:
            if hasattr(connection, "read_only"):
                connection.read_only = True
            cursor = connection.cursor()
            try:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(self.config.statement_timeout_ms),),
                )
                yield cursor
            finally:
                close = getattr(cursor, "close", None)
                if callable(close):
                    close()
        finally:
            rollback = getattr(connection, "rollback", None)
            if callable(rollback):
                try:
                    rollback()
                except Exception:
                    pass
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def _display_name(self, schema_name: str, table_name: str) -> str:
        if len(self.config.schemas) == 1:
            return table_name
        return f"{schema_name}.{table_name}"

    def _load_table_metadata(self) -> dict[str, _TableMeta]:
        with self._read_cursor() as cursor:
            cursor.execute(
                """
                /* tia:postgres-schema-columns */
                SELECT table_schema, table_name, column_name, data_type, udt_name,
                       is_nullable, ordinal_position
                FROM information_schema.columns
                WHERE table_schema = ANY(%s)
                ORDER BY table_schema, table_name, ordinal_position
                """,
                (self.config.schemas,),
            )
            column_rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                /* tia:postgres-schema-primary-keys */
                SELECT tc.table_schema, tc.table_name, kcu.column_name, kcu.ordinal_position
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.constraint_schema = kcu.constraint_schema
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = ANY(%s)
                ORDER BY tc.table_schema, tc.table_name, kcu.ordinal_position
                """,
                (self.config.schemas,),
            )
            pk_rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                /* tia:postgres-schema-foreign-keys */
                SELECT tc.table_schema, tc.table_name, kcu.column_name,
                       ref_kcu.table_schema AS foreign_table_schema,
                       ref_kcu.table_name AS foreign_table_name,
                       ref_kcu.column_name AS foreign_column_name
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.constraint_schema = kcu.constraint_schema
                JOIN information_schema.referential_constraints AS rc
                  ON rc.constraint_name = tc.constraint_name
                 AND rc.constraint_schema = tc.constraint_schema
                JOIN information_schema.key_column_usage AS ref_kcu
                  ON ref_kcu.constraint_name = rc.unique_constraint_name
                 AND ref_kcu.constraint_schema = rc.unique_constraint_schema
                 AND ref_kcu.ordinal_position = kcu.position_in_unique_constraint
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = ANY(%s)
                """,
                (self.config.schemas,),
            )
            fk_rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                /* tia:postgres-schema-estimates */
                SELECT n.nspname AS table_schema, c.relname AS table_name,
                       GREATEST(c.reltuples, 0)::bigint AS estimated_row_count
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p')
                  AND n.nspname = ANY(%s)
                """,
                (self.config.schemas,),
            )
            estimate_rows = [dict(row) for row in cursor.fetchall()]

        pk_index = {
            (str(row["table_schema"]), str(row["table_name"]), str(row["column_name"])): int(
                row["ordinal_position"]
            )
            for row in pk_rows
        }
        fk_index = {
            (str(row["table_schema"]), str(row["table_name"]), str(row["column_name"])): (
                str(row["foreign_table_schema"]),
                str(row["foreign_table_name"]),
                str(row["foreign_column_name"]),
            )
            for row in fk_rows
        }
        estimates = {
            (str(row["table_schema"]), str(row["table_name"])): int(row["estimated_row_count"])
            for row in estimate_rows
        }
        grouped: dict[tuple[str, str], list[_ColumnMeta]] = {}
        for row in column_rows:
            schema_name = str(row["table_schema"])
            table_name = str(row["table_name"])
            column_name = str(row["column_name"])
            fk = fk_index.get((schema_name, table_name, column_name))
            ref_table = self._display_name(fk[0], fk[1]) if fk else None
            grouped.setdefault((schema_name, table_name), []).append(
                _ColumnMeta(
                    name=column_name,
                    raw_type=str(row.get("udt_name") or row.get("data_type") or "").lower(),
                    kind=_kind_for_postgres(
                        data_type=str(row.get("data_type") or ""),
                        udt_name=str(row.get("udt_name") or ""),
                    ),
                    nullable=str(row.get("is_nullable") or "YES").upper() == "YES",
                    primary_key=(schema_name, table_name, column_name) in pk_index,
                    pk_position=pk_index.get((schema_name, table_name, column_name)),
                    references_table=ref_table,
                    references_column=fk[2] if fk else None,
                )
            )

        tables: dict[str, _TableMeta] = {}
        for (schema_name, table_name), columns in grouped.items():
            display = self._display_name(schema_name, table_name)
            qualified_display = f"{schema_name}.{table_name}"
            if (
                self.config.include_tables is not None
                and display not in self.config.include_tables
                and qualified_display not in self.config.include_tables
            ):
                continue
            if display in tables:
                raise PostgresReadonlyConnectorError(
                    f"PostgreSQL connector exposes ambiguous table name {display!r}. Configure explicit schemas."
                )
            tables[display] = _TableMeta(
                display_name=display,
                schema_name=schema_name,
                table_name=table_name,
                columns=tuple(columns),
                estimated_row_count=estimates.get((schema_name, table_name)),
            )
        if not tables:
            raise PostgresReadonlyConnectorError(
                "PostgreSQL connector schema contains no readable configured tables."
            )
        if len(tables) > 200:
            raise PostgresReadonlyConnectorError(
                "PostgreSQL connector exposes more than 200 tables. Restrict schemas/include_tables first."
            )
        return tables

    def discover_sync_schema(self) -> ClinicConnectorSchemaSnapshot:
        metadata = self._load_table_metadata()
        snapshot = ClinicConnectorSchemaSnapshot(
            tables=[
                ClinicConnectorTableSchema(
                    name=table.display_name,
                    estimated_row_count=table.estimated_row_count,
                    columns=[
                        ClinicConnectorColumnSchema(
                            name=column.name,
                            kind=column.kind,
                            nullable=column.nullable,
                            primary_key=column.primary_key,
                            references_table=column.references_table,
                            references_column=column.references_column,
                        )
                        for column in table.columns
                    ],
                )
                for table in sorted(metadata.values(), key=lambda item: item.display_name)
            ]
        )
        fingerprint = schema_fingerprint(snapshot)
        snapshot.revision = f"postgres:{fingerprint[:24]}"
        return snapshot

    @staticmethod
    def _sample_value(value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, (datetime, date, time, Decimal, UUID)):
            return str(value)
        if isinstance(value, (dict, list, tuple)):
            text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        else:
            text = str(value)
        return text if len(text) <= 300 else text[:300] + "…"

    def sample_sync_schema_rows(
        self, *, rows_per_table: int = 3, max_tables: int = 30
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        """Return bounded transient row samples for AI mapping only.

        This method is deliberately not used by scheduled sync. It never accepts SQL
        or identifiers from the model; identifiers come from PostgreSQL catalog discovery.
        """
        row_limit = max(1, min(int(rows_per_table), 5))
        table_limit = max(1, min(int(max_tables), 40))
        metadata = self._load_table_metadata()
        result: dict[str, tuple[dict[str, Any], ...]] = {}
        with self._read_cursor() as cursor:
            for table in sorted(metadata.values(), key=lambda item: item.display_name)[:table_limit]:
                selected_columns = [column.name for column in table.columns[:80]]
                if not selected_columns:
                    continue
                columns_sql = ", ".join(_quote_identifier(name) for name in selected_columns)
                order_sql = ""
                if table.primary_key:
                    order_sql = " ORDER BY " + ", ".join(
                        _quote_identifier(column.name) for column in table.primary_key
                    )
                cursor.execute(
                    f"/* tia:postgres-onboarding-sample */ SELECT {columns_sql} "
                    f"FROM {_qualified_table(table)}{order_sql} LIMIT %s",
                    (row_limit,),
                )
                rows = cursor.fetchall()
                result[table.display_name] = tuple(
                    {key: self._sample_value(value) for key, value in dict(row).items()}
                    for row in rows
                )
        return result

    def _mapping(self) -> ClinicSyncMapping:
        raw = self._approved_mapping_raw
        if raw is None:
            raise PostgresReadonlyConnectorError(
                "PostgreSQL connector raw sync requires an approved connector mapping."
            )
        try:
            return ClinicSyncMapping.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise PostgresReadonlyConnectorError("Approved PostgreSQL sync mapping is invalid.") from exc

    @staticmethod
    def _can_filter_payment_allocations(mapping: ClinicSyncMapping) -> bool:
        if mapping.payment_allocations is None or mapping.payments is None:
            return False
        allocation_sheet = mapping.payment_allocations.sheet
        if allocation_sheet in {item.name for item in mapping.transformations}:
            return False
        payment_only = mapping.model_copy(update={"payment_allocations": None})
        payment_plan = sync_mapping_extraction_plan(payment_only, ClinicSyncDomain.PAYMENTS)
        return allocation_sheet not in payment_plan.raw_sheets

    def validate_sync_mapping_runtime(
        self,
        mapping: ClinicSyncMapping,
        snapshot: ClinicConnectorSchemaSnapshot,
    ) -> None:
        snapshot_tables = {table.name: table for table in snapshot.tables}
        for domain in self.raw_sync_domains:
            section = {
                ClinicSyncDomain.PATIENTS: mapping.patients,
                ClinicSyncDomain.APPOINTMENTS: mapping.appointments,
                ClinicSyncDomain.PAYMENTS: mapping.payments,
            }[domain]
            if section is None:
                continue
            plan = sync_mapping_extraction_plan(mapping, domain)
            root = snapshot_tables.get(plan.root_sheet)
            if root is None:
                raise PostgresReadonlyConnectorError(
                    f"{domain.value}: root table {plan.root_sheet!r} is missing from PostgreSQL schema."
                )
            primary = [column for column in root.columns if column.primary_key]
            if not primary:
                raise PostgresReadonlyConnectorError(
                    f"{domain.value}: PostgreSQL sync root table {plan.root_sheet!r} needs a primary key for deterministic paging."
                )
            for table_name in plan.raw_sheets - {plan.root_sheet}:
                table = snapshot_tables.get(table_name)
                if table is None:
                    raise PostgresReadonlyConnectorError(
                        f"{domain.value}: required lookup table {table_name!r} is missing."
                    )
                allocation_raw = (
                    domain == ClinicSyncDomain.PAYMENTS
                    and mapping.payment_allocations is not None
                    and mapping.payment_allocations.sheet == table_name
                    and self._can_filter_payment_allocations(mapping)
                )
                if (
                    not allocation_raw
                    and table.estimated_row_count is not None
                    and table.estimated_row_count > self.config.lookup_table_row_limit
                ):
                    raise PostgresReadonlyConnectorError(
                        f"{domain.value}: lookup table {table_name!r} is too large for generic bounded extraction. "
                        "Use a connector-specific extraction strategy or reduce the mapped lookup scope."
                    )

        transform_names = {item.name for item in mapping.transformations}
        for entity_type, reference in (
            ("branch", mapping.references.branches),
            ("service", mapping.references.services),
            ("doctor", mapping.references.doctors),
        ):
            if reference is None:
                continue
            if reference.sheet in transform_names:
                raise PostgresReadonlyConnectorError(
                    f"{entity_type}: generic PostgreSQL reference candidates require a raw table mapping."
                )

        metadata = self._load_table_metadata()
        for domain in self.raw_sync_domains:
            section = {
                ClinicSyncDomain.PATIENTS: mapping.patients,
                ClinicSyncDomain.APPOINTMENTS: mapping.appointments,
                ClinicSyncDomain.PAYMENTS: mapping.payments,
            }[domain]
            if section is None:
                continue
            plan = sync_mapping_extraction_plan(mapping, domain)
            table = metadata.get(plan.root_sheet)
            if table is None:
                continue
            unsupported = [
                column.raw_type
                for column in table.primary_key
                if column.raw_type not in _SUPPORTED_CURSOR_TYPES
            ]
            if unsupported:
                raise PostgresReadonlyConnectorError(
                    f"{domain.value}: PostgreSQL root primary key uses unsupported paging type {unsupported[0]!r}."
                )

    def list_reference_candidates(
        self, *, mapping: ClinicSyncMapping, entity_type: str, limit: int = 500
    ) -> tuple[ClinicReferenceCandidate, ...]:
        reference = {
            "branch": mapping.references.branches,
            "service": mapping.references.services,
            "doctor": mapping.references.doctors,
        }.get(entity_type)
        if reference is None:
            raise PostgresReadonlyConnectorError(
                f"Approved mapping does not configure {entity_type} reference identities."
            )
        if reference.sheet in {item.name for item in mapping.transformations}:
            raise PostgresReadonlyConnectorError(
                "Generic PostgreSQL reference candidate extraction requires a raw table mapping."
            )
        metadata = self._load_table_metadata()
        table = metadata.get(reference.sheet)
        if table is None:
            raise PostgresReadonlyConnectorError(
                f"Reference table {reference.sheet!r} is unavailable in PostgreSQL schema."
            )
        if reference.external_id not in table.column_names:
            raise PostgresReadonlyConnectorError("Reference external-id column is unavailable.")
        if reference.label and reference.label not in table.column_names:
            raise PostgresReadonlyConnectorError("Reference label column is unavailable.")
        bounded = max(1, min(int(limit), 1000))
        id_col = _quote_identifier(reference.external_id)
        label_expr = _quote_identifier(reference.label) if reference.label else "NULL"
        with self._read_cursor() as cursor:
            cursor.execute(
                f"/* tia:postgres-reference-candidates */ "
                f"SELECT DISTINCT {id_col} AS external_id, {label_expr} AS label "
                f"FROM {_qualified_table(table)} "
                f"WHERE {id_col} IS NOT NULL ORDER BY {id_col} LIMIT %s",
                (bounded + 1,),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        if len(rows) > bounded:
            raise PostgresReadonlyConnectorError(
                f"{entity_type} reference candidates exceed the safe onboarding limit {bounded}."
            )
        by_external_id: dict[str, str | None] = {}
        for row in rows:
            external_id = str(row.get("external_id") or "").strip()
            if not external_id:
                continue
            raw_label = row.get("label")
            label = str(raw_label).strip() if raw_label is not None else None
            normalized_label = label or None
            if external_id in by_external_id and by_external_id[external_id] != normalized_label:
                raise PostgresReadonlyConnectorError(
                    f"{entity_type} reference id {external_id!r} has conflicting labels in the source system."
                )
            by_external_id[external_id] = normalized_label
        return tuple(
            ClinicReferenceCandidate(
                entity_type=entity_type, external_id=external_id, label=label
            )
            for external_id, label in sorted(by_external_id.items())
        )

    def _encode_cursor(
        self,
        *,
        domain: ClinicSyncDomain,
        root: _TableMeta,
        fingerprint: str,
        row: dict[str, Any],
    ) -> str:
        payload = {
            "v": 1,
            "domain": domain.value,
            "root": root.display_name,
            "schema": fingerprint,
            "pk": [_cursor_json_value(row[column.name]) for column in root.primary_key],
        }
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    def _decode_cursor(
        self,
        *,
        cursor: str | None,
        domain: ClinicSyncDomain,
        root: _TableMeta,
        fingerprint: str,
    ) -> tuple[Any, ...] | None:
        if cursor is None:
            return None
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        except Exception as exc:
            raise PostgresReadonlyConnectorError("PostgreSQL sync cursor is invalid.") from exc
        if not isinstance(payload, dict):
            raise PostgresReadonlyConnectorError("PostgreSQL sync cursor is invalid.")
        if (
            payload.get("v") != 1
            or payload.get("domain") != domain.value
            or payload.get("root") != root.display_name
            or payload.get("schema") != fingerprint
        ):
            raise PostgresReadonlyConnectorError(
                "PostgreSQL sync cursor does not match the active mapping/schema."
            )
        values = payload.get("pk")
        if not isinstance(values, list) or len(values) != len(root.primary_key):
            raise PostgresReadonlyConnectorError("PostgreSQL sync cursor primary key is invalid.")
        try:
            return tuple(
                _decode_typed_cursor_value(column.raw_type, str(value))
                for column, value in zip(root.primary_key, values, strict=True)
            )
        except (TypeError, ValueError) as exc:
            raise PostgresReadonlyConnectorError("PostgreSQL sync cursor value is invalid.") from exc

    def _fetch_root_page(
        self,
        *,
        cursor,
        root: _TableMeta,
        after: tuple[Any, ...] | None,
        limit: int,
        required_columns: frozenset[str],
    ) -> tuple[list[dict[str, Any]], bool]:
        pk = root.primary_key
        selected = required_columns | {column.name for column in pk}
        unknown = selected - root.column_names
        if unknown:
            raise PostgresReadonlyConnectorError(
                f"PostgreSQL root mapping references unavailable column {sorted(unknown)[0]!r}."
            )
        columns = ", ".join(_quote_identifier(name) for name in sorted(selected))
        order = ", ".join(_quote_identifier(column.name) for column in pk)
        params: list[Any] = []
        where = ""
        if after is not None:
            left = ", ".join(_quote_identifier(column.name) for column in pk)
            placeholders = ", ".join("%s" for _ in pk)
            where = f" WHERE ({left}) > ({placeholders})"
            params.extend(after)
        params.append(limit + 1)
        cursor.execute(
            f"/* tia:postgres-root-page */ SELECT {columns} FROM {_qualified_table(root)}"
            f"{where} ORDER BY {order} LIMIT %s",
            tuple(params),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        return rows[:limit], len(rows) > limit

    def _fetch_bounded_table(
        self, *, cursor, table: _TableMeta, required_columns: frozenset[str]
    ) -> list[dict[str, Any]]:
        selected = set(required_columns)
        unknown = selected - table.column_names
        if unknown:
            raise PostgresReadonlyConnectorError(
                f"PostgreSQL lookup mapping references unavailable column {sorted(unknown)[0]!r}."
            )
        if not selected:
            return []
        columns = ", ".join(_quote_identifier(name) for name in sorted(selected))
        cursor.execute(
            f"/* tia:postgres-bounded-lookup */ SELECT {columns} FROM {_qualified_table(table)} "
            "LIMIT %s",
            (self.config.lookup_table_row_limit + 1,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        if len(rows) > self.config.lookup_table_row_limit:
            raise PostgresReadonlyConnectorError(
                f"Lookup table {table.display_name!r} exceeds the generic connector safety limit."
            )
        _sort_rows(rows, selected)
        return rows

    def _materialized_payment_ids(
        self,
        *,
        mapping: ClinicSyncMapping,
        plan,
        tables: dict[str, list[dict[str, Any]]],
    ) -> list[str]:
        assert mapping.payments is not None
        try:
            materialized, _summary = apply_structural_transforms(
                {name: list(rows) for name, rows in tables.items()}, list(plan.transformations)
            )
        except Exception as exc:
            raise PostgresReadonlyConnectorError(
                "Could not materialize payment mapping for allocation extraction."
            ) from exc
        rows = materialized.get(mapping.payments.sheet, [])
        values: list[str] = []
        for row in rows:
            value = row.get(mapping.payments.external_id)
            if value is not None and str(value).strip():
                values.append(str(value).strip())
        return list(dict.fromkeys(values))

    def _fetch_payment_allocations(
        self,
        *,
        cursor,
        table: _TableMeta,
        mapping: ClinicSyncMapping,
        payment_ids: list[str],
    ) -> list[dict[str, Any]]:
        assert mapping.payment_allocations is not None
        column = mapping.payment_allocations.payment_external_id
        if column not in table.column_names:
            raise PostgresReadonlyConnectorError(
                "Payment allocation filter column is missing from PostgreSQL table."
            )
        if not payment_ids:
            return []
        selected = {
            mapping.payment_allocations.payment_external_id,
            mapping.payment_allocations.appointment_external_id,
            mapping.payment_allocations.amount_minor,
        }
        columns = ", ".join(_quote_identifier(name) for name in sorted(selected))
        cursor.execute(
            f"/* tia:postgres-payment-allocations */ SELECT {columns} FROM {_qualified_table(table)} "
            f"WHERE {_quote_identifier(column)} = ANY(%s) LIMIT %s",
            (payment_ids, self.config.lookup_table_row_limit + 1),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        if len(rows) > self.config.lookup_table_row_limit:
            raise PostgresReadonlyConnectorError(
                "Payment allocation rows for one sync page exceed the connector safety limit."
            )
        _sort_rows(rows, selected)
        return rows

    def fetch_raw_sync_page(self, request: ClinicRawSyncFetchRequest) -> ClinicRawSyncPage:
        if request.domain not in self.raw_sync_domains:
            raise PostgresReadonlyConnectorError(
                f"PostgreSQL connector does not expose {request.domain.value} raw sync."
            )
        mapping = self._mapping()
        plan = sync_mapping_extraction_plan(mapping, request.domain)
        required_columns = sync_mapping_required_raw_columns(mapping, request.domain)
        metadata = self._load_table_metadata()
        snapshot = self.discover_sync_schema()
        fingerprint = schema_fingerprint(snapshot)
        root = metadata.get(plan.root_sheet)
        if root is None:
            raise PostgresReadonlyConnectorError(
                f"PostgreSQL sync root table {plan.root_sheet!r} is unavailable."
            )
        if not root.primary_key:
            raise PostgresReadonlyConnectorError(
                f"PostgreSQL sync root table {plan.root_sheet!r} needs a primary key."
            )
        after = self._decode_cursor(
            cursor=request.cursor,
            domain=request.domain,
            root=root,
            fingerprint=fingerprint,
        )
        limit = max(1, min(int(request.limit), self.config.max_page_size))
        with self._read_cursor() as cursor:
            root_rows, has_more = self._fetch_root_page(
                cursor=cursor,
                root=root,
                after=after,
                limit=limit,
                required_columns=required_columns.get(plan.root_sheet, frozenset()),
            )
            tables: dict[str, list[dict[str, Any]]] = {plan.root_sheet: root_rows}
            allocation_table_name: str | None = None
            if (
                request.domain == ClinicSyncDomain.PAYMENTS
                and mapping.payment_allocations is not None
                and mapping.payment_allocations.sheet in plan.raw_sheets
                and self._can_filter_payment_allocations(mapping)
            ):
                allocation_table_name = mapping.payment_allocations.sheet

            for table_name in sorted(plan.raw_sheets - {plan.root_sheet}):
                if table_name == allocation_table_name:
                    continue
                table = metadata.get(table_name)
                if table is None:
                    raise PostgresReadonlyConnectorError(
                        f"PostgreSQL mapped lookup table {table_name!r} is unavailable."
                    )
                tables[table_name] = self._fetch_bounded_table(
                    cursor=cursor,
                    table=table,
                    required_columns=required_columns.get(table_name, frozenset()),
                )

            if allocation_table_name is not None:
                allocation_table = metadata.get(allocation_table_name)
                if allocation_table is None:
                    raise PostgresReadonlyConnectorError(
                        f"PostgreSQL payment allocation table {allocation_table_name!r} is unavailable."
                    )
                payment_ids = self._materialized_payment_ids(
                    mapping=mapping,
                    plan=plan,
                    tables=tables,
                )
                tables[allocation_table_name] = self._fetch_payment_allocations(
                    cursor=cursor,
                    table=allocation_table,
                    mapping=mapping,
                    payment_ids=payment_ids,
                )

        next_cursor = None
        if has_more and root_rows:
            next_cursor = self._encode_cursor(
                domain=request.domain,
                root=root,
                fingerprint=fingerprint,
                row=root_rows[-1],
            )
        return ClinicRawSyncPage(
            domain=request.domain,
            tables={name: tuple(rows) for name, rows in tables.items()},
            schema_fingerprint=fingerprint,
            cursor=request.cursor,
            next_cursor=next_cursor,
            source_revision=snapshot.revision,
            has_more=has_more,
        )


def build_postgres_readonly_adapter(
    *,
    secret_ref: str | None,
    config: dict[str, Any] | None,
    native_delegate: ClinicAdapter | None = None,
):
    return PostgresReadonlyClinicAdapter(
        secret_ref=secret_ref,
        config=config,
        native_delegate=native_delegate,
    )
