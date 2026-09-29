from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.integrations.clinic.authority import (
    PATIENT_EXTERNAL_SYNC_FIELDS,
    ClinicIntegrationAuthorityError,
    external_patient_fields,
    require_external_domain_authority,
)
from app.integrations.clinic.sync_contract import (
    ClinicSyncDomain,
    ClinicSyncPage,
    ExternalAppointmentSyncRecord,
    ExternalPatientSyncRecord,
    ExternalPaymentSyncRecord,
)
from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.clinic_integration import ClinicIntegration, ClinicIntegrationEntityLink
from app.models.clinic_integration_sync import (
    ClinicIntegrationSyncCheckpoint,
    ClinicIntegrationSyncFailure,
    ClinicIntegrationSyncRun,
)
from app.models.doctor import Doctor
from app.models.patient import PATIENT_SOURCES, PATIENT_STATUSES, Patient
from app.models.payment_transaction import (
    PAYMENT_METHODS,
    PaymentAllocation,
    PaymentTransaction,
)
from app.models.service import Service
from app.models.workspace import Workspace
from app.schemas.crm import normalize_phone
from app.services.activity import record_activity_event
from app.services.appointment_operations import (
    add_appointment_history,
    cancel_pending_appointment_jobs,
)
from app.services.payments import (
    reallocate_appointment_payments_on_reschedule,
    refresh_appointment_payment_snapshots,
    validate_allocation_total,
)


class ClinicIntegrationSyncError(ValueError):
    pass


