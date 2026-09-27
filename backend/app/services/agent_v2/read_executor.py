from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.integrations.clinic.base import (
    AppointmentReadRequest,
    AppointmentRecord,
    AvailabilityRequest,
    AvailabilitySlot,
    ClinicAdapter,
    ClinicCapability,
)
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.patient import Patient
from app.models.workspace import Workspace
from app.services.agent_v2.branch_resolution import resolve_single_location_branch_id
from app.services.agent_v2.planner import PlanStep, ReadKind, ReadRequest, VerificationFacts
from app.services.booking import BookingCompatibilityError, BookingRuleError
from app.services.clinic_knowledge_base import relevant_knowledge_context
from app.services.package_offers import list_package_offers
from app.services.package_refund_quotes import list_patient_package_refund_quotes
from app.services.patient_history import build_patient_history_context
from app.services.patient_packages import list_patient_packages
from app.services.pulse_billing import (
    list_patient_pulse_balances,
    list_patient_pulse_packs,
    list_pulse_billing_settings,
    list_pulse_pack_offers,
)

_MAX_AVAILABILITY_DAYS = 14
_ACTIONABLE_APPOINTMENT_STATUSES = frozenset({"pending", "confirmed"})


class ReadExecutionError(RuntimeError):
    pass


class StrictReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadResult(StrictReadModel):
    kind: ReadKind
    ok: bool
    payload: dict[str, object] = Field(default_factory=dict)
    error_code: str | None = None


class ReadExecutionBundle(StrictReadModel):
    results: list[ReadResult] = Field(default_factory=list)
    verification: VerificationFacts = Field(default_factory=VerificationFacts)


@dataclass(frozen=True)
class ReadExecutionContext:
    db: Session
    workspace: Workspace
    patient: Patient
    now: datetime
    catalog: dict[str, Any] | None = None
    adapter: ClinicAdapter | None = None


def _catalog(context: ReadExecutionContext) -> dict[str, Any]:
    return context.catalog or build_clinic_catalog(context.db, context.workspace)


def _adapter(context: ReadExecutionContext) -> ClinicAdapter:
    return context.adapter or get_clinic_adapter(db=context.db, workspace=context.workspace)


def _catalog_rows(catalog: dict[str, Any], collection: str) -> list[dict[str, Any]]:
    rows = catalog.get(collection)
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _catalog_row(
    catalog: dict[str, Any],
    collection: str,
    canonical_id: object,
) -> dict[str, Any] | None:
    target = str(canonical_id)
    for row in _catalog_rows(catalog, collection):
        row_id = row.get("id") or row.get(f"{collection.rstrip('s')}_id")
        if row_id is not None and str(row_id) == target:
            return row
    return None


def _availability_compatibility_failure(
    error: BookingCompatibilityError,
    *,
    params: dict[str, object],
    context: ReadExecutionContext,
) -> ReadResult:
    """Expose a typed canonical compatibility rejection without re-deriving the rule."""
    catalog = _catalog(context)
    service = _catalog_row(catalog, "services", params.get("service_id"))
    service_name = (
        str(service.get("name") or service.get("service_name"))
        if service is not None and (service.get("name") or service.get("service_name"))
        else "الخدمة"
    )

    if error.dimension == "doctor":
        requested = _catalog_row(catalog, "doctors", params.get("doctor_id"))
        if requested is None:
            raise error
        requested_name = (
            str(requested.get("name") or requested.get("doctor_name"))
            if requested.get("name") or requested.get("doctor_name")
            else None
        )
    elif error.dimension == "device":
        device_key = str(params.get("device_key") or "")
        requested = next(
            (
                device
                for row in _catalog_rows(catalog, "services")
                for device in (row.get("laser_devices") or [])
                if isinstance(device, dict)
                and str(device.get("device_key")) == device_key
            ),
            None,
        )
        if requested is None:
            raise error
        requested_name = (
            str(requested.get("device_name"))
            if requested.get("device_name")
            else device_key
        )
    else:
        raise error

    return ReadResult(
        kind="availability",
        ok=False,
        error_code=f"{error.dimension}_service_incompatible",
        payload={
            "compatibility_failure": {
                "dimension": error.dimension,
                "service_name": service_name,
                "requested_name": requested_name,
                "compatible_options": [],
            }
        },
    )


def _explanatory_knowledge(context: ReadExecutionContext) -> str | None:
    """Read only the clinic-wide saved "معلومات Tia" customer knowledge text."""
    payload = relevant_knowledge_context(
        context.db,
        workspace_id=context.workspace.id,
        include_clinic=True,
    )
    if not isinstance(payload, dict):
        return None
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None
    parts = [
        str(entry.get("content") or "").strip()
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("content") or "").strip()
    ]
    if not parts:
        return None
    return "\n\n".join(parts)[:6000]


def _single_location_branch_id(context: ReadExecutionContext) -> str:
    primary_branch_id = context.workspace.primary_branch_id
    branch_id = resolve_single_location_branch_id(
        primary_branch_id=primary_branch_id,
        catalog={} if primary_branch_id is not None else _catalog(context),
    )
    if branch_id is not None:
        return branch_id
    raise ReadExecutionError("Single-location clinic branch could not be resolved deterministically.")


