from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from langchain_core.tools import BaseTool, tool
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.availability_presentation import availability_windows_from_slots
from app.integrations.clinic.base import (
    AppointmentReadRequest,
    AppointmentReadResult,
    AppointmentRecord,
    AvailabilityRequest,
    AvailabilityResult,
    CancelAppointmentRequest,
    ClinicActionRequiresHuman,
    ClinicCapability,
    ConfirmAppointmentRequest,
    CreateAppointmentRequest,
    RescheduleAppointmentRequest,
)
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.agent_action import AgentAction
from app.models.branch import Branch
from app.models.conversation import Conversation
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.lead import Lead
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.workspace import Workspace
from app.services.booking import BookingRuleError
from app.services.campaign_attribution import record_direct_campaign_booking_conversion
from app.services.conversation_flows import get_active_flow
from app.services.crm_tasks import CRMTaskError, create_crm_task
from app.services.handoffs import create_handoff
from app.services.laser_booking_context import current_laser_device_key
from app.services.patient_history import build_patient_history_context
from app.services.patient_packages import list_patient_packages


@dataclass(frozen=True)
class AgentToolContext:
    db: Session
    workspace: Workspace
    patient: Patient
    conversation: Conversation
    run_id: UUID
    handoff_context: dict | None = None


def _json(payload: dict | list) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID from a tool result.") from exc


def _confirmation_ticket_reschedule_state(ctx: AgentToolContext) -> dict | None:
    flow = get_active_flow(
        ctx.db,
        workspace_id=ctx.workspace.id,
        conversation_id=ctx.conversation.id,
        patient_id=ctx.patient.id,
        run_id=ctx.run_id,
    )
    if flow is None or flow.flow_type != "appointment_reschedule":
        return None
    state = dict(flow.entity_state or {})
    if state.get("reschedule_origin") != "confirmation_ticket":
        return None
    if not state.get("appointment_id") or not state.get("locked_service_id"):
        return None
    return state


def _money(price_minor: int, currency: str) -> str:
    value = Decimal(price_minor) / Decimal(100)
    if value == value.to_integral():
        amount = f"{int(value):,}"
    else:
        amount = f"{value:,.2f}"
    if currency.upper() == "EGP":
        return f"{amount} EGP"
    return f"{amount} {currency.upper()}"


def _normalize_lookup_text(value: str | None) -> str:
    """Normalize customer-facing lookup text without collapsing distinct place names.

    The result is intentionally token-preserving. For example, "Cairo" stays
    distinct from "New Cairo" instead of applying fuzzy substitutions that could
    make one branch accidentally match the other.
    """
    if not value:
        return ""

    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    normalized = normalized.replace("ـ", "")

    # Remove Arabic/Unicode combining marks (harakat, accents, etc.).
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )

    # A few Arabic letter variants are commonly interchangeable in user input.
    normalized = normalized.translate(
        str.maketrans(
            {
                "أ": "ا",
                "إ": "ا",
                "آ": "ا",
                "ى": "ي",
                "ؤ": "و",
                "ئ": "ي",
            }
        )
    )

    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _service_search_text(service: object) -> str:
    values = [
        getattr(service, "name", None),
        getattr(service, "category", None),
        getattr(service, "description", None),
    ]
    return " | ".join(
        normalized
        for value in values
        if (normalized := _normalize_lookup_text(value))
    )


def _resolve_services_by_search(
    services: list[Service],
    search: str,
) -> list[Service]:
    """Resolve an AI-extracted service name with exact-name priority.

    A customer selecting an option such as ``ليزر إزالة الشعر`` should resolve
    that exact normalized service even when another service has a longer name
    such as ``ليزر إزالة الشعر — Demo``. Broad phrase matching is only used
    when there is no exact active service name match.
    """
    query = _normalize_lookup_text(search)
    if not query:
        return services

    exact = [
        service
        for service in services
        if _normalize_lookup_text(getattr(service, "name", None)) == query
    ]
    if exact:
        return exact

    return [
        service
        for service in services
        if query in _service_search_text(service)
    ]


def _branch_display_address(branch: object) -> str | None:
    """Build a stable human-readable branch address from persisted address fields."""
    parts: list[str] = []
    seen: set[str] = set()

    for field_name in ("address_line1", "address_line2", "city", "state"):
        raw_value = getattr(branch, field_name, None)
        if raw_value is None:
            continue

        value = str(raw_value).strip()
        if not value:
            continue

        key = _normalize_lookup_text(value)
        if key and key not in seen:
            seen.add(key)
            parts.append(value)

    return "، ".join(parts) or None


def _branch_search_text(branch: object) -> str:
    """Return normalized searchable text for one branch.

    Keeping the branch name as one normalized phrase means a query such as
    "Regression Cairo Branch" does not become a false positive for
    "Regression New Cairo Branch".
    """
    values = [
        getattr(branch, "name", None),
        getattr(branch, "code", None),
        getattr(branch, "address_line1", None),
        getattr(branch, "address_line2", None),
        getattr(branch, "city", None),
        getattr(branch, "state", None),
    ]
    return " | ".join(
        normalized
        for value in values
        if (normalized := _normalize_lookup_text(value))
    )


def _resolve_branch_by_search(
    branches: list[Branch],
    search: str,
) -> list[Branch]:
    """Resolve a customer-entered branch name/address conservatively.

    Exact normalized name/code/city matches are preferred. Otherwise, a
    normalized phrase match is used. Ambiguous matches are returned to the
    caller instead of guessing.
    """
    query = _normalize_lookup_text(search)
    if not query:
        return branches

    exact: list[Branch] = []
    phrase_matches: list[Branch] = []

    for branch in branches:
        exact_values = {
            _normalize_lookup_text(getattr(branch, "name", None)),
            _normalize_lookup_text(getattr(branch, "code", None)),
            _normalize_lookup_text(getattr(branch, "city", None)),
        }
        exact_values.discard("")

        if query in exact_values:
            exact.append(branch)
            continue

        if query in _branch_search_text(branch):
            phrase_matches.append(branch)

    return exact or phrase_matches


def _doctor_display_name(staff: object) -> str:
    first_name = str(getattr(staff, "first_name", "") or "").strip()
    last_name = str(getattr(staff, "last_name", "") or "").strip()
    return f"{first_name} {last_name}".strip()


def _doctor_search_text(doctor: object, staff: object) -> str:
    values = [
        _doctor_display_name(staff),
        getattr(staff, "first_name", None),
        getattr(staff, "last_name", None),
        getattr(doctor, "specialization", None),
    ]
    return " | ".join(
        normalized
        for value in values
        if (normalized := _normalize_lookup_text(value))
    )


def _resolve_doctor_rows_by_search(
    rows: list[tuple[Doctor, Staff]],
    search: str,
) -> list[tuple[Doctor, Staff]]:
    """Resolve an AI-extracted doctor entity without keyword intent routing.

    Exact normalized full-name/first-name/last-name matches are preferred.
    Otherwise a conservative normalized phrase match is used. Ambiguous
    matches are returned to the caller so the customer can choose.
    """
    query = _normalize_lookup_text(search)
    if not query:
        return rows

    exact: list[tuple[Doctor, Staff]] = []
    phrase_matches: list[tuple[Doctor, Staff]] = []

    for doctor, staff in rows:
        exact_values = {
            _normalize_lookup_text(_doctor_display_name(staff)),
            _normalize_lookup_text(getattr(staff, "first_name", None)),
            _normalize_lookup_text(getattr(staff, "last_name", None)),
        }
        exact_values.discard("")

        if query in exact_values:
            exact.append((doctor, staff))
            continue

        if query in _doctor_search_text(doctor, staff):
            phrase_matches.append((doctor, staff))

    return exact or phrase_matches