class ClinicSyncRecordError(ClinicIntegrationSyncError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code[:80]
        self.safe_message = message[:300]
        self.retryable = retryable


@dataclass(frozen=True)
class ClinicSyncRunResult:
    run_id: UUID
    domain: ClinicSyncDomain
    status: str
    cursor_before: str | None
    cursor_after: str | None
    processed_count: int
    created_count: int
    updated_count: int
    skipped_count: int
    failed_count: int


SyncOutcome = Literal["created", "updated", "skipped"]


def _clean_required(value: str, *, field: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ClinicSyncRecordError("invalid_record", f"{field} is required.", retryable=False)
    if len(text) > max_length:
        raise ClinicSyncRecordError(
            "invalid_record",
            f"{field} exceeds the supported length.",
            retryable=False,
        )
    return text


def _optional_text(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_length:
        raise ClinicSyncRecordError(
            "invalid_record",
            "An optional external field exceeds the supported length.",
            retryable=False,
        )
    return text


def _positive_int(value: object, *, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ClinicSyncRecordError(
            "invalid_record",
            f"{field} must be an integer.",
            retryable=False,
        ) from exc
    if result <= 0:
        raise ClinicSyncRecordError(
            "invalid_record",
            f"{field} must be positive.",
            retryable=False,
        )
    return result


def _nonnegative_int(value: object, *, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ClinicSyncRecordError(
            "invalid_record",
            f"{field} must be an integer.",
            retryable=False,
        ) from exc
    if result < 0:
        raise ClinicSyncRecordError(
            "invalid_record",
            f"{field} must be non-negative.",
            retryable=False,
        )
    return result


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ClinicSyncRecordError(
            "invalid_record",
            f"{field} must include timezone information.",
            retryable=False,
        )
    return value.astimezone(UTC)


def _cursor_value(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    if len(text) > 512:
        raise ClinicIntegrationSyncError(f"{field} exceeds the supported length.")
    return text


def _external_id(record: object) -> str:
    value = getattr(record, "external_id", "")
    return str(value or "").strip()


def _external_id_digest(external_id: str) -> str:
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ClinicSyncRecordError(
            "invalid_record",
            "External timestamps must include timezone information.",
            retryable=False,
        )
    return value.astimezone(UTC).isoformat()


def _link_for_external(
    db: Session,
    *,
    workspace_id: UUID,
    entity_type: str,
    external_id: str,
) -> ClinicIntegrationEntityLink | None:
    return db.scalar(
        select(ClinicIntegrationEntityLink).where(
            ClinicIntegrationEntityLink.workspace_id == workspace_id,
            ClinicIntegrationEntityLink.entity_type == entity_type,
            ClinicIntegrationEntityLink.external_id == external_id,
        )
    )


def _link_for_canonical(
    db: Session,
    *,
    workspace_id: UUID,
    entity_type: str,
    canonical_id: UUID,
) -> ClinicIntegrationEntityLink | None:
    return db.scalar(
        select(ClinicIntegrationEntityLink).where(
            ClinicIntegrationEntityLink.workspace_id == workspace_id,
            ClinicIntegrationEntityLink.entity_type == entity_type,
            ClinicIntegrationEntityLink.canonical_id == str(canonical_id),
        )
    )


def _link_uuid(link: ClinicIntegrationEntityLink, *, entity_type: str) -> UUID:
    try:
        return UUID(str(link.canonical_id))
    except (TypeError, ValueError) as exc:
        raise ClinicSyncRecordError(
            "broken_entity_link",
            f"Existing {entity_type} entity link is invalid.",
            retryable=False,
        ) from exc


def _ensure_link(
    db: Session,
    *,
    workspace_id: UUID,
    entity_type: str,
    canonical_id: UUID,
    external_id: str,
    metadata: dict,
) -> ClinicIntegrationEntityLink:
    by_external = _link_for_external(
        db,
        workspace_id=workspace_id,
        entity_type=entity_type,
        external_id=external_id,
    )
    if by_external is not None:
        if by_external.canonical_id != str(canonical_id):
            raise ClinicSyncRecordError(
                "identity_conflict",
                f"External {entity_type} id is already linked to another Linka entity.",
                retryable=False,
            )
        by_external.metadata_json = metadata
        return by_external

    by_canonical = _link_for_canonical(
        db,
        workspace_id=workspace_id,
        entity_type=entity_type,
        canonical_id=canonical_id,
    )
    if by_canonical is not None and by_canonical.external_id != external_id:
        raise ClinicSyncRecordError(
            "identity_conflict",
            f"Linka {entity_type} is already linked to another external entity.",
            retryable=False,
        )

    link = ClinicIntegrationEntityLink(
        workspace_id=workspace_id,
        entity_type=entity_type,
        canonical_id=str(canonical_id),
        external_id=external_id,
        metadata_json=metadata,
    )
    db.add(link)
    return link


def _patient_payload(record: ExternalPatientSyncRecord) -> tuple[dict, str | None, str | None]:
    first_name = _clean_required(record.first_name, field="first_name", max_length=120)
    last_name = _optional_text(record.last_name, max_length=120)
    status = _clean_required(record.status, field="status", max_length=20).lower()
    source = _clean_required(record.source, field="source", max_length=32).lower()
    language = _clean_required(
        record.preferred_language, field="preferred_language", max_length=10
    ).lower()
    if status not in PATIENT_STATUSES:
        raise ClinicSyncRecordError(
            "invalid_patient_status",
            "External patient status is not canonical.",
            retryable=False,
        )
    if source not in PATIENT_SOURCES:
        raise ClinicSyncRecordError(
            "invalid_patient_source",
            "External patient source is not canonical.",
            retryable=False,
        )

    gender = _optional_text(record.gender, max_length=32)
    birth_date = record.birth_date
    source_created_at = (
        _aware_utc(record.source_created_at, field="source_created_at")
        if record.source_created_at is not None
        else None
    )

    display_phone: str | None = None
    normalized_phone: str | None = None
    if record.phone:
        try:
            display_phone, normalized_phone = normalize_phone(record.phone)
        except ValueError as exc:
            raise ClinicSyncRecordError(
                "invalid_patient_phone",
                "External patient phone number is invalid.",
                retryable=False,
            ) from exc
    updated_at = _iso(record.source_updated_at)
    return (
        {
            "first_name": first_name,
            "last_name": last_name,
            "phone": display_phone,
            "phone_normalized": normalized_phone,
            "gender": gender,
            "birth_date": birth_date,
            "source_created_at": source_created_at,
            "status": status,
            "preferred_language": language,
            "source": source,
        },
        updated_at,
        normalized_phone,
    )


def _find_patient_identity(
    db: Session,
    *,
    workspace_id: UUID,
    normalized_phone: str | None,
) -> Patient | None:
    if not normalized_phone:
        return None
    return db.scalar(
        select(Patient).where(
            Patient.workspace_id == workspace_id,
            Patient.phone_normalized == normalized_phone,
        )
    )


def _patient_model_payload(patient: Patient) -> dict:
    return {
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "phone": patient.phone,
        "phone_normalized": patient.phone_normalized,
        "gender": patient.gender,
        "birth_date": patient.birth_date,
        "source_created_at": patient.source_created_at,
        "status": patient.status,
        "preferred_language": patient.preferred_language,
        "source": patient.source,
    }


def _patient_field_fingerprints(payload: dict) -> dict[str, str]:
    values: dict[str, object] = {
        "first_name": payload["first_name"],
        "last_name": payload["last_name"],
        "phone": [payload["phone"], payload["phone_normalized"]],
        "gender": payload["gender"],
        "birth_date": payload["birth_date"],
        "source_created_at": payload["source_created_at"],
        "status": payload["status"],
        "preferred_language": payload["preferred_language"],
        "source": payload["source"],
    }
    return {field_name: _fingerprint({"value": value}) for field_name, value in values.items()}


def _apply_external_patient_fields(
    patient: Patient,
    *,
    payload: dict,
    fields: frozenset[str],
) -> bool:
    changed = False
    for field_name in fields:
        if field_name == "phone":
            next_values = (payload["phone"], payload["phone_normalized"])
            current_values = (patient.phone, patient.phone_normalized)
            if current_values != next_values:
                patient.phone, patient.phone_normalized = next_values
                changed = True
            continue
        next_value = payload[field_name]
        if getattr(patient, field_name) != next_value:
            setattr(patient, field_name, next_value)
            changed = True
    return changed


def _sync_patient(
    db: Session,
    *,
    workspace: Workspace,
    integration: ClinicIntegration,
    record: ExternalPatientSyncRecord,
    run_id: UUID,
) -> SyncOutcome:
    external_id = _clean_required(record.external_id, field="external_id", max_length=512)
    payload, source_updated_at, normalized_phone = _patient_payload(record)
    fingerprint = _fingerprint(payload)
    incoming_field_fingerprints = _patient_field_fingerprints(payload)
    managed_fields = external_patient_fields(integration)
    if not managed_fields:
        raise ClinicSyncRecordError(
            "source_authority_denied",
            "External sync is not authoritative for any patient fields.",
            retryable=False,
        )

    link = _link_for_external(
        db,
        workspace_id=workspace.id,
        entity_type="patient",
        external_id=external_id,
    )
    patient: Patient | None = None
    link_existed = link is not None
    previous_metadata: dict = {}
    if link is not None:
        patient = db.get(Patient, _link_uuid(link, entity_type="patient"))
        if patient is None or patient.workspace_id != workspace.id:
            raise ClinicSyncRecordError(
                "broken_entity_link",
                "External patient link points to a missing Linka patient.",
                retryable=False,
            )
        previous_metadata = dict(link.metadata_json or {})
        previous_fingerprint = previous_metadata.get("sync_fingerprint")
        previous_updated_at = previous_metadata.get("source_updated_at")
        current_payload = _patient_model_payload(patient)
        current_fingerprint = _fingerprint(current_payload)
        previous_applied_fields = previous_metadata.get("applied_field_fingerprints")

        conflicts: list[str] = []
        if isinstance(previous_applied_fields, dict):
            current_field_fingerprints = _patient_field_fingerprints(current_payload)
            for field_name in managed_fields:
                previous_applied = previous_applied_fields.get(field_name)
                if not previous_applied:
                    continue
                current_value = current_field_fingerprints[field_name]
                incoming_value = incoming_field_fingerprints[field_name]
                if current_value != previous_applied and current_value != incoming_value:
                    conflicts.append(field_name)
        elif (
            previous_fingerprint
            and managed_fields == frozenset(PATIENT_EXTERNAL_SYNC_FIELDS)
            and current_fingerprint != previous_fingerprint
            and current_fingerprint != fingerprint
        ):
            conflicts.append("legacy_external_patient_fields")

        if conflicts:
            raise ClinicSyncRecordError(
                "source_authority_conflict",
                "Linka and the external clinic system changed the same authoritative patient data.",
                retryable=False,
            )

        if source_updated_at and previous_updated_at:
            if source_updated_at < str(previous_updated_at):
                return "skipped"
            if source_updated_at == str(previous_updated_at) and previous_fingerprint != fingerprint:
                raise ClinicSyncRecordError(
                    "source_version_conflict",
                    "External patient changed without a newer source timestamp.",
                    retryable=False,
                )

        if previous_fingerprint == fingerprint and not conflicts:
            return "skipped"

        identity_candidate = _find_patient_identity(
            db,
            workspace_id=workspace.id,
            normalized_phone=normalized_phone,
        )
        if identity_candidate is not None and identity_candidate.id != patient.id:
            raise ClinicSyncRecordError(
                "patient_identity_conflict",
                "Linked external patient now resolves to another Linka patient identity.",
                retryable=False,
            )
        outcome: SyncOutcome = "updated"
    else:
        patient = _find_patient_identity(
            db,
            workspace_id=workspace.id,
            normalized_phone=normalized_phone,
        )
        if patient is None:
            # Creation needs a complete canonical row. External values seed the
            # new patient once; authority controls subsequent mutations.
            patient = Patient(
                workspace_id=workspace.id,
                first_name=payload["first_name"],
                last_name=payload["last_name"],
                phone=payload["phone"],
                phone_normalized=payload["phone_normalized"],
                gender=payload["gender"],
                birth_date=payload["birth_date"],
                source_created_at=payload["source_created_at"],
                status=payload["status"],
                preferred_language=payload["preferred_language"],
                source=payload["source"],
                source_detail="External clinic sync",
            )
            db.add(patient)
            db.flush()
            outcome = "created"
        else:
            outcome = "updated"

    assert patient is not None
    changed = False
    if outcome != "created":
        changed = _apply_external_patient_fields(
            patient,
            payload=payload,
            fields=managed_fields,
        )
    if not patient.source_detail or patient.source_detail == "External clinic sync":
        patient.source_detail = "External clinic sync"
    db.flush()

    applied_field_fingerprints = _patient_field_fingerprints(_patient_model_payload(patient))
    _ensure_link(
        db,
        workspace_id=workspace.id,
        entity_type="patient",
        canonical_id=patient.id,
        external_id=external_id,
        metadata={
            "ownership": "external_sync",
            "sync_fingerprint": fingerprint,
            "source_updated_at": source_updated_at,
            "source_field_fingerprints": incoming_field_fingerprints,
            "applied_field_fingerprints": applied_field_fingerprints,
        },
    )

    effective_outcome: SyncOutcome = outcome
    if link_existed and outcome == "updated" and not changed:
        effective_outcome = "skipped"
    if effective_outcome != "skipped":
        record_activity_event(
            db,
            workspace_id=workspace.id,
            actor_type="system",
            action=f"integration.patient.{effective_outcome}",
            entity_type="patient",
            entity_id=patient.id,
            summary=f"External patient {effective_outcome}",
            metadata={"sync_run_id": run_id, "domain": "patients"},
        )
    return effective_outcome


def _resolve_linked_uuid(
    db: Session,
    *,
    workspace_id: UUID,
    entity_type: str,
    external_id: str,
) -> UUID:
    link = _link_for_external(
        db,
        workspace_id=workspace_id,
        entity_type=entity_type,
        external_id=external_id,
    )
    if link is None:
        raise ClinicSyncRecordError(
            "missing_dependency_link",
            f"External {entity_type} must be synchronized or linked first.",
            retryable=True,
        )
    return _link_uuid(link, entity_type=entity_type)


_EXTERNAL_APPOINTMENT_STATUSES = frozenset({"pending", "confirmed", "completed", "cancelled", "no_show"})
_ACTIVE_EXTERNAL_APPOINTMENT_STATUSES = frozenset({"pending", "confirmed"})
_TERMINAL_EXTERNAL_APPOINTMENT_STATUSES = frozenset({"completed", "cancelled", "no_show"})


def _resolve_linked_model(
    db: Session,
    *,
    workspace_id: UUID,
    entity_type: str,
    external_id: str,
    model_type,
):
    canonical_id = _resolve_linked_uuid(
        db,
        workspace_id=workspace_id,
        entity_type=entity_type,
        external_id=_clean_required(external_id, field=f"{entity_type}_external_id", max_length=512),
    )
    entity = db.get(model_type, canonical_id)
    if entity is None or entity.workspace_id != workspace_id:
        raise ClinicSyncRecordError(
            "missing_dependency",
            f"Linked external {entity_type} does not exist in this workspace.",
            retryable=True,
        )
    return entity


def _appointment_payload(
    db: Session,
    *,
    workspace: Workspace,
    record: ExternalAppointmentSyncRecord,
) -> tuple[dict, dict, str | None]:
    external_id = _clean_required(record.external_id, field="external_id", max_length=512)
    del external_id
    status = _clean_required(record.status, field="status", max_length=20).lower()
    if status not in _EXTERNAL_APPOINTMENT_STATUSES:
        raise ClinicSyncRecordError(
            "invalid_appointment_status",
            "External appointment status is not canonical.",
            retryable=False,
        )

    start_at = _aware_utc(record.start_at, field="start_at")
    end_at = _aware_utc(record.end_at, field="end_at")
    if end_at <= start_at:
        raise ClinicSyncRecordError(
            "invalid_appointment_interval",
            "External appointment end time must be after its start time.",
            retryable=False,
        )
    duration_minutes = int((end_at - start_at).total_seconds() // 60)
    if duration_minutes <= 0 or duration_minutes > 1440:
        raise ClinicSyncRecordError(
            "invalid_appointment_interval",
            "External appointment duration is outside the supported range.",
            retryable=False,
        )

    status_at = _aware_utc(record.status_at, field="status_at") if record.status_at else None
    if status in _TERMINAL_EXTERNAL_APPOINTMENT_STATUSES and status_at is None:
        raise ClinicSyncRecordError(
            "missing_appointment_status_time",
            "Terminal external appointment status requires status_at.",
            retryable=False,
        )
    if status in {"completed", "no_show"} and status_at is not None and status_at < start_at:
        raise ClinicSyncRecordError(
            "invalid_appointment_state",
            "Completed or no-show status cannot occur before appointment start.",
            retryable=False,
        )
    if status == "cancelled" and status_at is not None and status_at >= start_at:
        raise ClinicSyncRecordError(
            "invalid_appointment_state",
            "Cancelled external appointment has a cancellation time at or after its start.",
            retryable=False,
        )

    patient = _resolve_linked_model(
        db,
        workspace_id=workspace.id,
        entity_type="patient",
        external_id=record.patient_external_id,
        model_type=Patient,
    )
    branch = _resolve_linked_model(
        db,
        workspace_id=workspace.id,
        entity_type="branch",
        external_id=record.branch_external_id,
        model_type=Branch,
    )
    service = _resolve_linked_model(
        db,
        workspace_id=workspace.id,
        entity_type="service",
        external_id=record.service_external_id,
        model_type=Service,
    )
    doctor = _resolve_linked_model(
        db,
        workspace_id=workspace.id,
        entity_type="doctor",
        external_id=record.doctor_external_id,
        model_type=Doctor,
    )

    price_minor = service.price_minor if record.price_minor is None else _nonnegative_int(record.price_minor, field="price_minor")
    currency = service.currency if record.currency is None else _clean_required(record.currency, field="currency", max_length=3).upper()
    if len(currency) != 3:
        raise ClinicSyncRecordError(
            "invalid_appointment_currency",
            "External appointment currency must be a three-letter code.",
            retryable=False,
        )
    source_updated_at = _iso(record.source_updated_at)
    external_payload = {
        "patient_external_id": _clean_required(record.patient_external_id, field="patient_external_id", max_length=512),
        "branch_external_id": _clean_required(record.branch_external_id, field="branch_external_id", max_length=512),
        "service_external_id": _clean_required(record.service_external_id, field="service_external_id", max_length=512),
        "doctor_external_id": _clean_required(record.doctor_external_id, field="doctor_external_id", max_length=512),
        "status": status,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "status_at": status_at.isoformat() if status_at else None,
        "price_minor": price_minor,
        "currency": currency,
    }
    desired_model = {
        "patient_id": patient.id,
        "branch_id": branch.id,
        "service_id": service.id,
        "doctor_id": doctor.id,
        "status": status,
        "start_at": start_at,
        "end_at": end_at,
        "busy_start_at": start_at - timedelta(minutes=int(service.buffer_before_minutes or 0)),
        "busy_end_at": end_at + timedelta(minutes=int(service.buffer_after_minutes or 0)),
        "duration_minutes": duration_minutes,
        "price_minor": price_minor,
        "currency": currency,
        "status_at": status_at,
    }
    return external_payload, desired_model, source_updated_at


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _appointment_model_payload(appointment: Appointment) -> dict:
    status_at = None
    if appointment.status == "confirmed":
        status_at = appointment.confirmed_at
    elif appointment.status == "cancelled":
        status_at = appointment.cancelled_at
    elif appointment.status == "completed":
        status_at = appointment.completed_at
    elif appointment.status == "no_show":
        status_at = appointment.no_show_at
    return {
        "patient_id": str(appointment.patient_id),
        "branch_id": str(appointment.branch_id),
        "service_id": str(appointment.service_id),
        "doctor_id": str(appointment.doctor_id),
        "status": appointment.status,
        "start_at": _stored_utc(appointment.start_at).isoformat(),
        "end_at": _stored_utc(appointment.end_at).isoformat(),
        "price_minor": int(appointment.price_minor),
        "currency": appointment.currency,
        "status_at": _stored_utc(status_at).isoformat() if status_at else None,
    }


def _appointment_desired_model_payload(desired: dict) -> dict:
    return {
        "patient_id": str(desired["patient_id"]),
        "branch_id": str(desired["branch_id"]),
        "service_id": str(desired["service_id"]),
        "doctor_id": str(desired["doctor_id"]),
        "status": desired["status"],
        "start_at": desired["start_at"].isoformat(),
        "end_at": desired["end_at"].isoformat(),
        "price_minor": int(desired["price_minor"]),
        "currency": desired["currency"],
        "status_at": desired["status_at"].isoformat() if desired["status_at"] else None,
    }


def _set_appointment_status_timestamps(appointment: Appointment, *, status: str, status_at: datetime | None) -> None:
    if status == "confirmed":
        appointment.confirmed_at = status_at
    elif status == "cancelled":
        appointment.cancelled_at = status_at
        appointment.cancellation_reason = "external_sync"
    elif status == "completed":
        appointment.completed_at = status_at
    elif status == "no_show":
        appointment.no_show_at = status_at


def _validate_external_status_transition(current_status: str, target_status: str) -> None:
    if current_status == target_status:
        return
    allowed: dict[str, set[str]] = {
        "pending": {"confirmed", "cancelled", "completed", "no_show"},
        "confirmed": {"cancelled", "completed", "no_show"},
    }
    if target_status not in allowed.get(current_status, set()):
        raise ClinicSyncRecordError(
            "source_state_conflict",
            f"External appointment cannot transition from {current_status!r} to {target_status!r}.",
            retryable=False,
        )


def _create_external_appointment(
    db: Session,
    *,
    workspace: Workspace,
    desired: dict,
    external_id: str,
    run_id: UUID,
) -> Appointment:
    appointment = Appointment(
        workspace_id=workspace.id,
        patient_id=desired["patient_id"],
        branch_id=desired["branch_id"],
        doctor_id=desired["doctor_id"],
        service_id=desired["service_id"],
        status=desired["status"],
        source="other",
        start_at=desired["start_at"],
        end_at=desired["end_at"],
        busy_start_at=desired["busy_start_at"],
        busy_end_at=desired["busy_end_at"],
        duration_minutes=desired["duration_minutes"],
        price_minor=desired["price_minor"],
        currency=desired["currency"],
        payment_status="unpaid",
        amount_paid_minor=0,
        payment_method="unknown",
        idempotency_key=(
            "extsync:" + hashlib.sha256(f"{workspace.id}:appointment:{external_id}".encode()).hexdigest()[:56]
        ),
    )
    _set_appointment_status_timestamps(appointment, status=desired["status"], status_at=desired["status_at"])
    db.add(appointment)
    db.flush()
    add_appointment_history(
        db,
        appointment=appointment,
        changed_by_user_id=None,
        from_status=None,
        to_status=appointment.status,
        reason="external_appointment_sync_created",
        metadata={"sync_run_id": str(run_id)},
    )
    record_activity_event(
        db,
        workspace_id=workspace.id,
        actor_type="system",
        action="integration.appointment.created",
        entity_type="appointment",
        entity_id=appointment.id,
        summary="External appointment created",
        metadata={"sync_run_id": run_id, "domain": "appointments", "status": appointment.status},
    )
    return appointment


def _replace_external_appointment(
    db: Session,
    *,
    workspace: Workspace,
    current: Appointment,
    desired: dict,
    external_id: str,
    run_id: UUID,
) -> Appointment:
    if current.status not in _ACTIVE_EXTERNAL_APPOINTMENT_STATUSES or desired["status"] not in _ACTIVE_EXTERNAL_APPOINTMENT_STATUSES:
        raise ClinicSyncRecordError(
            "source_state_conflict",
            "External schedule changes are only supported while the appointment is pending or confirmed.",
            retryable=False,
        )
    old_status = current.status
    old_start_at = current.start_at
    replacement = Appointment(
        workspace_id=workspace.id,
        patient_id=desired["patient_id"],
        branch_id=desired["branch_id"],
        doctor_id=desired["doctor_id"],
        service_id=desired["service_id"],
        lead_id=current.lead_id,
        created_by_user_id=None,
        rescheduled_from_appointment_id=current.id,
        status=desired["status"],
        source=current.source,
        start_at=desired["start_at"],
        end_at=desired["end_at"],
        busy_start_at=desired["busy_start_at"],
        busy_end_at=desired["busy_end_at"],
        duration_minutes=desired["duration_minutes"],
        price_minor=desired["price_minor"],
        currency=desired["currency"],
        payment_status=current.payment_status,
        amount_paid_minor=current.amount_paid_minor,
        payment_method=current.payment_method,
        customer_note=current.customer_note,
        idempotency_key=(
            "extsync-reschedule:" + hashlib.sha256(
                f"{workspace.id}:{external_id}:{desired['start_at'].isoformat()}:{desired['doctor_id']}:{desired['service_id']}".encode()
            ).hexdigest()[:44]
        ),
    )
    _set_appointment_status_timestamps(replacement, status=desired["status"], status_at=desired["status_at"])
    current.status = "rescheduled"
    db.flush()
    db.add(replacement)
    db.flush()
    reallocate_appointment_payments_on_reschedule(
        db,
        workspace_id=workspace.id,
        from_appointment_id=current.id,
        to_appointment_id=replacement.id,
    )
    add_appointment_history(
        db,
        appointment=current,
        changed_by_user_id=None,
        from_status=old_status,
        to_status="rescheduled",
        reason="external_appointment_sync_rescheduled",
        metadata={
            "replacement_appointment_id": str(replacement.id),
            "old_start_at": old_start_at.isoformat(),
            "new_start_at": replacement.start_at.isoformat(),
            "sync_run_id": str(run_id),
        },
    )
    add_appointment_history(
        db,
        appointment=replacement,
        changed_by_user_id=None,
        from_status=None,
        to_status=replacement.status,
        reason="external_appointment_sync_replacement",
        metadata={"previous_appointment_id": str(current.id), "sync_run_id": str(run_id)},
    )
    cancel_pending_appointment_jobs(
        db,
        appointment=current,
        reason="external_appointment_rescheduled",
        now=datetime.now(UTC),
    )
    record_activity_event(
        db,
        workspace_id=workspace.id,
        actor_type="system",
        action="integration.appointment.rescheduled",
        entity_type="appointment",
        entity_id=current.id,
        summary="External appointment rescheduled",
        metadata={
            "sync_run_id": run_id,
            "domain": "appointments",
            "replacement_appointment_id": replacement.id,
        },
    )
    return replacement


def _sync_appointment(
    db: Session,
    *,
    workspace: Workspace,
    record: ExternalAppointmentSyncRecord,
    run_id: UUID,
) -> SyncOutcome:
    external_id = _clean_required(record.external_id, field="external_id", max_length=512)
    external_payload, desired, source_updated_at = _appointment_payload(db, workspace=workspace, record=record)
    fingerprint = _fingerprint(external_payload)
    desired_model_payload = _appointment_desired_model_payload(desired)
    desired_model_fingerprint = _fingerprint(desired_model_payload)

    link = _link_for_external(
        db,
        workspace_id=workspace.id,
        entity_type="appointment",
        external_id=external_id,
    )
    if link is None:
        appointment = _create_external_appointment(
            db,
            workspace=workspace,
            desired=desired,
            external_id=external_id,
            run_id=run_id,
        )
        _ensure_link(
            db,
            workspace_id=workspace.id,
            entity_type="appointment",
            canonical_id=appointment.id,
            external_id=external_id,
            metadata={
                "ownership": "external_sync",
                "sync_fingerprint": fingerprint,
                "applied_model_fingerprint": desired_model_fingerprint,
                "source_updated_at": source_updated_at,
            },
        )
        return "created"

    appointment = db.get(Appointment, _link_uuid(link, entity_type="appointment"))
    if appointment is None or appointment.workspace_id != workspace.id:
        raise ClinicSyncRecordError(
            "broken_entity_link",
            "External appointment link points to a missing Linka appointment.",
            retryable=False,
        )
    metadata = dict(link.metadata_json or {})
    previous_fingerprint = metadata.get("sync_fingerprint")
    previous_applied = metadata.get("applied_model_fingerprint")
    previous_updated_at = metadata.get("source_updated_at")
    if previous_fingerprint is None or previous_applied is None:
        raise ClinicSyncRecordError(
            "unmanaged_existing_appointment_link",
            "Existing appointment link has no external-sync provenance and requires review.",
            retryable=False,
        )

    current_model_fingerprint = _fingerprint(_appointment_model_payload(appointment))
    if current_model_fingerprint != previous_applied and current_model_fingerprint != desired_model_fingerprint:
        raise ClinicSyncRecordError(
            "source_authority_conflict",
            "Linka and the external clinic system changed the same authoritative appointment data.",
            retryable=False,
        )
    if source_updated_at and previous_updated_at:
        if source_updated_at < str(previous_updated_at):
            return "skipped"
        if source_updated_at == str(previous_updated_at) and previous_fingerprint != fingerprint:
            raise ClinicSyncRecordError(
                "source_version_conflict",
                "External appointment changed without a newer source timestamp.",
                retryable=False,
            )
    if previous_fingerprint == fingerprint and current_model_fingerprint == previous_applied:
        return "skipped"

    if appointment.patient_id != desired["patient_id"]:
        raise ClinicSyncRecordError(
            "appointment_patient_conflict",
            "Linked external appointment changed to another patient.",
            retryable=False,
        )

    schedule_changed = any(
        (
            appointment.branch_id != desired["branch_id"],
            appointment.service_id != desired["service_id"],
            appointment.doctor_id != desired["doctor_id"],
            _stored_utc(appointment.start_at) != desired["start_at"],
            _stored_utc(appointment.end_at) != desired["end_at"],
        )
    )
    if schedule_changed:
        replacement = _replace_external_appointment(
            db,
            workspace=workspace,
            current=appointment,
            desired=desired,
            external_id=external_id,
            run_id=run_id,
        )
        link.canonical_id = str(replacement.id)
        appointment = replacement
    else:
        _validate_external_status_transition(appointment.status, desired["status"])
        previous_status = appointment.status
        changed = False
        if appointment.status != desired["status"]:
            appointment.status = desired["status"]
            _set_appointment_status_timestamps(
                appointment,
                status=desired["status"],
                status_at=desired["status_at"],
            )
            add_appointment_history(
                db,
                appointment=appointment,
                changed_by_user_id=None,
                from_status=previous_status,
                to_status=appointment.status,
                reason="external_appointment_sync_status",
                metadata={"sync_run_id": str(run_id)},
            )
            if appointment.status in _TERMINAL_EXTERNAL_APPOINTMENT_STATUSES:
                cancel_pending_appointment_jobs(
                    db,
                    appointment=appointment,
                    reason=f"external_appointment_{appointment.status}",
                    now=datetime.now(UTC),
                )
            changed = True
        if appointment.price_minor != desired["price_minor"]:
            appointment.price_minor = desired["price_minor"]
            changed = True
        if appointment.currency != desired["currency"]:
            appointment.currency = desired["currency"]
            changed = True
        if changed:
            record_activity_event(
                db,
                workspace_id=workspace.id,
                actor_type="system",
                action="integration.appointment.updated",
                entity_type="appointment",
                entity_id=appointment.id,
                summary="External appointment updated",
                metadata={"sync_run_id": run_id, "domain": "appointments", "status": appointment.status},
            )

    db.flush()
    applied_fingerprint = _fingerprint(_appointment_model_payload(appointment))
    link.metadata_json = {
        "ownership": "external_sync",
        "sync_fingerprint": fingerprint,
        "applied_model_fingerprint": applied_fingerprint,
        "source_updated_at": source_updated_at,
    }
    return "updated" if applied_fingerprint != current_model_fingerprint else "skipped"


def _payment_fingerprint(record: ExternalPaymentSyncRecord) -> tuple[str, list[tuple[str, int]]]:
    external_id = _clean_required(record.external_id, field="external_id", max_length=512)
    del external_id
    patient_external_id = _clean_required(
        record.patient_external_id, field="patient_external_id", max_length=512
    )
    transaction_type = _clean_required(
        record.transaction_type, field="transaction_type", max_length=16
    ).lower()
    if transaction_type not in {"payment", "refund"}:
        raise ClinicSyncRecordError(
            "invalid_payment_type",
            "External payment transaction type is not canonical.",
            retryable=False,
        )
    amount_minor = _positive_int(record.amount_minor, field="amount_minor")
    currency = _clean_required(record.currency, field="currency", max_length=3).upper()
    if len(currency) != 3:
        raise ClinicSyncRecordError(
            "invalid_payment_currency",
            "External payment currency must be a three-letter code.",
            retryable=False,
        )
    method = _clean_required(record.payment_method, field="payment_method", max_length=24).lower()
    if method not in PAYMENT_METHODS:
        raise ClinicSyncRecordError(
            "invalid_payment_method",
            "External payment method is not canonical.",
            retryable=False,
        )
    created_at = _iso(record.created_at)
    external_reference = _optional_text(record.external_reference, max_length=128)
    reference_external = _optional_text(record.reference_payment_external_id, max_length=512)
    if transaction_type == "payment" and reference_external is not None:
        raise ClinicSyncRecordError(
            "invalid_refund_reference",
            "A payment cannot reference another payment transaction.",
            retryable=False,
        )
    if transaction_type == "refund" and reference_external is None:
        raise ClinicSyncRecordError(
            "invalid_refund_reference",
            "An external refund must reference the original payment.",
            retryable=False,
        )

    seen: set[str] = set()
    allocations: list[tuple[str, int]] = []
    for item in record.allocations:
        appointment_external_id = _clean_required(
            item.appointment_external_id,
            field="appointment_external_id",
            max_length=512,
        )
        if appointment_external_id in seen:
            raise ClinicSyncRecordError(
                "duplicate_payment_allocation",
                "External payment contains duplicate appointment allocations.",
                retryable=False,
            )
        seen.add(appointment_external_id)
        allocation_amount = _positive_int(
            item.amount_minor, field="allocation_amount_minor"
        )
        allocations.append((appointment_external_id, allocation_amount))
    try:
        validate_allocation_total(
            transaction_amount_minor=amount_minor,
            allocation_amounts=[amount for _, amount in allocations],
        )
    except ValueError as exc:
        raise ClinicSyncRecordError(
            "invalid_payment_allocation",
            "External payment allocations are inconsistent with the transaction amount.",
            retryable=False,
        ) from exc
    allocations.sort(key=lambda value: value[0])
    return (
        _fingerprint(
            {
                "patient_external_id": patient_external_id,
                "transaction_type": transaction_type,
                "amount_minor": amount_minor,
                "currency": currency,
                "payment_method": method,
                "created_at": created_at,
                "external_reference": external_reference,
                "reference_payment_external_id": reference_external,
                "allocations": allocations,
            }
        ),
        allocations,
    )


def _validate_refund_limits(
    db: Session,
    *,
    workspace_id: UUID,
    original: PaymentTransaction,
    refund_amount_minor: int,
    refund_allocations: list[tuple[UUID, int]],
) -> None:
    refunded_total = db.scalar(
        select(func.coalesce(func.sum(PaymentTransaction.amount_minor), 0)).where(
            PaymentTransaction.workspace_id == workspace_id,
            PaymentTransaction.transaction_type == "refund",
            PaymentTransaction.reference_transaction_id == original.id,
        )
    )
    if int(refunded_total or 0) + refund_amount_minor > int(original.amount_minor):
        raise ClinicSyncRecordError(
            "refund_exceeds_payment",
            "External refunds exceed the original payment amount.",
            retryable=False,
        )

    if not refund_allocations:
        return
    original_allocations = {
        appointment_id: int(amount_minor)
        for appointment_id, amount_minor in db.execute(
            select(PaymentAllocation.appointment_id, PaymentAllocation.amount_minor).where(
                PaymentAllocation.workspace_id == workspace_id,
                PaymentAllocation.transaction_id == original.id,
            )
        ).all()
    }
    for appointment_id, amount_minor in refund_allocations:
        original_amount = original_allocations.get(appointment_id)
        if original_amount is None:
            raise ClinicSyncRecordError(
                "refund_allocation_mismatch",
                "External refund allocation does not match the original payment allocation.",
                retryable=False,
            )
        already_refunded = db.scalar(
            select(func.coalesce(func.sum(PaymentAllocation.amount_minor), 0))
            .join(
                PaymentTransaction,
                (PaymentTransaction.workspace_id == PaymentAllocation.workspace_id)
                & (PaymentTransaction.id == PaymentAllocation.transaction_id),
            )
            .where(
                PaymentAllocation.workspace_id == workspace_id,
                PaymentAllocation.appointment_id == appointment_id,
                PaymentTransaction.transaction_type == "refund",
                PaymentTransaction.reference_transaction_id == original.id,
            )
        )
        if int(already_refunded or 0) + amount_minor > original_amount:
            raise ClinicSyncRecordError(
                "refund_allocation_exceeds_payment",
                "External refund allocation exceeds the original appointment allocation.",
                retryable=False,
            )


def _sync_payment(
    db: Session,
    *,
    workspace: Workspace,
    record: ExternalPaymentSyncRecord,
    run_id: UUID,
) -> SyncOutcome:
    external_id = _clean_required(record.external_id, field="external_id", max_length=512)
    fingerprint, allocation_facts = _payment_fingerprint(record)
    existing_link = _link_for_external(
        db,
        workspace_id=workspace.id,
        entity_type="payment",
        external_id=external_id,
    )
    if existing_link is not None:
        transaction = db.get(
            PaymentTransaction,
            _link_uuid(existing_link, entity_type="payment"),
        )
        if transaction is None or transaction.workspace_id != workspace.id:
            raise ClinicSyncRecordError(
                "broken_entity_link",
                "External payment link points to a missing Linka transaction.",
                retryable=False,
            )
        metadata = dict(existing_link.metadata_json or {})
        previous_fingerprint = metadata.get("sync_fingerprint")
        if previous_fingerprint == fingerprint:
            return "skipped"
        if previous_fingerprint is None:
            raise ClinicSyncRecordError(
                "unmanaged_existing_payment_link",
                "Existing payment link has no external-sync provenance and requires review.",
                retryable=False,
            )
        raise ClinicSyncRecordError(
            "immutable_financial_fact_changed",
            "External financial fact changed after it was synchronized.",
            retryable=False,
        )

    patient_external_id = _clean_required(
        record.patient_external_id, field="patient_external_id", max_length=512
    )
    transaction_type = record.transaction_type.strip().lower()
    currency = record.currency.strip().upper()
    payment_method = record.payment_method.strip().lower()
    patient_id = _resolve_linked_uuid(
        db,
        workspace_id=workspace.id,
        entity_type="patient",
        external_id=patient_external_id,
    )
    patient = db.get(Patient, patient_id)
    if patient is None or patient.workspace_id != workspace.id:
        raise ClinicSyncRecordError(
            "missing_dependency",
            "Linked external patient does not exist in this workspace.",
            retryable=True,
        )

    resolved_allocations: list[tuple[UUID, int]] = []
    for appointment_external_id, amount_minor in allocation_facts:
        appointment_id = _resolve_linked_uuid(
            db,
            workspace_id=workspace.id,
            entity_type="appointment",
            external_id=appointment_external_id,
        )
        appointment = db.get(Appointment, appointment_id)
        if appointment is None or appointment.workspace_id != workspace.id:
            raise ClinicSyncRecordError(
                "missing_dependency",
                "Linked external appointment does not exist in this workspace.",
                retryable=True,
            )
        if appointment.patient_id != patient.id:
            raise ClinicSyncRecordError(
                "payment_patient_mismatch",
                "External payment allocation belongs to another Linka patient.",
                retryable=False,
            )
        resolved_allocations.append((appointment.id, amount_minor))

    reference_transaction: PaymentTransaction | None = None
    if transaction_type == "refund":
        assert record.reference_payment_external_id is not None
        reference_id = _resolve_linked_uuid(
            db,
            workspace_id=workspace.id,
            entity_type="payment",
            external_id=record.reference_payment_external_id.strip(),
        )
        reference_transaction = db.get(PaymentTransaction, reference_id)
        if (
            reference_transaction is None
            or reference_transaction.workspace_id != workspace.id
            or reference_transaction.transaction_type != "payment"
        ):
            raise ClinicSyncRecordError(
                "invalid_refund_reference",
                "External refund does not reference a valid synchronized payment.",
                retryable=False,
            )
        if reference_transaction.patient_id != patient.id:
            raise ClinicSyncRecordError(
                "refund_patient_mismatch",
                "External refund and original payment belong to different patients.",
                retryable=False,
            )
        _validate_refund_limits(
            db,
            workspace_id=workspace.id,
            original=reference_transaction,
            refund_amount_minor=int(record.amount_minor),
            refund_allocations=resolved_allocations,
        )

    compatibility_appointment_id = None
    if len(resolved_allocations) == 1 and resolved_allocations[0][1] == int(record.amount_minor):
        compatibility_appointment_id = resolved_allocations[0][0]

    transaction = PaymentTransaction(
        workspace_id=workspace.id,
        appointment_id=compatibility_appointment_id,
        origin_appointment_id=compatibility_appointment_id,
        patient_id=patient.id,
        created_by_user_id=None,
        reference_transaction_id=(reference_transaction.id if reference_transaction else None),
        transaction_type=transaction_type,
        amount_minor=int(record.amount_minor),
        currency=currency,
        payment_method=payment_method,
        source="integration",
        external_reference=_optional_text(record.external_reference, max_length=128),
        reason=None,
        idempotency_key=(
            "extsync:"
            + hashlib.sha256(
                f"{workspace.id}:payment:{external_id}".encode()
            ).hexdigest()[:56]
        ),
        created_at=record.created_at,
    )
    db.add(transaction)
    db.flush()
    for appointment_id, amount_minor in resolved_allocations:
        db.add(
            PaymentAllocation(
                workspace_id=workspace.id,
                transaction_id=transaction.id,
                appointment_id=appointment_id,
                amount_minor=amount_minor,
                created_at=record.created_at,
            )
        )
    db.flush()
    refresh_appointment_payment_snapshots(
        db,
        workspace_id=workspace.id,
        appointment_ids={appointment_id for appointment_id, _ in resolved_allocations},
    )
    _ensure_link(
        db,
        workspace_id=workspace.id,
        entity_type="payment",
        canonical_id=transaction.id,
        external_id=external_id,
        metadata={
            "ownership": "external_sync",
            "sync_fingerprint": fingerprint,
        },
    )
    record_activity_event(
        db,
        workspace_id=workspace.id,
        actor_type="system",
        action="integration.payment.created",
        entity_type="payment_transaction",
        entity_id=transaction.id,
        summary="External payment synchronized",
        metadata={
            "sync_run_id": run_id,
            "domain": "payments",
            "transaction_type": transaction.transaction_type,
            "amount_minor": transaction.amount_minor,
            "currency": transaction.currency,
            "allocation_count": len(resolved_allocations),
        },
    )
    return "created"


def _record_failure(
    db: Session,
    *,
    workspace_id: UUID,
    run_id: UUID,
    domain: ClinicSyncDomain,
    external_id: str,
    exc: Exception,
) -> None:
    if isinstance(exc, ClinicSyncRecordError):
        code = exc.code
        message = exc.safe_message
        retryable = exc.retryable
    elif isinstance(exc, IntegrityError):
        code = "database_conflict"
        message = "External record conflicts with existing workspace data."
        retryable = True
    else:
        code = "sync_record_error"
        message = "External record could not be synchronized."
        retryable = True
    db.add(
        ClinicIntegrationSyncFailure(
            workspace_id=workspace_id,
            run_id=run_id,
            domain=domain.value,
            external_id_digest=_external_id_digest(external_id or "missing"),
            error_code=code,
            message=message,
            retryable=retryable,
        )
    )


def _get_checkpoint(
    db: Session,
    *,
    workspace_id: UUID,
    domain: ClinicSyncDomain,
) -> ClinicIntegrationSyncCheckpoint | None:
    return db.get(ClinicIntegrationSyncCheckpoint, (workspace_id, domain.value))


def apply_external_sync_page(
    *,
    db: Session,
    workspace: Workspace,
    page: ClinicSyncPage,
    now: datetime | None = None,
) -> ClinicSyncRunResult:
    """Apply one canonical external page with savepoint-level failure isolation.

    The caller owns the outer transaction/commit. Successful records survive
    sibling record failures inside that transaction. The durable checkpoint only
    advances when the whole page succeeds, making page replay safe and lossless.
    """

    if not isinstance(page.domain, ClinicSyncDomain):
        raise ClinicIntegrationSyncError("Sync page domain is not supported.")

    integration = db.scalar(
        select(ClinicIntegration)
        .where(ClinicIntegration.workspace_id == workspace.id)
        .with_for_update()
    )
    if integration is None:
        raise ClinicIntegrationSyncError(
            "Clinic integration configuration is missing. Run database migrations first."
        )
    if integration.status != "active":
        raise ClinicIntegrationSyncError("Clinic integration must be active before sync.")
    if integration.mode not in {"external_api", "hybrid", "imported"}:
        raise ClinicIntegrationSyncError(
            "External sync requires external_api, hybrid, or imported integration mode."
        )
    try:
        require_external_domain_authority(integration, page.domain.value)
    except ClinicIntegrationAuthorityError as exc:
        raise ClinicIntegrationSyncError(str(exc)) from exc

    checkpoint = _get_checkpoint(
        db,
        workspace_id=workspace.id,
        domain=page.domain,
    )
    page_cursor = _cursor_value(page.cursor, field="cursor")
    next_cursor = _cursor_value(page.next_cursor, field="next_cursor")
    expected_cursor = checkpoint.cursor if checkpoint is not None else None
    if page_cursor != expected_cursor:
        raise ClinicIntegrationSyncError(
            "Sync page cursor does not match the durable checkpoint. Refetch from the saved cursor."
        )

    expected_types = {
        ClinicSyncDomain.PATIENTS: ExternalPatientSyncRecord,
        ClinicSyncDomain.PAYMENTS: ExternalPaymentSyncRecord,
        ClinicSyncDomain.APPOINTMENTS: ExternalAppointmentSyncRecord,
    }
    expected_type = expected_types[page.domain]
    if any(not isinstance(record, expected_type) for record in page.records):
        raise ClinicIntegrationSyncError(
            f"Sync page for {page.domain.value} contains a record from another domain."
        )

    run = ClinicIntegrationSyncRun(
        workspace_id=workspace.id,
        domain=page.domain.value,
        status="running",
        cursor_before=page_cursor,
        cursor_after=page_cursor,
        source_revision=_optional_text(page.source_revision, max_length=255),
    )
    db.add(run)
    db.flush()

    created = updated = skipped = failed = 0
    for record in page.records:
        external_id = _external_id(record)
        try:
            with db.begin_nested():
                if page.domain == ClinicSyncDomain.PATIENTS:
                    outcome = _sync_patient(
                        db,
                        workspace=workspace,
                        integration=integration,
                        record=record,
                        run_id=run.id,
                    )
                elif page.domain == ClinicSyncDomain.PAYMENTS:
                    outcome = _sync_payment(
                        db,
                        workspace=workspace,
                        record=record,
                        run_id=run.id,
                    )
                else:
                    outcome = _sync_appointment(
                        db,
                        workspace=workspace,
                        record=record,
                        run_id=run.id,
                    )
                db.flush()
        except (ClinicSyncRecordError, IntegrityError) as exc:
            failed += 1
            _record_failure(
                db,
                workspace_id=workspace.id,
                run_id=run.id,
                domain=page.domain,
                external_id=external_id,
                exc=exc,
            )
            db.flush()
            continue
        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
        else:
            skipped += 1

    completed_at = (now or datetime.now(UTC)).astimezone(UTC)
    run.processed_count = len(page.records)
    run.created_count = created
    run.updated_count = updated
    run.skipped_count = skipped
    run.failed_count = failed
    run.completed_at = completed_at

    if failed:
        run.status = "failed" if failed == len(page.records) and page.records else "partial"
        run.cursor_after = expected_cursor
    else:
        run.status = "succeeded"
        run.cursor_after = next_cursor
        if checkpoint is None:
            checkpoint = ClinicIntegrationSyncCheckpoint(
                workspace_id=workspace.id,
                domain=page.domain.value,
            )
            db.add(checkpoint)
        checkpoint.cursor = next_cursor
        checkpoint.source_revision = run.source_revision
        checkpoint.last_success_at = completed_at
        checkpoint.last_run_id = run.id

    db.flush()
    record_activity_event(
        db,
        workspace_id=workspace.id,
        actor_type="system",
        action="integration.sync.completed",
        entity_type="clinic_integration",
        summary="External clinic sync page completed",
        metadata={
            "sync_run_id": run.id,
            "domain": page.domain.value,
            "status": run.status,
            "processed_count": run.processed_count,
            "created_count": created,
            "updated_count": updated,
            "skipped_count": skipped,
            "failed_count": failed,
            "checkpoint_advanced": failed == 0,
        },
    )
    db.flush()
    return ClinicSyncRunResult(
        run_id=run.id,
        domain=page.domain,
        status=run.status,
        cursor_before=run.cursor_before,
        cursor_after=run.cursor_after,
        processed_count=run.processed_count,
        created_count=created,
        updated_count=updated,
        skipped_count=skipped,
        failed_count=failed,
    )