def _parse_uuid(value: object, *, field_name: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ReadExecutionError(f"{field_name} must be a canonical UUID for this read source.") from exc


def _constraint_dates(
    raw: object,
    *,
    now_date: date,
) -> tuple[list[date], bool, bool]:
    if not isinstance(raw, dict):
        return [], False, False
    mode = str(raw.get("mode") or "")
    start_raw = raw.get("start_date")
    end_raw = raw.get("end_date")

    if mode == "exact" and start_raw:
        return [date.fromisoformat(str(start_raw))], False, False
    if mode == "range" and start_raw and end_raw:
        start = date.fromisoformat(str(start_raw))
        end = date.fromisoformat(str(end_raw))
        if end < start:
            raise ReadExecutionError("Availability date range is reversed.")
        requested_days = (end - start).days + 1
        count = min(requested_days, _MAX_AVAILABILITY_DAYS)
        return [start + timedelta(days=index) for index in range(count)], requested_days > count, False
    if mode == "from_date" and start_raw:
        start = date.fromisoformat(str(start_raw))
        return (
            [start + timedelta(days=index) for index in range(_MAX_AVAILABILITY_DAYS)],
            True,
            True,
        )
    if mode == "next_available":
        return (
            [now_date + timedelta(days=index) for index in range(_MAX_AVAILABILITY_DAYS)],
            True,
            True,
        )
    return [], False, False


def _time_constraint(raw: object) -> tuple[str | None, time | None, time | None]:
    if not isinstance(raw, dict):
        return None, None, None
    mode = str(raw.get("mode") or "")
    start_raw = raw.get("start_time")
    end_raw = raw.get("end_time")
    start = time.fromisoformat(str(start_raw)) if start_raw else None
    end = time.fromisoformat(str(end_raw)) if end_raw else None
    return mode or None, start, end


def _slot_matches_time(slot: AvailabilitySlot, *, timezone_name: str, constraint: object) -> bool:
    mode, start, end = _time_constraint(constraint)
    if mode is None or mode == "nearest":
        return True
    tz = ZoneInfo(timezone_name)
    local_start = slot.start_at.astimezone(tz).timetz().replace(tzinfo=None)
    local_end = slot.end_at.astimezone(tz).timetz().replace(tzinfo=None)
    if mode == "exact":
        return start is not None and local_start == start
    if mode == "after":
        return start is not None and local_start >= start
    if mode == "before":
        return start is not None and local_end <= start
    if mode == "range":
        return start is not None and end is not None and local_start >= start and local_end <= end
    return False


def _minutes_of_day(value: time) -> int:
    return value.hour * 60 + value.minute


def _nearest_payloads(
    slots: list[dict[str, object]],
    *,
    anchor: time | None,
) -> list[dict[str, object]]:
    if not slots:
        return []
    parsed = [(slot, datetime.fromisoformat(str(slot["start_local"]))) for slot in slots]
    earliest_date = min(local.date() for _, local in parsed)
    same_date = [(slot, local) for slot, local in parsed if local.date() == earliest_date]
    if anchor is None:
        earliest = min(local.time() for _, local in same_date)
        return [slot for slot, local in same_date if local.time() == earliest]
    anchor_minutes = _minutes_of_day(anchor)
    distances = [
        abs(_minutes_of_day(local.time()) - anchor_minutes)
        for _, local in same_date
    ]
    minimum = min(distances)
    return [
        slot
        for (slot, _local), distance in zip(same_date, distances, strict=True)
        if distance == minimum
    ]


def _slot_payload(slot: AvailabilitySlot, *, timezone_name: str) -> dict[str, object]:
    tz = ZoneInfo(timezone_name)
    local_start = slot.start_at.astimezone(tz)
    local_end = slot.end_at.astimezone(tz)
    return {
        "branch_id": slot.branch_id,
        "branch_name": slot.branch_name,
        "service_id": slot.service_id,
        "service_name": slot.service_name,
        "doctor_id": slot.doctor_id,
        "doctor_name": slot.doctor_name,
        "start_at": slot.start_at.isoformat(),
        "end_at": slot.end_at.isoformat(),
        "start_local": local_start.isoformat(),
        "end_local": local_end.isoformat(),
        "start_time_24h": local_start.strftime("%H:%M"),
        "end_time_24h": local_end.strftime("%H:%M"),
        "duration_minutes": slot.duration_minutes,
        "price_minor": int(slot.price_minor),
        "currency": slot.currency,
        "laser_device_key": slot.laser_device_key,
        "laser_device_name": slot.laser_device_name,
    }


def _appointment_payload(row: AppointmentRecord) -> dict[str, object]:
    try:
        tz = ZoneInfo(row.timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    return {
        "appointment_id": row.appointment_id,
        "visit_group_id": row.visit_group_id,
        "status": row.status,
        "service_id": row.service_id,
        "service_name": row.service_name,
        "branch_id": row.branch_id,
        "branch_name": row.branch_name,
        "doctor_id": row.doctor_id,
        "doctor_name": row.doctor_name,
        "start_at": row.start_at.isoformat(),
        "end_at": row.end_at.isoformat(),
        "start_local": row.start_at.astimezone(tz).isoformat(),
        "end_local": row.end_at.astimezone(tz).isoformat(),
        "timezone": row.timezone,
        "price_minor": int(row.price_minor),
        "currency": row.currency,
        "payment_status": row.payment_status,
        "amount_paid_minor": row.amount_paid_minor,
        "payment_method": row.payment_method,
        "billing_context": row.billing_context,
        "patient_package_id": row.patient_package_id,
        "package_external_id": row.package_external_id,
        "laser_device_key": row.laser_device_key,
        "laser_device_name": row.laser_device_name,
    }


def _group_appointment_rows(
    rows: list[AppointmentRecord],
) -> list[list[AppointmentRecord]]:
    groups: dict[str, list[AppointmentRecord]] = {}
    for row in rows:
        key = (
            f"visit:{row.visit_group_id}"
            if row.visit_group_id
            else f"appointment:{row.appointment_id}"
        )
        groups.setdefault(key, []).append(row)
    return [
        sorted(group, key=lambda item: (item.start_at, item.appointment_id))
        for group in groups.values()
    ]


def _common(values: list[str | None]) -> str | None:
    unique = {value for value in values if value is not None}
    return next(iter(unique)) if len(unique) == 1 else None


def _visit_payload(rows: list[AppointmentRecord]) -> dict[str, object]:
    ordered = sorted(rows, key=lambda item: (item.start_at, item.appointment_id))
    statuses = {row.status for row in ordered}
    currencies = {row.currency for row in ordered}
    return {
        "visit_group_id": _common([row.visit_group_id for row in ordered]),
        "appointment_ids": [row.appointment_id for row in ordered],
        "status": next(iter(statuses)) if len(statuses) == 1 else "mixed",
        "start_at": min(row.start_at for row in ordered).isoformat(),
        "end_at": max(row.end_at for row in ordered).isoformat(),
        "branch_id": _common([row.branch_id for row in ordered]),
        "branch_name": _common([row.branch_name for row in ordered]),
        "doctor_id": _common([row.doctor_id for row in ordered]),
        "doctor_name": _common([row.doctor_name for row in ordered]),
        "currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "price_minor": sum(int(row.price_minor) for row in ordered),
        "services": [
            {
                "appointment_id": row.appointment_id,
                "service_id": row.service_id,
                "service_name": row.service_name,
                "start_at": row.start_at.isoformat(),
                "end_at": row.end_at.isoformat(),
                "laser_device_key": row.laser_device_key,
                "laser_device_name": row.laser_device_name,
            }
            for row in ordered
        ],
    }


def _appointment_matches_date(
    row: AppointmentRecord,
    raw: object,
    *,
    now: datetime,
) -> bool:
    if not isinstance(raw, dict):
        return True
    tz = ZoneInfo(row.timezone)
    dates, _truncated, _first_only = _constraint_dates(
        raw,
        now_date=now.astimezone(tz).date(),
    )
    if not dates:
        return True
    return row.start_at.astimezone(tz).date() in set(dates)


def _appointment_matches_time(
    row: AppointmentRecord,
    raw: object,
) -> bool:
    mode, start, end = _time_constraint(raw)
    if mode is None or mode == "nearest":
        return True
    tz = ZoneInfo(row.timezone)
    local_start = row.start_at.astimezone(tz).timetz().replace(tzinfo=None)
    local_end = row.end_at.astimezone(tz).timetz().replace(tzinfo=None)
    if mode == "exact":
        return start is not None and local_start == start
    if mode == "after":
        return start is not None and local_start >= start
    if mode == "before":
        return start is not None and local_end <= start
    if mode == "range":
        return start is not None and end is not None and local_start >= start and local_end <= end
    return False


def _read_service_catalog(
    request: ReadRequest,
    context: ReadExecutionContext,
    *,
    include_explanation: bool = False,
) -> ReadResult:
    service_id = request.parameters.get("service_id")
    if service_id is None:
        services = [
            {"name": row.get("name")}
            for row in _catalog_rows(_catalog(context), "services")
            if row.get("name") not in (None, "")
        ]
        return ReadResult(
            kind=request.kind,
            ok=True,
            payload={"services": services},
        )

    row = _catalog_row(_catalog(context), "services", service_id)
    if row is None:
        return ReadResult(
            kind=request.kind,
            ok=False,
            payload={},
            error_code="service_not_found",
        )

    service = dict(row)
    service.pop("description", None)
    if include_explanation:
        knowledge = _explanatory_knowledge(context)
        if knowledge:
            service["description"] = knowledge
    return ReadResult(kind=request.kind, ok=True, payload={"service": service})


def _read_clinic_info(request: ReadRequest, context: ReadExecutionContext) -> ReadResult:
    branches = _catalog_rows(_catalog(context), "branches")
    primary = str(context.workspace.primary_branch_id) if context.workspace.primary_branch_id else None
    visible = []
    for branch in branches:
        if primary is not None and str(branch.get("id")) != primary:
            continue
        visible.append(
            {
                key: branch.get(key)
                for key in (
                    "name",
                    "phone",
                    "email",
                    "address",
                    "address_line1",
                    "address_line2",
                    "city",
                    "state",
                    "country_code",
                    "timezone",
                )
                if branch.get(key) not in (None, "")
            }
        )
    payload: dict[str, object] = {
        "clinic_name": context.workspace.name,
        "timezone": context.workspace.timezone,
        "locations": visible[:1],
    }
    knowledge = _explanatory_knowledge(context)
    if knowledge:
        payload["knowledge"] = knowledge
    return ReadResult(kind=request.kind, ok=True, payload=payload)


def _read_doctors(request: ReadRequest, context: ReadExecutionContext) -> ReadResult:
    service_id = request.parameters.get("service_id")
    doctor_id = request.parameters.get("doctor_id")
    raw_doctor_ids = request.parameters.get("doctor_ids")
    doctor_ids = (
        {str(item) for item in raw_doctor_ids}
        if isinstance(raw_doctor_ids, list)
        else set()
    )
    filtered = []
    for row in _catalog_rows(_catalog(context), "doctors"):
        row_id = str(row.get("id"))
        if doctor_id is not None and row_id != str(doctor_id):
            continue
        if doctor_ids and row_id not in doctor_ids:
            continue
        if service_id is not None:
            service_ids = row.get("service_ids")
            if not isinstance(service_ids, list) or str(service_id) not in {
                str(item) for item in service_ids
            }:
                continue
        filtered.append(row)
    return ReadResult(kind=request.kind, ok=True, payload={"doctors": filtered})


def _with_verified_compatibility_options(
    result: ReadResult,
    *,
    params: dict[str, object],
    context: ReadExecutionContext,
    adapter: ClinicAdapter,
    branch_id: str,
    booking_date: date,
    appointment_id: str | None,
) -> ReadResult:
    """Offer only alternatives returned by the canonical availability engine."""
    failure = result.payload.get("compatibility_failure")
    if not isinstance(failure, dict):
        return result
    dimension = failure.get("dimension")
    if dimension not in {"doctor", "device"}:
        return result

    try:
        availability = adapter.get_availability(
            AvailabilityRequest(
                branch_id=branch_id,
                service_id=str(params["service_id"]),
                booking_date=booking_date,
                doctor_id=(
                    None
                    if dimension == "doctor"
                    else str(params["doctor_id"])
                    if params.get("doctor_id")
                    else None
                ),
                exclude_appointment_id=(
                    appointment_id if params.get("reschedule") else None
                ),
                now=context.now,
                laser_device_key=(
                    None
                    if dimension == "device"
                    else str(params["device_key"])
                    if params.get("device_key")
                    else None
                ),
            )
        )
    except BookingRuleError:
        return result

    labels: list[str] = []
    for slot in availability.slots:
        if not _slot_matches_time(
            slot,
            timezone_name=availability.timezone,
            constraint=params.get("time"),
        ):
            continue
        label = slot.doctor_name if dimension == "doctor" else slot.laser_device_name
        if label and label not in labels:
            labels.append(label)

    payload = dict(result.payload)
    payload["compatibility_failure"] = {
        **failure,
        "compatible_options": labels,
    }
    return result.model_copy(update={"payload": payload})


def _read_availability(
    request: ReadRequest,
    context: ReadExecutionContext,
    *,
    inherited: dict[str, object] | None = None,
) -> tuple[ReadResult, VerificationFacts]:
    params = {**dict(inherited or {}), **request.parameters}
    service_id = params.get("service_id")
    if service_id is None:
        return ReadResult(kind=request.kind, ok=False, error_code="missing_service"), VerificationFacts()

    date_values, truncated, stop_on_first = _constraint_dates(
        params.get("date"),
        now_date=context.now.astimezone(ZoneInfo(context.workspace.timezone)).date(),
    )
    if not date_values:
        return ReadResult(kind=request.kind, ok=False, error_code="missing_date"), VerificationFacts()

    branch_id = str(params.get("branch_id") or _single_location_branch_id(context))
    doctor_id = str(params["doctor_id"]) if params.get("doctor_id") else None
    raw_doctor_ids = params.get("doctor_ids")
    requested_doctors = (
        [str(item) for item in raw_doctor_ids]
        if isinstance(raw_doctor_ids, list) and raw_doctor_ids
        else ([doctor_id] if doctor_id is not None else [None])
    )
    appointment_id = str(params["appointment_id"]) if params.get("appointment_id") else None
    device_key = str(params["device_key"]) if params.get("device_key") else None
    adapter = _adapter(context)
    adapter.require_capability(ClinicCapability.AVAILABILITY_READ)

    slots: list[dict[str, object]] = []
    checked_dates: list[str] = []
    service_meta: dict[str, object] = {}
    for booking_date in date_values:
        checked_dates.append(booking_date.isoformat())
        date_matches: list[dict[str, object]] = []
        for requested_doctor in requested_doctors:
            try:
                availability = adapter.get_availability(
                    AvailabilityRequest(
                        branch_id=branch_id,
                        service_id=str(service_id),
                        booking_date=booking_date,
                        doctor_id=requested_doctor,
                        exclude_appointment_id=appointment_id if params.get("reschedule") else None,
                        now=context.now,
                        laser_device_key=device_key,
                    )
                )
            except BookingCompatibilityError as exc:
                failure = _availability_compatibility_failure(
                    exc,
                    params={
                        **params,
                        "doctor_id": requested_doctor,
                        "device_key": device_key,
                    },
                    context=context,
                )
                failure = _with_verified_compatibility_options(
                    failure,
                    params={
                        **params,
                        "doctor_id": requested_doctor,
                        "device_key": device_key,
                    },
                    context=context,
                    adapter=adapter,
                    branch_id=branch_id,
                    booking_date=booking_date,
                    appointment_id=appointment_id,
                )
                return failure, VerificationFacts()
            if not service_meta:
                service_meta = {
                    "service_id": availability.service_id,
                    "service_name": availability.service_name,
                    "service_duration_minutes": availability.service_duration_minutes,
                    "service_price_minor": availability.service_price_minor,
                    "service_currency": availability.service_currency,
                    "branch_id": availability.branch_id,
                    "branch_name": availability.branch_name,
                    "timezone": availability.timezone,
                }
            matches = [
                slot
                for slot in availability.slots
                if _slot_matches_time(
                    slot,
                    timezone_name=availability.timezone,
                    constraint=params.get("time"),
                )
            ]
            date_matches.extend(
                _slot_payload(slot, timezone_name=availability.timezone) for slot in matches
            )
        # A set query can produce duplicates if an adapter ignores its doctor filter.
        unique: dict[tuple[str, str, str], dict[str, object]] = {}
        for slot in date_matches:
            key = (
                str(slot.get("doctor_id") or ""),
                str(slot.get("start_at") or ""),
                str(slot.get("laser_device_key") or ""),
            )
            unique[key] = slot
        date_matches = list(unique.values())
        slots.extend(date_matches)
        if date_matches and stop_on_first:
            break

    mode, start, _end = _time_constraint(params.get("time"))
    if mode == "nearest":
        slots = _nearest_payloads(slots, anchor=start)
    exact_count = len(slots) if mode == "exact" else None
    verified: dict[str, object] = {}
    if exact_count == 1:
        slot = slots[0]
        verified = {
            "branch_id": slot["branch_id"],
            "service_id": slot["service_id"],
            "doctor_id": slot["doctor_id"],
            "start_at": slot["start_at"],
        }
        if slot.get("laser_device_key"):
            verified["device_key"] = slot["laser_device_key"]
        if appointment_id is not None:
            verified["appointment_id"] = appointment_id

    return (
        ReadResult(
            kind=request.kind,
            ok=True,
            payload={
                **service_meta,
                "checked_dates": checked_dates,
                "slots": slots,
                "matching_slot_count": len(slots),
                "search_truncated": truncated,
            },
        ),
        VerificationFacts(exact_slot_match_count=exact_count, verified_parameters=verified),
    )


def _read_group_reschedule_availability(
    request: ReadRequest,
    context: ReadExecutionContext,
    *,
    appointments: list[AppointmentRecord],
) -> tuple[ReadResult, VerificationFacts]:
    ordered = sorted(appointments, key=lambda item: (item.start_at, item.appointment_id))
    anchor = ordered[0]
    if len({row.branch_id for row in ordered}) != 1 or len({row.doctor_id for row in ordered}) != 1:
        return (
            ReadResult(kind=request.kind, ok=False, error_code="invalid_visit_group"),
            VerificationFacts(exact_slot_match_count=0),
        )

    params = dict(request.parameters)
    date_values, truncated, stop_on_first = _constraint_dates(
        params.get("date"),
        now_date=context.now.astimezone(ZoneInfo(context.workspace.timezone)).date(),
    )
    if not date_values:
        return (
            ReadResult(kind=request.kind, ok=False, error_code="missing_date"),
            VerificationFacts(),
        )

    target_branch_id = str(params.get("branch_id") or anchor.branch_id)
    target_doctor_id = str(params.get("doctor_id") or anchor.doctor_id)
    excluded_ids = tuple(row.appointment_id for row in ordered)
    adapter = _adapter(context)
    adapter.require_capability(ClinicCapability.AVAILABILITY_READ)
    slots: list[dict[str, object]] = []
    checked_dates: list[str] = []
    service_meta: dict[str, object] = {}

    for booking_date in date_values:
        checked_dates.append(booking_date.isoformat())
        anchor_availability = adapter.get_availability(
            AvailabilityRequest(
                branch_id=target_branch_id,
                service_id=anchor.service_id,
                booking_date=booking_date,
                doctor_id=target_doctor_id,
                exclude_appointment_ids=excluded_ids,
                now=context.now,
                laser_device_key=anchor.laser_device_key,
            )
        )
        if not service_meta:
            service_meta = {
                "service_id": anchor_availability.service_id,
                "service_name": anchor_availability.service_name,
                "branch_id": anchor_availability.branch_id,
                "branch_name": anchor_availability.branch_name,
                "timezone": anchor_availability.timezone,
            }
        anchor_matches = [
            slot
            for slot in anchor_availability.slots
            if _slot_matches_time(
                slot,
                timezone_name=anchor_availability.timezone,
                constraint=params.get("time"),
            )
        ]
        date_matches: list[dict[str, object]] = []
        for anchor_slot in anchor_matches:
            delta = anchor_slot.start_at - anchor.start_at
            component_targets: list[dict[str, object]] = []
            group_available = True
            for row in ordered:
                target_start = row.start_at + delta
                if row.appointment_id == anchor.appointment_id:
                    matched_slot = anchor_slot
                else:
                    target_date = target_start.astimezone(
                        ZoneInfo(anchor_availability.timezone)
                    ).date()
                    component_availability = adapter.get_availability(
                        AvailabilityRequest(
                            branch_id=target_branch_id,
                            service_id=row.service_id,
                            booking_date=target_date,
                            doctor_id=anchor_slot.doctor_id,
                            exclude_appointment_ids=excluded_ids,
                            now=context.now,
                            laser_device_key=row.laser_device_key,
                        )
                    )
                    matched_slot = next(
                        (
                            slot
                            for slot in component_availability.slots
                            if slot.start_at == target_start
                        ),
                        None,
                    )
                    if matched_slot is None:
                        group_available = False
                        break
                component_target: dict[str, object] = {
                    "appointment_id": row.appointment_id,
                    "branch_id": matched_slot.branch_id,
                    "service_id": matched_slot.service_id,
                    "doctor_id": matched_slot.doctor_id,
                    "start_at": matched_slot.start_at.isoformat(),
                }
                if matched_slot.laser_device_key:
                    component_target["device_key"] = matched_slot.laser_device_key
                component_targets.append(component_target)
            if group_available:
                payload = _slot_payload(
                    anchor_slot,
                    timezone_name=anchor_availability.timezone,
                )
                payload.update(
                    {
                        "visit_group_id": anchor.visit_group_id,
                        "appointment_ids": list(excluded_ids),
                        "reschedule_components": component_targets,
                    }
                )
                date_matches.append(payload)

        unique: dict[tuple[str, str], dict[str, object]] = {}
        for slot in date_matches:
            key = (str(slot.get("doctor_id") or ""), str(slot.get("start_at") or ""))
            unique[key] = slot
        date_matches = list(unique.values())
        slots.extend(date_matches)
        if date_matches and stop_on_first:
            break

    mode, start, _end = _time_constraint(params.get("time"))
    if mode == "nearest":
        slots = _nearest_payloads(slots, anchor=start)
    exact_count = len(slots) if mode == "exact" else None
    verified: dict[str, object] = {}
    if exact_count == 1:
        slot = slots[0]
        verified = {
            "appointment_id": anchor.appointment_id,
            "appointment_ids": list(excluded_ids),
            "visit_group_id": anchor.visit_group_id,
            "branch_id": slot["branch_id"],
            "service_id": slot["service_id"],
            "doctor_id": slot["doctor_id"],
            "start_at": slot["start_at"],
            "reschedule_components": slot["reschedule_components"],
        }

    return (
        ReadResult(
            kind=request.kind,
            ok=True,
            payload={
                **service_meta,
                "checked_dates": checked_dates,
                "slots": slots,
                "matching_slot_count": len(slots),
                "search_truncated": truncated,
                "presentation_unit": "visit",
            },
        ),
        VerificationFacts(
            exact_slot_match_count=exact_count,
            verified_parameters=verified,
        ),
    )


def _read_appointments(
    request: ReadRequest,
    context: ReadExecutionContext,
    *,
    operation_type: str,
) -> tuple[ReadResult, VerificationFacts, list[AppointmentRecord] | None]:
    adapter = _adapter(context)
    adapter.require_capability(ClinicCapability.APPOINTMENTS_READ)
    response = adapter.get_patient_appointments(
        AppointmentReadRequest(
            patient_id=str(context.patient.id),
            include_past=False,
            now=context.now,
        )
    )
    rows = list(response.appointments)
    if operation_type in {"confirm_appointment", "cancel_appointment", "reschedule"}:
        rows = [row for row in rows if row.status in _ACTIONABLE_APPOINTMENT_STATUSES]
    if operation_type == "confirm_appointment":
        rows = [row for row in rows if row.status == "pending"]

    params = request.parameters
    if params.get("appointment_id") is not None:
        rows = [
            row for row in rows
            if row.appointment_id == str(params["appointment_id"])
        ]
    if params.get("service_id") is not None:
        rows = [row for row in rows if row.service_id == str(params["service_id"])]
    if params.get("doctor_id") is not None:
        rows = [row for row in rows if row.doctor_id == str(params["doctor_id"])]
    if params.get("device_key") is not None:
        rows = [
            row
            for row in rows
            if row.laser_device_key == str(params["device_key"])
        ]
    if params.get("date") is not None:
        rows = [
            row
            for row in rows
            if _appointment_matches_date(row, params["date"], now=context.now)
        ]
    if params.get("time") is not None:
        rows = [
            row
            for row in rows
            if _appointment_matches_time(row, params["time"])
        ]

    component_scoped = (
        params.get("appointment_id") is not None
        or params.get("service_id") is not None
        or operation_type == "confirm_appointment"
    )
    logical_groups = (
        [[row] for row in rows]
        if component_scoped
        else _group_appointment_rows(rows)
    )
    selected = logical_groups[0] if len(logical_groups) == 1 else None
    verified: dict[str, object] = {}
    if selected is not None:
        anchor = selected[0]
        verified = {
            "appointment_id": anchor.appointment_id,
            "branch_id": anchor.branch_id,
            "doctor_id": anchor.doctor_id,
        }
        if len(selected) == 1:
            verified["service_id"] = anchor.service_id
            if anchor.laser_device_key:
                verified["device_key"] = anchor.laser_device_key
        else:
            verified["appointment_ids"] = [row.appointment_id for row in selected]
            if anchor.visit_group_id:
                verified["visit_group_id"] = anchor.visit_group_id

    if operation_type == "appointment_list":
        count = None
    elif operation_type == "confirm_appointment":
        count = len(rows)
    else:
        count = len(logical_groups)
    visits = [_visit_payload(group) for group in _group_appointment_rows(rows)]
    return (
        ReadResult(
            kind=request.kind,
            ok=True,
            payload={
                "appointments": [_appointment_payload(row) for row in rows],
                "visits": visits,
                "visit_count": len(visits),
                "presentation_unit": "visit",
            },
        ),
        VerificationFacts(
            appointment_match_count=count,
            verified_parameters=verified,
        ),
        selected,
    )


def _read_customer_profile(request: ReadRequest, context: ReadExecutionContext) -> ReadResult:
    patient = context.patient
    return ReadResult(
        kind=request.kind,
        ok=True,
        payload={
            "patient": {
                "first_name": patient.first_name,
                "last_name": patient.last_name,
                "phone": patient.phone,
                "preferred_language": patient.preferred_language,
                "status": patient.status,
            }
        },
    )


def _read_customer_history(
    request: ReadRequest,
    context: ReadExecutionContext,
) -> ReadResult:
    history = build_patient_history_context(
        context.db,
        workspace_id=context.workspace.id,
        patient=context.patient,
        recent_limit=20,
    )
    payload = history.model_dump(mode="json")
    recent = payload.get("recent_appointments")
    if isinstance(recent, list):
        grouped: dict[str, list[dict[str, object]]] = {}
        for item in recent:
            if not isinstance(item, dict):
                continue
            group_id = item.get("visit_group_id")
            appointment_id = item.get("appointment_id")
            key = (
                f"visit:{group_id}"
                if group_id
                else f"appointment:{appointment_id}"
            )
            grouped.setdefault(key, []).append(item)
        visits: list[dict[str, object]] = []
        for items in grouped.values():
            ordered = sorted(
                items,
                key=lambda item: str(item.get("start_at") or ""),
            )
            statuses = {str(item.get("status") or "") for item in ordered}
            visits.append(
                {
                    "visit_group_id": ordered[0].get("visit_group_id"),
                    "appointment_ids": [
                        item.get("appointment_id") for item in ordered
                    ],
                    "status": (
                        next(iter(statuses)) if len(statuses) == 1 else "mixed"
                    ),
                    "start_at": min(
                        str(item.get("start_at") or "") for item in ordered
                    ),
                    "end_at": max(
                        str(item.get("end_at") or "") for item in ordered
                    ),
                    "services": [item.get("service_name") for item in ordered],
                    "branch_name": ordered[0].get("branch_name"),
                    "doctor_name": ordered[0].get("doctor_name"),
                    "price_minor": sum(
                        int(item.get("price_minor") or 0) for item in ordered
                    ),
                    "net_paid_minor": sum(
                        int(item.get("net_paid_minor") or 0) for item in ordered
                    ),
                }
            )
        payload["recent_visits"] = visits
        payload["recent_visit_count"] = len(visits)
        payload["presentation_unit"] = "visit"
    return ReadResult(
        kind=request.kind,
        ok=True,
        payload={"history": payload},
    )


def _package_filters(request: ReadRequest) -> tuple[UUID | None, str | None, int | None, UUID | None]:
    service_id = request.parameters.get("service_id")
    package_id = request.parameters.get("package_id")
    device_key = str(request.parameters["device_key"]) if request.parameters.get("device_key") else None
    sessions = int(request.parameters["package_sessions"]) if request.parameters.get("package_sessions") is not None else None
    return (
        _parse_uuid(service_id, field_name="service_id") if service_id is not None else None,
        device_key,
        sessions,
        _parse_uuid(package_id, field_name="package_id") if package_id is not None else None,
    )


def _read_customer_packages(request: ReadRequest, context: ReadExecutionContext) -> ReadResult:
    service_id, device_key, _sessions, package_id = _package_filters(request)
    rows = list_patient_packages(
        context.db,
        workspace_id=context.workspace.id,
        patient_id=context.patient.id,
        service_id=service_id,
        usable_only=False,
        include_financials=False,
    )
    if package_id is not None:
        rows = [row for row in rows if row.id == package_id]
    if device_key is not None:
        rows = [row for row in rows if row.laser_device_key == device_key]
    return ReadResult(
        kind=request.kind,
        ok=True,
        payload={
            "packages": [
                row.model_dump(
                    mode="json",
                    include={
                        "name",
                        "sessions_purchased",
                        "sessions_reserved",
                        "sessions_consumed",
                        "sessions_remaining",
                        "laser_device_name",
                        "purchased_at",
                        "expires_at",
                        "status",
                        "effective_status",
                        "source",
                    },
                )
                for row in rows
            ]
        },
    )


def _read_package_offers(
    request: ReadRequest,
    context: ReadExecutionContext,
) -> tuple[ReadResult, VerificationFacts]:
    service_id, device_key, sessions, _package_id = _package_filters(request)
    rows = list_package_offers(
        context.db,
        workspace_id=context.workspace.id,
        service_id=service_id,
        active_only=True,
    )
    if device_key is not None:
        rows = [row for row in rows if row.device_key == device_key]
    if sessions is not None:
        rows = [row for row in rows if row.sessions_count == sessions]

    verified: dict[str, object] = {}
    if len(rows) == 1:
        row = rows[0]
        verified = {
            "package_offer_id": str(row.id),
            "service_id": str(row.service_id),
            "device_key": row.device_key,
            "package_sessions": row.sessions_count,
            "price_minor": int(row.price_minor),
            "currency": row.currency,
        }
    return (
        ReadResult(
            kind=request.kind,
            ok=True,
            payload={"offers": [row.model_dump(mode="json") for row in rows]},
        ),
        VerificationFacts(
            package_offer_match_count=len(rows),
            verified_parameters=verified,
        ),
    )


def _pulse_device_key(request: ReadRequest) -> str | None:
    value = request.parameters.get("device_key")
    return str(value) if value not in (None, "") else None


def _read_pulse_balance(request: ReadRequest, context: ReadExecutionContext) -> ReadResult:
    device_key = _pulse_device_key(request)
    rows = list_patient_pulse_balances(
        context.db,
        workspace_id=context.workspace.id,
        patient_id=context.patient.id,
    )
    if device_key is not None:
        rows = [row for row in rows if row.device_key == device_key]
    return ReadResult(
        kind=request.kind,
        ok=True,
        payload={"balances": [row.model_dump(mode="json") for row in rows]},
    )


def _read_pulse_packs(request: ReadRequest, context: ReadExecutionContext) -> ReadResult:
    device_key = _pulse_device_key(request)
    rows = list_patient_pulse_packs(
        context.db,
        workspace_id=context.workspace.id,
        patient_id=context.patient.id,
        device_key=device_key,
        include_financials=False,
    )
    return ReadResult(
        kind=request.kind,
        ok=True,
        payload={
            "packs": [
                row.model_dump(
                    mode="json",
                    include={
                        "device_key",
                        "device_name",
                        "pulses_purchased",
                        "pulses_consumed",
                        "pulses_remaining",
                        "purchased_at",
                        "expires_at",
                        "status",
                        "effective_status",
                    },
                )
                for row in rows
            ]
        },
    )


def _read_pulse_pack_offers(
    request: ReadRequest,
    context: ReadExecutionContext,
) -> tuple[ReadResult, VerificationFacts]:
    device_key = _pulse_device_key(request)
    pulse_count = (
        int(request.parameters["pulse_count"])
        if request.parameters.get("pulse_count") is not None
        else None
    )
    rows = list_pulse_pack_offers(
        context.db,
        workspace_id=context.workspace.id,
        active_only=True,
    )
    if device_key is not None:
        rows = [row for row in rows if row.device_key == device_key]
    if pulse_count is not None:
        rows = [row for row in rows if row.pulses_count == pulse_count]

    verified: dict[str, object] = {}
    if len(rows) == 1:
        row = rows[0]
        verified = {
            "pulse_pack_offer_id": str(row.id),
            "device_key": row.device_key,
            "pulse_count": int(row.pulses_count),
            "price_minor": int(row.price_minor),
            "currency": row.currency,
        }
    return (
        ReadResult(
            kind=request.kind,
            ok=True,
            payload={"offers": [row.model_dump(mode="json") for row in rows]},
        ),
        VerificationFacts(
            pulse_offer_match_count=len(rows),
            verified_parameters=verified,
        ),
    )


def _read_pulse_billing_settings(
    request: ReadRequest,
    context: ReadExecutionContext,
) -> ReadResult:
    device_key = _pulse_device_key(request)
    rows = list_pulse_billing_settings(
        context.db,
        workspace_id=context.workspace.id,
    )
    if device_key is not None:
        rows = [row for row in rows if row.device_key == device_key]
    payload: dict[str, object] = {
        "devices": [row.model_dump(mode="json") for row in rows]
    }
    pulse_count = request.parameters.get("pulse_count")
    if (
        isinstance(pulse_count, int)
        and not isinstance(pulse_count, bool)
        and pulse_count > 0
        and len(rows) == 1
        and rows[0].overage_price_minor is not None
    ):
        payload.update(
            requested_pulse_count=pulse_count,
            overage_total_minor=pulse_count * int(rows[0].overage_price_minor),
            currency=rows[0].currency,
        )
    return ReadResult(
        kind=request.kind,
        ok=True,
        payload=payload,
    )


def _read_package_refund_quote(request: ReadRequest, context: ReadExecutionContext) -> ReadResult:
    service_id, device_key, _sessions, package_id = _package_filters(request)
    quotes, unsafe = list_patient_package_refund_quotes(
        context.db,
        workspace_id=context.workspace.id,
        patient_id=context.patient.id,
        package_id=package_id,
        service_id=service_id,
        laser_device_key=device_key,
    )
    return ReadResult(
        kind=request.kind,
        ok=not unsafe,
        payload={
            "quotes": [quote.as_dict() for quote in quotes],
            "unsafe_package_ids": [str(item) for item in unsafe],
            "needs_package_choice": len(quotes) + len(unsafe) > 1,
        },
        error_code="refund_quote_requires_staff" if unsafe else None,
    )


def _merge_verification(base: VerificationFacts, extra: VerificationFacts) -> VerificationFacts:
    return VerificationFacts(
        appointment_match_count=(
            extra.appointment_match_count
            if extra.appointment_match_count is not None
            else base.appointment_match_count
        ),
        exact_slot_match_count=(
            extra.exact_slot_match_count
            if extra.exact_slot_match_count is not None
            else base.exact_slot_match_count
        ),
        package_offer_match_count=(
            extra.package_offer_match_count
            if extra.package_offer_match_count is not None
            else base.package_offer_match_count
        ),
        pulse_offer_match_count=(
            extra.pulse_offer_match_count
            if extra.pulse_offer_match_count is not None
            else base.pulse_offer_match_count
        ),
        requires_human=base.requires_human or extra.requires_human,
        verified_parameters={**base.verified_parameters, **extra.verified_parameters},
    )


def execute_step_reads(
    step: PlanStep,
    context: ReadExecutionContext,
) -> ReadExecutionBundle:
    """Execute one planner step's verified reads without performing any write action."""
    results: list[ReadResult] = []
    verification = VerificationFacts()
    selected_appointments: list[AppointmentRecord] | None = None

    for request in step.reads:
        if request.kind == "service_catalog":
            results.append(
                _read_service_catalog(
                    request,
                    context,
                    include_explanation=step.operation_type == "service_info",
                )
            )
        elif request.kind == "clinic_info":
            results.append(_read_clinic_info(request, context))
        elif request.kind == "doctors":
            results.append(_read_doctors(request, context))
        elif request.kind == "appointments":
            result, verified, selected = _read_appointments(
                request,
                context,
                operation_type=step.operation_type,
            )
            results.append(result)
            verification = _merge_verification(verification, verified)
            if selected is not None:
                selected_appointments = selected
        elif request.kind == "availability":
            if (
                step.operation_type == "reschedule"
                and selected_appointments is not None
                and len(selected_appointments) > 1
            ):
                result, verified = _read_group_reschedule_availability(
                    request,
                    context,
                    appointments=selected_appointments,
                )
            else:
                inherited: dict[str, object] = {}
                if step.operation_type == "reschedule" and selected_appointments:
                    appointment = selected_appointments[0]
                    inherited = {
                        "appointment_id": appointment.appointment_id,
                        "branch_id": appointment.branch_id,
                        "service_id": appointment.service_id,
                        "doctor_id": appointment.doctor_id,
                        "device_key": appointment.laser_device_key,
                        "reschedule": True,
                    }
                result, verified = _read_availability(
                    request,
                    context,
                    inherited=inherited,
                )
            results.append(result)
            verification = _merge_verification(verification, verified)
        elif request.kind == "customer_profile":
            results.append(_read_customer_profile(request, context))
        elif request.kind == "customer_history":
            results.append(_read_customer_history(request, context))
        elif request.kind == "customer_packages":
            results.append(_read_customer_packages(request, context))
        elif request.kind == "package_offers":
            result, verified = _read_package_offers(request, context)
            results.append(result)
            verification = _merge_verification(verification, verified)
        elif request.kind == "package_refund_quote":
            results.append(_read_package_refund_quote(request, context))
        elif request.kind == "pulse_balance":
            results.append(_read_pulse_balance(request, context))
        elif request.kind == "pulse_packs":
            results.append(_read_pulse_packs(request, context))
        elif request.kind == "pulse_pack_offers":
            result, verified = _read_pulse_pack_offers(request, context)
            results.append(result)
            verification = _merge_verification(verification, verified)
        elif request.kind == "pulse_billing_settings":
            results.append(_read_pulse_billing_settings(request, context))
        else:
            raise ReadExecutionError(f"Unsupported V2 read kind: {request.kind}")

    return ReadExecutionBundle(results=results, verification=verification)