def _active_booking_doctor_rows(
    ctx: AgentToolContext,
    *,
    branch_id: UUID,
    service_id: UUID,
) -> list[tuple[Doctor, Staff]]:
    rows = ctx.db.execute(
        select(Doctor, Staff)
        .join(
            Staff,
            (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
        )
        .join(
            DoctorBranch,
            (DoctorBranch.workspace_id == Doctor.workspace_id)
            & (DoctorBranch.doctor_id == Doctor.id),
        )
        .join(
            DoctorService,
            (DoctorService.workspace_id == Doctor.workspace_id)
            & (DoctorService.doctor_id == Doctor.id),
        )
        .where(
            Doctor.workspace_id == ctx.workspace.id,
            Doctor.is_active.is_(True),
            Doctor.booking_enabled.is_(True),
            Staff.is_active.is_(True),
            DoctorBranch.branch_id == branch_id,
            DoctorBranch.is_active.is_(True),
            DoctorService.service_id == service_id,
            DoctorService.is_active.is_(True),
        )
        .order_by(Staff.first_name, Staff.last_name)
    ).all()

    unique: list[tuple[Doctor, Staff]] = []
    seen: set[UUID] = set()
    for doctor, staff in rows:
        if doctor.id in seen:
            continue
        seen.add(doctor.id)
        unique.append((doctor, staff))
    return unique


def _availability_doctor_names(
    ctx: AgentToolContext,
    *,
    doctor_ids: set[UUID],
) -> dict[UUID, str]:
    """Load only doctor display names needed by already-computed slots.

    Branch and service rows are already present in _availability_payload, so the
    previous helper's extra branch/service reads were redundant remote round trips.
    """
    doctor_names: dict[UUID, str] = {}
    if not doctor_ids:
        return doctor_names

    rows = ctx.db.execute(
        select(Doctor.id, Staff.first_name, Staff.last_name)
        .join(
            Staff,
            (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
        )
        .where(
            Doctor.workspace_id == ctx.workspace.id,
            Doctor.id.in_(doctor_ids),
        )
    ).all()
    for doctor_id_value, first_name, last_name in rows:
        full_name = f"{first_name or ''} {last_name or ''}".strip()
        doctor_names[doctor_id_value] = full_name or "الدكتور المتاح"
    return doctor_names


def _availability_display_context(
    ctx: AgentToolContext,
    *,
    branch_id: UUID,
    service_id: UUID,
    doctor_ids: set[UUID],
    preloaded_branch: Branch | None = None,
    preloaded_service: Service | None = None,
) -> tuple[str | None, str | None, dict[UUID, str]]:
    """Return display labels for availability without forcing redundant reads.

    The optional preloaded rows preserve the v0.19.3.2 latency optimization in
    `_availability_payload`, while keeping this helper available for legacy tool
    paths and regression tests that monkeypatch it.
    """
    branch = preloaded_branch
    if branch is None:
        branch = ctx.db.scalar(
            select(Branch).where(
                Branch.workspace_id == ctx.workspace.id,
                Branch.id == branch_id,
            )
        )

    service = preloaded_service
    if service is None:
        service = ctx.db.scalar(
            select(Service).where(
                Service.workspace_id == ctx.workspace.id,
                Service.id == service_id,
            )
        )

    doctor_names = _availability_doctor_names(ctx, doctor_ids=doctor_ids)
    return (
        branch.name if branch else None,
        service.name if service else None,
        doctor_names,
    )


def _filter_slots_by_local_window(
    slots: list,
    *,
    tz: ZoneInfo,
    lower_bound: time | None,
    upper_bound: time | None,
) -> list[tuple[object, datetime]]:
    filtered: list[tuple[object, datetime]] = []

    for slot in slots:
        local_start = slot.start_at.astimezone(tz)
        local_end = slot.end_at.astimezone(tz)

        start_time = local_start.timetz().replace(tzinfo=None)
        end_time = local_end.timetz().replace(tzinfo=None)

        if lower_bound and start_time < lower_bound:
            continue

        # If the customer says "من 8 لحد 9", the whole appointment must
        # fit inside that window, not merely start before 9.
        if upper_bound and end_time > upper_bound:
            continue

        filtered.append((slot, local_start))

    return filtered


def _service_matches(
    ctx: AgentToolContext,
    search: str,
) -> list[Service]:
    services = list(
        ctx.db.scalars(
            select(Service)
            .where(
                Service.workspace_id == ctx.workspace.id,
                Service.is_active.is_(True),
            )
            .order_by(Service.name)
            .limit(100)
        )
    )
    return _resolve_services_by_search(services, search)


def _active_branches(ctx: AgentToolContext) -> list[Branch]:
    stmt = select(Branch).where(
        Branch.workspace_id == ctx.workspace.id,
        Branch.is_active.is_(True),
    )
    if ctx.workspace.primary_branch_id is not None:
        stmt = stmt.where(Branch.id == ctx.workspace.primary_branch_id)
    return list(ctx.db.scalars(stmt.order_by(Branch.name)))


def _adapter_availability(
    ctx: AgentToolContext,
    *,
    branch_id: str,
    service_id: str,
    booking_date: date,
    doctor_id: str | None = None,
    exclude_appointment_id: str | None = None,
    laser_device_key: str | None = None,
) -> AvailabilityResult:
    """Read verified availability through the workspace clinic adapter."""
    adapter = get_clinic_adapter(db=ctx.db, workspace=ctx.workspace)
    adapter.require_capability(ClinicCapability.AVAILABILITY_READ)
    return adapter.get_availability(
        AvailabilityRequest(
            branch_id=branch_id,
            service_id=service_id,
            booking_date=booking_date,
            doctor_id=doctor_id,
            exclude_appointment_id=exclude_appointment_id,
            laser_device_key=laser_device_key,
        )
    )


def _adapter_patient_appointments(
    ctx: AgentToolContext,
    *,
    include_past: bool = False,
) -> AppointmentReadResult:
    """Read the current patient's appointments through the clinic adapter."""
    adapter = get_clinic_adapter(db=ctx.db, workspace=ctx.workspace)
    adapter.require_capability(ClinicCapability.APPOINTMENTS_READ)
    return adapter.get_patient_appointments(
        AppointmentReadRequest(
            patient_id=str(ctx.patient.id),
            include_past=include_past,
        )
    )


def _canonical_appointment_summary(appointment: AppointmentRecord) -> dict:
    try:
        tz = ZoneInfo(appointment.timezone)
    except Exception:
        tz = UTC

    return {
        "appointment_id": appointment.appointment_id,
        "status": appointment.status,
        "service": appointment.service_name,
        "branch": appointment.branch_name,
        "doctor": appointment.doctor_name,
        "start_local": appointment.start_at.astimezone(tz).isoformat(),
        "end_local": appointment.end_at.astimezone(tz).isoformat(),
        "timezone": getattr(tz, "key", "UTC"),
        "price": _money(appointment.price_minor, appointment.currency),
        "payment_status": appointment.payment_status,
        "amount_paid": (
            _money(appointment.amount_paid_minor, "EGP")
            if appointment.amount_paid_minor is not None
            else None
        ),
        "payment_method": appointment.payment_method,
        "billing_context": getattr(appointment, "billing_context", "standard"),
        "package_external_id": getattr(appointment, "package_external_id", None),
        "laser_device_key": getattr(appointment, "laser_device_key", None),
        "laser_device_name": getattr(appointment, "laser_device_name", None),
    }


def _availability_payload(
    ctx: AgentToolContext,
    *,
    branch_id: str | None = None,
    service_id: str | None = None,
    branch: Branch | None = None,
    service: Service | None = None,
    booking_date: date,
    doctor_id: str | UUID | None,
    requested_start: time | None,
    lower_bound: time | None,
    upper_bound: time | None,
    exclude_appointment_id: str | UUID | None = None,
    laser_device_key: str | None = None,
) -> dict:
    resolved_branch_id = branch_id or (str(branch.id) if branch is not None else None)
    resolved_service_id = service_id or (str(service.id) if service is not None else None)
    if resolved_branch_id is None:
        raise TypeError("_availability_payload() requires branch_id or branch")
    if resolved_service_id is None:
        raise TypeError("_availability_payload() requires service_id or service")

    availability = _adapter_availability(
        ctx,
        branch_id=str(resolved_branch_id),
        service_id=str(resolved_service_id),
        booking_date=booking_date,
        doctor_id=str(doctor_id) if doctor_id is not None else None,
        exclude_appointment_id=(
            str(exclude_appointment_id) if exclude_appointment_id is not None else None
        ),
        laser_device_key=laser_device_key,
    )
    timezone_name = availability.timezone
    slots = list(availability.slots)
    tz = ZoneInfo(timezone_name)
    window_slots = _filter_slots_by_local_window(
        slots,
        tz=tz,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
    )

    requested_time_unavailable = False
    matching_slot_count = len(window_slots)
    presented_slots = window_slots

    if requested_start is not None:
        exact_slots = [
            (slot, local_start)
            for slot, local_start in window_slots
            if local_start.timetz().replace(tzinfo=None) == requested_start
        ]
        matching_slot_count = len(exact_slots)
        if exact_slots:
            presented_slots = exact_slots
        else:
            requested_time_unavailable = True
            target_minutes = requested_start.hour * 60 + requested_start.minute

            def distance_from_requested(item) -> tuple[int, object]:
                _, local_start = item
                local_minutes = local_start.hour * 60 + local_start.minute
                return abs(local_minutes - target_minutes), local_start

            if lower_bound is not None or upper_bound is not None:
                nearby_pool = window_slots
            else:
                nearby_pool = [(slot, slot.start_at.astimezone(tz)) for slot in slots]
            presented_slots = sorted(nearby_pool, key=distance_from_requested)[:12]

    result_slots: list[dict[str, object]] = []
    for slot, local_start in presented_slots:
        local_end = slot.end_at.astimezone(tz)
        result_slots.append(
            {
                "branch_id": slot.branch_id,
                "branch_name": slot.branch_name,
                "doctor_id": slot.doctor_id,
                "doctor_name": slot.doctor_name or "الدكتور المتاح",
                "service_id": slot.service_id,
                "service_name": slot.service_name,
                "start_local": local_start.isoformat(),
                "end_local": local_end.isoformat(),
                "start_time_24h": local_start.strftime("%H:%M"),
                "end_time_24h": local_end.strftime("%H:%M"),
                "duration_minutes": slot.duration_minutes,
                "timezone": timezone_name,
                "price": _money(slot.price_minor, slot.currency),
                "laser_device_key": getattr(slot, "laser_device_key", None),
                "laser_device_name": getattr(slot, "laser_device_name", None),
            }
        )

    return {
        "date": booking_date.isoformat(),
        "timezone": timezone_name,
        "branch": {
            "branch_id": availability.branch_id,
            "branch_name": availability.branch_name,
        },
        "service": {
            "service_id": availability.service_id,
            "service_name": availability.service_name,
            "duration_minutes": availability.service_duration_minutes,
            "price": (
                _money(availability.service_price_minor, availability.service_currency)
                if availability.service_price_minor is not None
                and availability.service_currency
                else None
            ),
        },
        "requested_start_time": (
            requested_start.strftime("%H:%M") if requested_start else None
        ),
        # Keep all verified starts internally so a customer can choose any clock
        # time from a displayed availability window. These rows are not rendered
        # directly to the customer.
        "slots": result_slots,
        "availability_windows": availability_windows_from_slots(result_slots),
        "matching_slot_count": matching_slot_count,
        "requested_time_unavailable": requested_time_unavailable,
        "more_slots_available": False,
    }

def _record_action(
    ctx: AgentToolContext,
    *,
    tool_name: str,
    action_type: str,
    status: str,
    input_payload: dict,
    output_payload: dict,
    appointment_id: UUID | None = None,
    error_message: str | None = None,
) -> None:
    ctx.db.add(
        AgentAction(
            workspace_id=ctx.workspace.id,
            conversation_id=ctx.conversation.id,
            patient_id=ctx.patient.id,
            appointment_id=appointment_id,
            run_id=ctx.run_id,
            tool_name=tool_name,
            action_type=action_type,
            status=status,
            input_json=input_payload,
            output_json=output_payload,
            error_message=error_message,
        )
    )
    ctx.db.commit()


def _native_action_appointment_id(appointment_id: str | None) -> UUID | None:
    """Attach native appointment FKs to AgentAction when the source uses UUID IDs.

    External clinic systems may use identifiers such as ``BK-991``; those remain
    in the action payload while the nullable native FK stays empty.
    """
    if not appointment_id:
        return None
    try:
        return UUID(str(appointment_id))
    except (TypeError, ValueError):
        return None



def _set_handoff(
    ctx: AgentToolContext,
    *,
    reason: str,
    category: str = "booking_exception",
    priority: str = "normal",
):
    context = dict(ctx.handoff_context or {})
    context["trigger"] = "system"
    context["semantic_reason"] = reason
    return create_handoff(
        ctx.db,
        workspace_id=ctx.workspace.id,
        conversation=ctx.conversation,
        patient=ctx.patient,
        reason=reason,
        category=category,
        priority=priority,
        source="system",
        handoff_context=context,
        commit=False,
    )


def build_clinic_tools(ctx: AgentToolContext) -> list[BaseTool]:
    @tool
    def get_customer_profile() -> str:
        """Get the current customer's own CRM profile. Never use this to access another patient."""
        payload = {
            "ok": True,
            "patient": {
                "patient_id": str(ctx.patient.id),
                "first_name": ctx.patient.first_name,
                "last_name": ctx.patient.last_name,
                "phone": ctx.patient.phone,
                "preferred_branch_id": (
                    str(ctx.patient.preferred_branch_id)
                    if ctx.patient.preferred_branch_id
                    else None
                ),
                "preferred_language": ctx.patient.preferred_language,
            },
        }
        _record_action(
            ctx,
            tool_name="get_customer_profile",
            action_type="crm_read",
            status="success",
            input_payload={},
            output_payload=payload,
        )
        return _json(payload)

    @tool
    def get_customer_history(recent_limit: int = 20) -> str:
        """Get the current customer's own historical clinic facts: past visits, services and payments. Never use this to access another patient."""
        history = build_patient_history_context(
            ctx.db,
            workspace_id=ctx.workspace.id,
            patient=ctx.patient,
            recent_limit=recent_limit,
        )
        payload = {"ok": True, "history": history.model_dump(mode="json")}
        _record_action(
            ctx,
            tool_name="get_customer_history",
            action_type="crm_history_read",
            status="success",
            input_payload={"recent_limit": recent_limit},
            output_payload=payload,
        )
        return _json(payload)

    @tool
    def update_marketing_consent(consent: bool) -> str:
        """Update only the current customer's own promotional/marketing message consent after an explicit customer request."""
        previous = bool(ctx.patient.marketing_consent)
        ctx.patient.marketing_consent = bool(consent)
        ctx.patient.marketing_consent_at = datetime.now(UTC) if consent else None
        payload = {
            "ok": True,
            "marketing_consent": bool(consent),
            "changed": previous != bool(consent),
        }
        ctx.db.add(
            AgentAction(
                workspace_id=ctx.workspace.id,
                conversation_id=ctx.conversation.id,
                patient_id=ctx.patient.id,
                appointment_id=None,
                run_id=ctx.run_id,
                tool_name="update_marketing_consent",
                action_type="marketing_consent_update",
                status="success",
                input_json={"consent": bool(consent)},
                output_json=payload,
            )
        )
        ctx.db.commit()
        return _json(payload)

    @tool
    def search_services(search: str = "") -> str:
        """Search active clinic services and return real names, prices, durations, and medical-review flags."""
        search = search.strip()
        stmt = select(Service).where(
            Service.workspace_id == ctx.workspace.id,
            Service.is_active.is_(True),
        )
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                or_(
                    Service.name.ilike(pattern),
                    Service.category.ilike(pattern),
                    Service.description.ilike(pattern),
                )
            )
        rows = list(ctx.db.scalars(stmt.order_by(Service.name).limit(20)))
        services = [
            {
                "service_id": str(row.id),
                "name": row.name,
                "category": row.category,
                "description": row.description,
                "duration_minutes": row.duration_minutes,
                "price": _money(row.price_minor, row.currency),
                "price_minor": row.price_minor,
                "currency": row.currency,
                "requires_medical_review": row.requires_medical_review,
            }
            for row in rows
        ]
        payload = {"ok": True, "services": services}
        _record_action(
            ctx,
            tool_name="search_services",
            action_type="clinic_read",
            status="success",
            input_payload={"search": search},
            output_payload=payload,
        )
        return _json(payload)

    @tool
    def list_branches() -> str:
        """List active clinic branches with their real location and contact details."""
        rows = _active_branches(ctx)
        branches = [
            {
                "branch_id": str(row.id),
                "name": row.name,
                "phone": row.phone,
                "address": _branch_display_address(row),
                "timezone": row.timezone or ctx.workspace.timezone,
            }
            for row in rows
        ]
        payload = {"ok": True, "branches": branches}
        _record_action(
            ctx,
            tool_name="list_branches",
            action_type="clinic_read",
            status="success",
            input_payload={},
            output_payload=payload,
        )
        return _json(payload)

    @tool
    def list_doctors(branch_id: str = "", service_id: str = "") -> str:
        """List active bookable doctors, optionally filtered by a branch ID and/or service ID returned by other tools."""
        try:
            branch_uuid = _uuid(branch_id, "branch_id") if branch_id else None
            service_uuid = _uuid(service_id, "service_id") if service_id else None

            stmt = (
                select(Doctor, Staff)
                .join(
                    Staff,
                    (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
                )
                .where(
                    Doctor.workspace_id == ctx.workspace.id,
                    Doctor.is_active.is_(True),
                    Doctor.booking_enabled.is_(True),
                    Staff.is_active.is_(True),
                )
            )
            if branch_uuid is not None:
                stmt = stmt.join(
                    DoctorBranch,
                    (DoctorBranch.workspace_id == Doctor.workspace_id)
                    & (DoctorBranch.doctor_id == Doctor.id),
                ).where(
                    DoctorBranch.branch_id == branch_uuid,
                    DoctorBranch.is_active.is_(True),
                )
            if service_uuid is not None:
                stmt = stmt.join(
                    DoctorService,
                    (DoctorService.workspace_id == Doctor.workspace_id)
                    & (DoctorService.doctor_id == Doctor.id),
                ).where(
                    DoctorService.service_id == service_uuid,
                    DoctorService.is_active.is_(True),
                )

            rows = list(ctx.db.execute(stmt.order_by(Staff.first_name, Staff.last_name)).all())
            doctors = []
            seen: set[UUID] = set()
            for doctor, staff in rows:
                if doctor.id in seen:
                    continue
                seen.add(doctor.id)
                doctors.append(
                    {
                        "doctor_id": str(doctor.id),
                        "name": f"{staff.first_name} {staff.last_name}".strip(),
                        "specialization": doctor.specialization,
                    }
                )
            payload = {"ok": True, "doctors": doctors}
            _record_action(
                ctx,
                tool_name="list_doctors",
                action_type="clinic_read",
                status="success",
                input_payload={"branch_id": branch_id, "service_id": service_id},
                output_payload=payload,
            )
            return _json(payload)
        except ValueError as exc:
            payload = {"ok": False, "error": str(exc)}
            _record_action(
                ctx,
                tool_name="list_doctors",
                action_type="clinic_read",
                status="error",
                input_payload={"branch_id": branch_id, "service_id": service_id},
                output_payload=payload,
                error_message=str(exc),
            )
            return _json(payload)

    @tool
    def get_booking_options(
        booking_date: str,
        service_id: str = "",
        branch_id: str = "",
        doctor_id: str = "",
        laser_device_key: str = "",
        requested_start_time: str = "",
        not_before_time: str = "",
        not_after_time: str = "",
        service_search: str = "",
        branch_search: str = "",
        doctor_search: str = "",
    ) -> str:
        """High-level booking discovery tool.

        Prefer this tool for requests like:
        - "عايزة أحجز ليزر بكرة الساعة 6" => requested_start_time="18:00"
        - "عايزة أحجز ليزر بكرة بعد 6" => not_before_time="18:00"
        - "فيه مواعيد ليزر بكرة من 8 لحد 9؟" => both time bounds

        Exact requested starts are separate from broad time windows. Never encode
        an exact start by setting not_before_time == not_after_time.

        In the grounded runtime, service_id / branch_id / doctor_id are canonical
        PostgreSQL IDs selected by the LLM from the catalog snapshot. Exact IDs
        bypass all text lookup logic. Legacy *_search fields remain only for the
        rollback path and are not used by the grounded unified customer runtime.
        """
        inputs = {
            "service_id": service_id,
            "service_search": service_search,
            "booking_date": booking_date,
            "requested_start_time": requested_start_time,
            "not_before_time": not_before_time,
            "not_after_time": not_after_time,
            "branch_id": branch_id,
            "doctor_id": doctor_id,
            "laser_device_key": laser_device_key or None,
            "branch_search": branch_search,
            "doctor_search": doctor_search,
        }

        try:
            requested_date = date.fromisoformat(booking_date)
            requested_start = (
                time.fromisoformat(requested_start_time)
                if requested_start_time
                else None
            )
            lower_bound = time.fromisoformat(not_before_time) if not_before_time else None
            upper_bound = time.fromisoformat(not_after_time) if not_after_time else None

            if lower_bound and upper_bound and lower_bound > upper_bound:
                raise ValueError("not_before_time cannot be later than not_after_time.")

            if service_id:
                service_uuid = _uuid(service_id, "service_id")
                service = ctx.db.scalar(
                    select(Service).where(
                        Service.workspace_id == ctx.workspace.id,
                        Service.id == service_uuid,
                        Service.is_active.is_(True),
                    )
                )
                services = [service] if service is not None else []
            elif service_search.strip():
                services = _service_matches(ctx, service_search)
            else:
                services = []

            if not services:
                payload = {
                    "ok": False,
                    "reason": "service_not_found",
                    "message": "No active service matched the grounded selection.",
                    "service_id": service_id or None,
                    "service_search": service_search or None,
                }
                _record_action(
                    ctx,
                    tool_name="get_booking_options",
                    action_type="booking_discovery",
                    status="success",
                    input_payload=inputs,
                    output_payload=payload,
                )
                return _json(payload)

            if len(services) > 1:
                payload = {
                    "ok": True,
                    "needs_service_choice": True,
                    "services": [
                        {
                            "service_id": str(service.id),
                            "service_name": service.name,
                            "category": service.category,
                            "duration_minutes": service.duration_minutes,
                            "price": _money(service.price_minor, service.currency),
                        }
                        for service in services[:8]
                    ],
                }
                _record_action(
                    ctx,
                    tool_name="get_booking_options",
                    action_type="booking_discovery",
                    status="success",
                    input_payload=inputs,
                    output_payload=payload,
                )
                return _json(payload)

            service = services[0]

            if branch_id:
                branch_uuid = _uuid(branch_id, "branch_id")
                selected_branch = ctx.db.scalar(
                    select(Branch).where(
                        Branch.workspace_id == ctx.workspace.id,
                        Branch.id == branch_uuid,
                        Branch.is_active.is_(True),
                        *([Branch.id == ctx.workspace.primary_branch_id] if ctx.workspace.primary_branch_id else []),
                    )
                )
                branches = [selected_branch] if selected_branch is not None else []
                if not branches:
                    raise BookingRuleError("Branch not found or inactive.")
            else:
                branches = _active_branches(ctx)

            if not branch_id and branch_search.strip():
                branches = _resolve_branch_by_search(branches, branch_search)
                if not branches:
                    payload = {
                        "ok": False,
                        "reason": "branch_not_found",
                        "message": "No active branch matched the customer's requested branch.",
                        "branch_search": branch_search,
                    }
                    _record_action(
                        ctx,
                        tool_name="get_booking_options",
                        action_type="booking_discovery",
                        status="success",
                        input_payload=inputs,
                        output_payload=payload,
                    )
                    return _json(payload)
            elif not branch_id and ctx.patient.preferred_branch_id:
                preferred = [
                    branch for branch in branches if branch.id == ctx.patient.preferred_branch_id
                ]
                if preferred:
                    branches = preferred

            if not branches:
                payload = {
                    "ok": False,
                    "reason": "no_active_branch",
                    "message": "No active clinic branch is available.",
                }
                _record_action(
                    ctx,
                    tool_name="get_booking_options",
                    action_type="booking_discovery",
                    status="success",
                    input_payload=inputs,
                    output_payload=payload,
                )
                return _json(payload)

            if len(branches) > 1:
                payload = {
                    "ok": True,
                    "needs_branch_choice": True,
                    "service": {
                        "service_id": str(service.id),
                        "service_name": service.name,
                    },
                    "branches": [
                        {
                            "branch_id": str(branch.id),
                            "branch_name": branch.name,
                            "city": branch.city,
                            "address": _branch_display_address(branch),
                        }
                        for branch in branches[:8]
                    ],
                }
                _record_action(
                    ctx,
                    tool_name="get_booking_options",
                    action_type="booking_discovery",
                    status="success",
                    input_payload=inputs,
                    output_payload=payload,
                )
                return _json(payload)

            branch = branches[0]
            doctor_uuid = _uuid(doctor_id, "doctor_id") if doctor_id else None
            selected_doctor: dict[str, str | None] | None = None

            if doctor_uuid is None and doctor_search.strip():
                doctor_rows = _active_booking_doctor_rows(
                    ctx,
                    branch_id=branch.id,
                    service_id=service.id,
                )
                matches = _resolve_doctor_rows_by_search(doctor_rows, doctor_search)

                if not matches:
                    payload = {
                        "ok": False,
                        "reason": "doctor_not_found",
                        "message": (
                            "No active bookable doctor matched the customer's requested doctor "
                            "for this branch and service."
                        ),
                        "doctor_search": doctor_search,
                    }
                    _record_action(
                        ctx,
                        tool_name="get_booking_options",
                        action_type="booking_discovery",
                        status="success",
                        input_payload=inputs,
                        output_payload=payload,
                    )
                    return _json(payload)

                if len(matches) > 1:
                    payload = {
                        "ok": True,
                        "needs_service_choice": False,
                        "needs_branch_choice": False,
                        "needs_doctor_choice": True,
                        "service": {
                            "service_id": str(service.id),
                            "service_name": service.name,
                        },
                        "branch": {
                            "branch_id": str(branch.id),
                            "branch_name": branch.name,
                        },
                        "doctors": [
                            {
                                "doctor_id": str(doctor.id),
                                "doctor_name": _doctor_display_name(staff),
                                "specialization": doctor.specialization,
                            }
                            for doctor, staff in matches[:8]
                        ],
                    }
                    _record_action(
                        ctx,
                        tool_name="get_booking_options",
                        action_type="booking_discovery",
                        status="success",
                        input_payload=inputs,
                        output_payload=payload,
                    )
                    return _json(payload)

                doctor, staff = matches[0]
                doctor_uuid = doctor.id
                selected_doctor = {
                    "doctor_id": str(doctor.id),
                    "doctor_name": _doctor_display_name(staff),
                    "specialization": doctor.specialization,
                }

            availability = _availability_payload(
                ctx,
                branch_id=str(branch.id),
                service_id=str(service.id),
                booking_date=requested_date,
                doctor_id=str(doctor_uuid) if doctor_uuid is not None else None,
                requested_start=requested_start,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                laser_device_key=laser_device_key or None,
            )

            payload = {
                "ok": True,
                "needs_service_choice": False,
                "needs_branch_choice": False,
                "needs_doctor_choice": False,
                "doctor": selected_doctor,
                "requested_time_window": {
                    "not_before_time": not_before_time or None,
                    "not_after_time": not_after_time or None,
                },
                **availability,
            }
            _record_action(
                ctx,
                tool_name="get_booking_options",
                action_type="booking_discovery",
                status="success",
                input_payload=inputs,
                output_payload=payload,
            )
            return _json(payload)

        except (ValueError, BookingRuleError) as exc:
            payload = {"ok": False, "error": str(exc)}
            _record_action(
                ctx,
                tool_name="get_booking_options",
                action_type="booking_discovery",
                status="error",
                input_payload=inputs,
                output_payload=payload,
                error_message=str(exc),
            )
            return _json(payload)

    @tool
    def get_reschedule_options(
        booking_date: str,
        appointment_id: str = "",
        service_id: str = "",
        doctor_id: str = "",
        laser_device_key: str = "",
        service_search: str = "",
        requested_start_time: str = "",
        not_before_time: str = "",
        not_after_time: str = "",
    ) -> str:
        """High-level rescheduling discovery tool.

        Prefer this tool when the customer wants to move an existing appointment.
        Appointment reads and replacement availability both cross the ClinicAdapter
        boundary, so the tool never depends on the native Appointment table.
        """
        ticket_state = _confirmation_ticket_reschedule_state(ctx)
        if ticket_state is not None:
            appointment_id = str(ticket_state["appointment_id"])
            service_id = str(ticket_state["locked_service_id"])
            locked_device = ticket_state.get("locked_laser_device_key")
            laser_device_key = str(locked_device) if locked_device is not None else ""
            service_search = ""

        inputs = {
            "booking_date": booking_date,
            "appointment_id": appointment_id or None,
            "service_id": service_id,
            "doctor_id": doctor_id or None,
            "laser_device_key": laser_device_key or None,
            "service_search": service_search,
            "requested_start_time": requested_start_time,
            "not_before_time": not_before_time,
            "not_after_time": not_after_time,
        }

        try:
            requested_date = date.fromisoformat(booking_date)
            requested_start = (
                time.fromisoformat(requested_start_time)
                if requested_start_time
                else None
            )
            lower_bound = time.fromisoformat(not_before_time) if not_before_time else None
            upper_bound = time.fromisoformat(not_after_time) if not_after_time else None

            if lower_bound and upper_bound and lower_bound > upper_bound:
                raise ValueError("not_before_time cannot be later than not_after_time.")

            appointment_result = _adapter_patient_appointments(ctx, include_past=False)
            appointments = [
                appointment
                for appointment in appointment_result.appointments
                if appointment.status in {"pending", "confirmed"}
            ]
            if appointment_id:
                appointments = [
                    appointment
                    for appointment in appointments
                    if appointment.appointment_id == appointment_id
                ]

            if service_id and not appointment_id:
                appointments = [
                    appointment
                    for appointment in appointments
                    if appointment.service_id == service_id
                ]
            elif service_search.strip() and not appointment_id:
                # Legacy rollback path only. Grounded unified turns pass service_id.
                matching_service_ids = {
                    str(service.id) for service in _service_matches(ctx, service_search)
                }
                appointments = [
                    appointment
                    for appointment in appointments
                    if appointment.service_id in matching_service_ids
                ]

            appointments = appointments[:10]
            if not appointments:
                payload = {
                    "ok": False,
                    "reason": "appointment_not_found",
                    "message": "No matching upcoming appointment was found.",
                }
                _record_action(
                    ctx,
                    tool_name="get_reschedule_options",
                    action_type="appointment_reschedule_discovery",
                    status="success",
                    input_payload=inputs,
                    output_payload=payload,
                )
                return _json(payload)

            if len(appointments) > 1:
                payload = {
                    "ok": True,
                    "needs_appointment_choice": True,
                    "appointments": [
                        _canonical_appointment_summary(appointment)
                        for appointment in appointments[:8]
                    ],
                }
                _record_action(
                    ctx,
                    tool_name="get_reschedule_options",
                    action_type="appointment_reschedule_discovery",
                    status="success",
                    input_payload=inputs,
                    output_payload=payload,
                )
                return _json(payload)

            current = appointments[0]
            target_service_id = service_id or current.service_id
            if ticket_state is not None and target_service_id != str(current.service_id):
                raise BookingRuleError("The confirmation-ticket reschedule must keep the original service.")
            target_device_key = laser_device_key or current.laser_device_key
            if ticket_state is not None:
                target_device_key = ticket_state.get("locked_laser_device_key")
            availability = _availability_payload(
                ctx,
                branch_id=current.branch_id,
                service_id=target_service_id,
                booking_date=requested_date,
                doctor_id=doctor_id or current.doctor_id,
                requested_start=requested_start,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                exclude_appointment_id=current.appointment_id,
                laser_device_key=target_device_key,
            )

            payload = {
                "ok": True,
                "needs_appointment_choice": False,
                "current_appointment": _canonical_appointment_summary(current),
                "requested_time_window": {
                    "not_before_time": not_before_time or None,
                    "not_after_time": not_after_time or None,
                },
                **availability,
            }
            _record_action(
                ctx,
                tool_name="get_reschedule_options",
                action_type="appointment_reschedule_discovery",
                status="success",
                input_payload=inputs,
                output_payload=payload,
                appointment_id=_native_action_appointment_id(current.appointment_id),
            )
            return _json(payload)

        except (ValueError, BookingRuleError) as exc:
            payload = {"ok": False, "error": str(exc)}
            _record_action(
                ctx,
                tool_name="get_reschedule_options",
                action_type="appointment_reschedule_discovery",
                status="error",
                input_payload=inputs,
                output_payload=payload,
                error_message=str(exc),
            )
            return _json(payload)

    @tool
    def get_available_slots(
        branch_id: str,
        service_id: str,
        booking_date: str,
        doctor_id: str = "",
        requested_start_time: str = "",
        not_before_time: str = "",
        not_after_time: str = "",
    ) -> str:
        """Get real available appointment slots.

        booking_date must be YYYY-MM-DD.
        IDs must come from clinic tools.

        IMPORTANT time rule:
        - Exact "at 6 PM" / "الساعة 6" => requested_start_time="18:00".
        - "بعد 6 مساءً" / "from 6 PM" => not_before_time="18:00".
        - If the customer gives an upper time limit, pass not_after_time in local 24-hour HH:MM.
        - Never encode an exact start as equal lower/upper bounds.
        - Do not fetch unfiltered slots and then guess whether a requested time window is available.
        """
        inputs = {
            "branch_id": branch_id,
            "service_id": service_id,
            "booking_date": booking_date,
            "doctor_id": doctor_id,
            "requested_start_time": requested_start_time,
            "not_before_time": not_before_time,
            "not_after_time": not_after_time,
        }
        try:
            requested_date = date.fromisoformat(booking_date)

            requested_start = time.fromisoformat(requested_start_time) if requested_start_time else None
            lower_bound = time.fromisoformat(not_before_time) if not_before_time else None
            upper_bound = time.fromisoformat(not_after_time) if not_after_time else None

            if lower_bound and upper_bound and lower_bound > upper_bound:
                raise ValueError("not_before_time cannot be later than not_after_time.")

            availability = _adapter_availability(
                ctx,
                branch_id=branch_id,
                service_id=service_id,
                booking_date=requested_date,
                doctor_id=doctor_id or None,
            )
            timezone_name = availability.timezone
            slots = list(availability.slots)
            tz = ZoneInfo(timezone_name)

            filtered_slots = _filter_slots_by_local_window(
                slots,
                tz=tz,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
            )
            if requested_start is not None:
                filtered_slots = [
                    (slot, local_start)
                    for slot, local_start in filtered_slots
                    if local_start.timetz().replace(tzinfo=None) == requested_start
                ]

            result_slots = []
            for slot, local_start in filtered_slots[:12]:
                local_end = slot.end_at.astimezone(tz)
                result_slots.append(
                    {
                        "branch_id": slot.branch_id,
                        "branch_name": slot.branch_name,
                        "doctor_id": slot.doctor_id,
                        "doctor_name": slot.doctor_name or "الدكتور المتاح",
                        "service_id": slot.service_id,
                        "service_name": slot.service_name,
                        "start_local": local_start.isoformat(),
                        "end_local": local_end.isoformat(),
                        "start_time_24h": local_start.strftime("%H:%M"),
                        "end_time_24h": local_end.strftime("%H:%M"),
                        "timezone": timezone_name,
                        "price": _money(slot.price_minor, slot.currency),
                        "laser_device_key": getattr(slot, "laser_device_key", None),
                        "laser_device_name": getattr(slot, "laser_device_name", None),
                    }
                )

            payload = {
                "ok": True,
                "date": booking_date,
                "timezone": timezone_name,
                "requested_start_time": requested_start_time or None,
                "requested_time_window": {
                    "not_before_time": not_before_time or None,
                    "not_after_time": not_after_time or None,
                },
                "slots": result_slots,
                "matching_slot_count": len(filtered_slots),
                "more_slots_available": len(filtered_slots) > len(result_slots),
            }
            _record_action(
                ctx,
                tool_name="get_available_slots",
                action_type="availability_read",
                status="success",
                input_payload=inputs,
                output_payload=payload,
            )
            return _json(payload)
        except (ValueError, BookingRuleError) as exc:
            payload = {"ok": False, "error": str(exc)}
            _record_action(
                ctx,
                tool_name="get_available_slots",
                action_type="availability_read",
                status="error",
                input_payload=inputs,
                output_payload=payload,
                error_message=str(exc),
            )
            return _json(payload)

    @tool
    def get_customer_appointments(include_past: bool = False) -> str:
        """Get the current customer's real appointments. Use before answering questions about their bookings."""
        result = _adapter_patient_appointments(ctx, include_past=include_past)
        appointments = [
            _canonical_appointment_summary(row) for row in result.appointments
        ]
        payload = {"ok": True, "appointments": appointments}
        _record_action(
            ctx,
            tool_name="get_customer_appointments",
            action_type="booking_read",
            status="success",
            input_payload={"include_past": include_past},
            output_payload=payload,
        )
        return _json(payload)

    @tool
    def get_customer_packages(service_id: str = "") -> str:
        """List this customer's prepaid packages and remaining sessions. Use when the customer asks to use a package or bundle."""
        parsed_service_id = _uuid(service_id, "service_id") if service_id.strip() else None
        packages = list_patient_packages(
            ctx.db,
            workspace_id=ctx.workspace.id,
            patient_id=ctx.patient.id,
            service_id=parsed_service_id,
            usable_only=False,
        )
        payload = {
            "ok": True,
            "packages": [item.model_dump(mode="json") for item in packages],
        }
        _record_action(
            ctx,
            tool_name="get_customer_packages",
            action_type="package_read",
            status="success",
            input_payload={"service_id": service_id or None},
            output_payload=payload,
        )
        return _json(payload)


    @tool
    def book_appointment(
        branch_id: str,
        service_id: str,
        doctor_id: str,
        start_at: str,
        customer_note: str = "",
        patient_package_id: str = "",
    ) -> str:
        """Create a real appointment after the customer has clearly chosen an exact offered slot.

        The selected canonical IDs and exact start are passed to the clinic adapter,
        which must revalidate the slot against the clinic source of truth before writing.
        """
        inputs = {
            "branch_id": branch_id,
            "service_id": service_id,
            "doctor_id": doctor_id,
            "start_at": start_at,
            "customer_note": customer_note,
            "patient_package_id": patient_package_id or None,
        }
        try:
            if ctx.patient.status == "blocked":
                raise BookingRuleError("This customer is blocked from new appointments.")
            requested_start = datetime.fromisoformat(start_at)
            if requested_start.tzinfo is None or requested_start.utcoffset() is None:
                raise ValueError(
                    "start_at must include the timezone offset from availability results."
                )

            adapter = get_clinic_adapter(db=ctx.db, workspace=ctx.workspace)
            adapter.require_capability(ClinicCapability.APPOINTMENTS_CREATE)
            result = adapter.create_appointment(
                CreateAppointmentRequest(
                    patient_id=str(ctx.patient.id),
                    branch_id=branch_id,
                    service_id=service_id,
                    doctor_id=doctor_id,
                    start_at=requested_start,
                    operation_id=str(ctx.run_id),
                    customer_note=customer_note,
                    patient_package_id=patient_package_id.strip() or None,
                    laser_device_key=current_laser_device_key(),
                )
            )
            try:
                local_appointment_id = UUID(str(result.appointment.appointment_id))
            except (TypeError, ValueError):
                local_appointment_id = None
            if local_appointment_id is not None:
                record_direct_campaign_booking_conversion(
                    ctx.db,
                    workspace_id=ctx.workspace.id,
                    patient_id=ctx.patient.id,
                    conversation_id=ctx.conversation.id,
                    appointment_id=local_appointment_id,
                )
            payload = {
                "ok": True,
                "appointment": _canonical_appointment_summary(result.appointment),
            }
            _record_action(
                ctx,
                tool_name="book_appointment",
                action_type="appointment_create",
                status="success",
                input_payload=inputs,
                output_payload=payload,
                appointment_id=_native_action_appointment_id(
                    result.appointment.appointment_id
                ),
            )
            return _json(payload)
        except (ValueError, BookingRuleError, IntegrityError) as exc:
            ctx.db.rollback()
            payload = {"ok": False, "error": str(exc)}
            _record_action(
                ctx,
                tool_name="book_appointment",
                action_type="appointment_create",
                status="error",
                input_payload=inputs,
                output_payload=payload,
                error_message=str(exc),
            )
            return _json(payload)

    @tool
    def confirm_appointment(appointment_id: str) -> str:
        """Confirm one of the current customer's pending appointments."""
        inputs = {"appointment_id": appointment_id}
        try:
            adapter = get_clinic_adapter(db=ctx.db, workspace=ctx.workspace)
            adapter.require_capability(ClinicCapability.APPOINTMENTS_CONFIRM)
            result = adapter.confirm_appointment(
                ConfirmAppointmentRequest(
                    patient_id=str(ctx.patient.id),
                    appointment_id=appointment_id,
                    operation_id=str(ctx.run_id),
                )
            )
            payload = {
                "ok": True,
                "appointment": _canonical_appointment_summary(result.appointment),
            }
            _record_action(
                ctx,
                tool_name="confirm_appointment",
                action_type="appointment_confirm",
                status="success",
                input_payload=inputs,
                output_payload=payload,
                appointment_id=_native_action_appointment_id(
                    result.appointment.appointment_id
                ),
            )
            return _json(payload)
        except (ValueError, BookingRuleError) as exc:
            ctx.db.rollback()
            payload = {"ok": False, "error": str(exc)}
            _record_action(
                ctx,
                tool_name="confirm_appointment",
                action_type="appointment_confirm",
                status="error",
                input_payload=inputs,
                output_payload=payload,
                error_message=str(exc),
            )
            return _json(payload)

    @tool
    def cancel_appointment(appointment_id: str, reason: str = "") -> str:
        """Cancel the current customer's appointment when clinic policy allows it.

        Reason is optional. The clinic adapter owns cancellation policy; if staff
        approval is required, the tool converts that decision into Linka handoff state.
        """
        reason = reason.strip() or "customer_requested"
        inputs = {"appointment_id": appointment_id, "reason": reason}
        try:
            adapter = get_clinic_adapter(db=ctx.db, workspace=ctx.workspace)
            adapter.require_capability(ClinicCapability.APPOINTMENTS_CANCEL)
            result = adapter.cancel_appointment(
                CancelAppointmentRequest(
                    patient_id=str(ctx.patient.id),
                    appointment_id=appointment_id,
                    operation_id=str(ctx.run_id),
                    reason=reason,
                )
            )
            payload = {
                "ok": True,
                "appointment": _canonical_appointment_summary(result.appointment),
            }
            _record_action(
                ctx,
                tool_name="cancel_appointment",
                action_type="appointment_cancel",
                status="success",
                input_payload=inputs,
                output_payload=payload,
                appointment_id=_native_action_appointment_id(
                    result.appointment.appointment_id
                ),
            )
            return _json(payload)
        except ClinicActionRequiresHuman as exc:
            ctx.db.rollback()
            handoff = _set_handoff(
                ctx,
                reason=str(exc) or "Clinic cancellation policy requires staff approval.",
                category="booking_exception",
                priority="normal",
            )
            payload = {
                "ok": False,
                "requires_human": True,
                "handoff_id": str(handoff.id),
                "handoff_category": handoff.category,
                "handoff_priority": handoff.priority,
                "error": str(exc),
            }
            _record_action(
                ctx,
                tool_name="cancel_appointment",
                action_type="appointment_cancel",
                status="blocked",
                input_payload=inputs,
                output_payload=payload,
                appointment_id=_native_action_appointment_id(exc.appointment_id),
                error_message=str(exc),
            )
            return _json(payload)
        except (ValueError, BookingRuleError) as exc:
            ctx.db.rollback()
            payload = {"ok": False, "error": str(exc)}
            _record_action(
                ctx,
                tool_name="cancel_appointment",
                action_type="appointment_cancel",
                status="error",
                input_payload=inputs,
                output_payload=payload,
                error_message=str(exc),
            )
            return _json(payload)

    @tool
    def reschedule_appointment(
        appointment_id: str,
        start_at: str,
        branch_id: str = "",
        doctor_id: str = "",
        service_id: str = "",
        reason: str = "",
    ) -> str:
        """Reschedule the customer's pending/confirmed appointment to an exact slot."""
        ticket_state = _confirmation_ticket_reschedule_state(ctx)
        if ticket_state is not None:
            appointment_id = str(ticket_state["appointment_id"])
            service_id = str(ticket_state["locked_service_id"])
            locked_device_key = ticket_state.get("locked_laser_device_key")
        else:
            locked_device_key = current_laser_device_key()
        inputs = {
            "appointment_id": appointment_id,
            "start_at": start_at,
            "branch_id": branch_id,
            "doctor_id": doctor_id,
            "service_id": service_id,
            "reason": reason,
        }
        try:
            requested_start = datetime.fromisoformat(start_at)
            if requested_start.tzinfo is None or requested_start.utcoffset() is None:
                raise ValueError(
                    "start_at must include the timezone offset from availability results."
                )

            adapter = get_clinic_adapter(db=ctx.db, workspace=ctx.workspace)
            adapter.require_capability(ClinicCapability.APPOINTMENTS_RESCHEDULE)
            result = adapter.reschedule_appointment(
                RescheduleAppointmentRequest(
                    patient_id=str(ctx.patient.id),
                    appointment_id=appointment_id,
                    start_at=requested_start,
                    operation_id=str(ctx.run_id),
                    branch_id=branch_id or None,
                    doctor_id=doctor_id or None,
                    service_id=service_id or None,
                    laser_device_key=locked_device_key,
                    reason=reason,
                )
            )
            payload = {
                "ok": True,
                "appointment": _canonical_appointment_summary(result.appointment),
            }
            action_payload = dict(payload)
            if result.previous_appointment_id:
                action_payload["previous_appointment_id"] = result.previous_appointment_id
            _record_action(
                ctx,
                tool_name="reschedule_appointment",
                action_type="appointment_reschedule",
                status="success",
                input_payload=inputs,
                output_payload=action_payload,
                appointment_id=_native_action_appointment_id(
                    result.appointment.appointment_id
                ),
            )
            return _json(payload)
        except ClinicActionRequiresHuman as exc:
            ctx.db.rollback()
            payload = {
                "ok": False,
                "requires_human": True,
                "handoff_category": "payment",
                "handoff_priority": "normal",
                "error": str(exc),
            }
            _record_action(
                ctx,
                tool_name="reschedule_appointment",
                action_type="appointment_reschedule",
                status="blocked",
                input_payload=inputs,
                output_payload=payload,
                appointment_id=_native_action_appointment_id(exc.appointment_id or appointment_id),
                error_message=str(exc),
            )
            return _json(payload)
        except (ValueError, BookingRuleError, IntegrityError) as exc:
            ctx.db.rollback()
            payload = {"ok": False, "error": str(exc)}
            _record_action(
                ctx,
                tool_name="reschedule_appointment",
                action_type="appointment_reschedule",
                status="error",
                input_payload=inputs,
                output_payload=payload,
                error_message=str(exc),
            )
            return _json(payload)

    @tool
    def create_follow_up_task(
        due_at_local: str,
        title: str = "متابعة مع العميل",
        note: str = "",
    ) -> str:
        """Schedule one automatic Linka follow-up for the current patient.

        Use only when the customer clearly asks to be contacted/reminded later, or
        when the current conversation explicitly agrees on a future follow-up.
        due_at_local must be a specific future clinic-local datetime such as
        2026-08-26T15:30. Natural-language interpretation belongs to the model;
        Python validates the resolved time, patient, conversation, ownership, and
        idempotency. At the due time Linka composes a fresh natural WhatsApp message
        from the latest conversation context and sends it through the normal outbox.
        """
        inputs = {"due_at_local": due_at_local, "title": title, "note": note}
        try:
            raw_due = datetime.fromisoformat(due_at_local.strip())
            timezone_name = ctx.workspace.timezone or "Africa/Cairo"
            try:
                clinic_tz = ZoneInfo(timezone_name)
            except Exception:
                clinic_tz = ZoneInfo("Africa/Cairo")
            due = (
                raw_due.replace(tzinfo=clinic_tz)
                if raw_due.tzinfo is None or raw_due.utcoffset() is None
                else raw_due.astimezone(clinic_tz)
            )
            now_local = datetime.now(clinic_tz)
            if due <= now_local:
                raise CRMTaskError("Follow-up time must be in the future.")
            if due > now_local + timedelta(days=366):
                raise CRMTaskError("Follow-up time cannot be more than 366 days ahead.")

            lead = ctx.db.scalar(
                select(Lead)
                .where(
                    Lead.workspace_id == ctx.workspace.id,
                    Lead.patient_id == ctx.patient.id,
                    Lead.status.in_(("new", "contacted", "qualified", "booked")),
                )
                .order_by(
                    Lead.assigned_user_id.is_(None),
                    Lead.updated_at.desc(),
                )
                .limit(1)
            )
            task = create_crm_task(
                ctx.db,
                workspace_id=ctx.workspace.id,
                patient_id=ctx.patient.id,
                lead_id=lead.id if lead else None,
                conversation_id=ctx.conversation.id,
                assigned_user_id=None,
                created_by_user_id=None,
                task_type="follow_up",
                priority="normal",
                title=title,
                description=note or None,
                due_at=due,
                source="ai",
                execution_mode="ai",
                dedupe_key=f"agent:{ctx.run_id}:follow_up",
                commit=False,
            )
            payload = {
                "ok": True,
                "task_id": str(task.id),
                "task_type": task.task_type,
                "status": task.status,
                "due_at": task.due_at.isoformat(),
                "assigned_user_id": str(task.assigned_user_id) if task.assigned_user_id else None,
                "execution_mode": task.execution_mode,
                "title": task.title,
            }
            _record_action(
                ctx,
                tool_name="create_follow_up_task",
                action_type="crm_follow_up_create",
                status="success",
                input_payload=inputs,
                output_payload=payload,
            )
            return _json(payload)
        except (ValueError, CRMTaskError) as exc:
            ctx.db.rollback()
            payload = {"ok": False, "error": str(exc)}
            _record_action(
                ctx,
                tool_name="create_follow_up_task",
                action_type="crm_follow_up_create",
                status="error",
                input_payload=inputs,
                output_payload=payload,
                error_message=str(exc),
            )
            return _json(payload)

    @tool
    def escalate_to_human(
        reason: str,
        category: str = "other",
        priority: str = "normal",
    ) -> str:
        """Transfer conversation ownership to clinic staff and create/reuse the human handoff queue item."""
        reason = reason.strip() or "human_handoff_requested"
        handoff = create_handoff(
            ctx.db,
            workspace_id=ctx.workspace.id,
            conversation=ctx.conversation,
            patient=ctx.patient,
            reason=reason,
            category=category,
            priority=priority,
            source="ai",
            handoff_context=ctx.handoff_context,
            commit=False,
        )
        payload = {
            "ok": True,
            "handoff_required": True,
            "handoff_id": str(handoff.id),
            "reason": reason,
            "category": handoff.category,
            "priority": handoff.priority,
            "owner_type": ctx.conversation.owner_type,
        }
        ctx.db.add(
            AgentAction(
                workspace_id=ctx.workspace.id,
                conversation_id=ctx.conversation.id,
                patient_id=ctx.patient.id,
                appointment_id=None,
                run_id=ctx.run_id,
                tool_name="escalate_to_human",
                action_type="human_handoff",
                status="success",
                input_json={
                    "reason": reason,
                    "category": handoff.category,
                    "priority": handoff.priority,
                },
                output_json=payload,
            )
        )
        ctx.db.commit()
        return _json(payload)

    return [
        get_customer_profile,
        get_customer_history,
        update_marketing_consent,
        search_services,
        list_branches,
        list_doctors,
        get_booking_options,
        get_reschedule_options,
        get_available_slots,
        get_customer_appointments,
        get_customer_packages,
        book_appointment,
        confirm_appointment,
        cancel_appointment,
        reschedule_appointment,
        create_follow_up_task,
        escalate_to_human,
    ]
