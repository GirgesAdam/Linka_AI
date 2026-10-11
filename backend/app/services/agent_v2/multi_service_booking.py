from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.patient import Patient
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.agent_v2.package_booking_policy import resolve_booking_package
from app.services.agent_v2.state import BookingProposal
from app.services.appointment_commerce import add_additional_service
from app.services.appointment_creation import create_appointment_operation
from app.services.appointment_operations import AppointmentOperationError
from app.services.booking import BookingRuleError, calculate_availability, resolve_timezone
from app.services.inventory import InventoryOperationError, configured_device_price


class MultiServiceBookingUnavailable(ValueError):
    def __init__(self, message: str, *, alternatives: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.alternatives = alternatives


class MultiServiceBookingError(ValueError):
    def __init__(self, message: str, *, dimension: str = "service") -> None:
        super().__init__(message)
        self.dimension = dimension


@dataclass(frozen=True)
class AdditionalServiceResolution:
    service_id: UUID
    device_key: str | None


def service_duration_minutes(catalog: dict[str, Any], service_id: str) -> int:
    services = catalog.get("services")
    if not isinstance(services, list):
        raise MultiServiceBookingError("Service catalog is unavailable.")
    for row in services:
        if not isinstance(row, dict) or str(row.get("id")) != str(service_id):
            continue
        try:
            duration = int(row.get("duration_minutes"))
        except (TypeError, ValueError) as exc:
            raise MultiServiceBookingError("Service duration is unavailable.") from exc
        if duration <= 0:
            raise MultiServiceBookingError("Service duration is invalid.")
        return duration
    raise MultiServiceBookingError("Requested service is not in the active catalog.")


def select_main_service_id(catalog: dict[str, Any], service_ids: list[str]) -> str:
    ordered = list(dict.fromkeys(str(item) for item in service_ids if str(item)))
    if not ordered:
        raise MultiServiceBookingError("At least one canonical service is required.")
    # Stable max: customer-request canonical order wins duration ties.
    return max(
        enumerate(ordered),
        key=lambda item: (service_duration_minutes(catalog, item[1]), -item[0]),
    )[1]


def _catalog_service_row(catalog: dict[str, Any], service_id: str) -> dict[str, Any]:
    services = catalog.get("services")
    if not isinstance(services, list):
        raise MultiServiceBookingError("Service catalog is unavailable.")
    for row in services:
        if not isinstance(row, dict):
            continue
        canonical_id = row.get("id") or row.get("service_id")
        if str(canonical_id or "") == str(service_id):
            return row
    raise MultiServiceBookingError("Requested service is not in the active catalog.")


def laser_service_ids(catalog: dict[str, Any], service_ids: list[str]) -> list[str]:
    ordered = list(dict.fromkeys(str(item) for item in service_ids if str(item)))
    return [
        service_id
        for service_id in ordered
        if _catalog_service_row(catalog, service_id).get("requires_laser_device") is True
    ]


def configured_laser_device_keys(catalog: dict[str, Any], service_id: str) -> list[str]:
    row = _catalog_service_row(catalog, service_id)
    if row.get("requires_laser_device") is not True:
        return []
    raw_devices = row.get("laser_devices")
    if not isinstance(raw_devices, list):
        return []
    keys: list[str] = []
    for raw in raw_devices:
        if not isinstance(raw, dict) or raw.get("configured") is False:
            continue
        key = str(raw.get("device_key") or "").strip()
        if not key:
            continue
        if raw.get("price_minor") is None and raw.get("price") in (None, ""):
            continue
        if raw.get("duration_minutes") is None:
            continue
        if key not in keys:
            keys.append(key)
    return keys


def common_laser_device_keys(catalog: dict[str, Any], service_ids: list[str]) -> list[str]:
    laser_ids = laser_service_ids(catalog, service_ids)
    if not laser_ids:
        return []
    first = configured_laser_device_keys(catalog, laser_ids[0])
    common = list(first)
    for service_id in laser_ids[1:]:
        allowed = set(configured_laser_device_keys(catalog, service_id))
        common = [device_key for device_key in common if device_key in allowed]
    return common


def validate_additional_service_compatibility(
    db: Session,
    *,
    workspace: Workspace,
    branch_id: UUID,
    doctor_id: UUID,
    service_id: UUID,
    device_key: str | None,
) -> AdditionalServiceResolution:
    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.id == service_id,
            Service.is_active.is_(True),
        )
    )
    if service is None:
        raise MultiServiceBookingError("Additional service is missing or inactive.")

    doctor = db.scalar(
        select(Doctor).where(
            Doctor.workspace_id == workspace.id,
            Doctor.id == doctor_id,
            Doctor.is_active.is_(True),
            Doctor.booking_enabled.is_(True),
        )
    )
    branch_assignment = db.scalar(
        select(DoctorBranch.id).where(
            DoctorBranch.workspace_id == workspace.id,
            DoctorBranch.doctor_id == doctor_id,
            DoctorBranch.branch_id == branch_id,
            DoctorBranch.is_active.is_(True),
        )
    )
    service_assignment = db.scalar(
        select(DoctorService.id).where(
            DoctorService.workspace_id == workspace.id,
            DoctorService.doctor_id == doctor_id,
            DoctorService.service_id == service_id,
            DoctorService.is_active.is_(True),
        )
    )
    if doctor is None or branch_assignment is None or service_assignment is None:
        raise MultiServiceBookingError(
            "The selected doctor cannot perform every requested service at this branch.",
            dimension="doctor",
        )

    normalized_device = (device_key or "").strip() or None
    if bool(service.requires_laser_device):
        if normalized_device is None:
            raise MultiServiceBookingError(
                "A compatible visit-level laser device must be selected for every requested laser service.",
                dimension="device",
            )
        try:
            configured_device_price(
                db,
                workspace_id=workspace.id,
                service_id=service.id,
                device_key=normalized_device,
            )
        except InventoryOperationError as exc:
            raise MultiServiceBookingError(str(exc), dimension="device") from exc
    else:
        normalized_device = None

    return AdditionalServiceResolution(service_id=service.id, device_key=normalized_device)


def resolve_additional_services(
    db: Session,
    *,
    workspace: Workspace,
    proposal: BookingProposal,
) -> list[AdditionalServiceResolution]:
    branch_id = UUID(proposal.branch_id)
    doctor_id = UUID(proposal.doctor_id)
    resolved: list[AdditionalServiceResolution] = []
    for raw_service_id in proposal.additional_service_ids:
        service_id = UUID(raw_service_id)
        resolved.append(
            validate_additional_service_compatibility(
                db,
                workspace=workspace,
                branch_id=branch_id,
                doctor_id=doctor_id,
                service_id=service_id,
                device_key=proposal.device_key,
            )
        )
    return resolved


def booking_proposal_alternatives(
    db: Session,
    *,
    workspace: Workspace,
    proposal: BookingProposal,
    now: datetime | None = None,
    horizon_days: int = 7,
    limit: int = 3,
) -> list[dict[str, str]]:
    branch = db.scalar(
        select(Branch).where(
            Branch.workspace_id == workspace.id,
            Branch.id == UUID(proposal.branch_id),
            Branch.is_active.is_(True),
        )
    )
    if branch is None:
        return []
    timezone = resolve_timezone(workspace, branch)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    first_date = proposal.start_at.astimezone(timezone).date()
    alternatives: list[dict[str, str]] = []
    for offset in range(horizon_days + 1):
        try:
            _, slots = calculate_availability(
                db,
                workspace,
                branch_id=UUID(proposal.branch_id),
                service_id=UUID(proposal.main_service_id),
                booking_date=first_date + timedelta(days=offset),
                doctor_id=UUID(proposal.doctor_id),
                now=current,
                laser_device_key=proposal.device_key,
            )
        except BookingRuleError:
            continue
        for slot in slots:
            if slot.start_at <= current or slot.start_at == proposal.start_at:
                continue
            local = slot.start_at.astimezone(timezone)
            alternatives.append(
                {
                    "start_at": slot.start_at.isoformat(),
                    "start_local": local.isoformat(),
                    "date": local.date().isoformat(),
                    "time": local.strftime("%H:%M"),
                }
            )
            if len(alternatives) >= limit:
                return alternatives
    return alternatives


def execute_booking_proposal(
    db: Session,
    *,
    workspace: Workspace,
    patient: Patient,
    proposal: BookingProposal,
    idempotency_key: str | None,
) -> dict[str, object]:
    if str(patient.id) != proposal.patient_id:
        raise MultiServiceBookingError("Booking proposal does not belong to this patient.")
    if patient.status == "blocked":
        raise MultiServiceBookingError("Blocked patients cannot receive new appointments.")

    # Resolve every additional service against the single visit-level device before
    # creating the main appointment. This fail-closed boundary prevents partial or
    # cross-device same-visit writes.
    additionals = resolve_additional_services(db, workspace=workspace, proposal=proposal)

    main_service_id = UUID(proposal.main_service_id)
    package_resolution = resolve_booking_package(
        db,
        workspace=workspace,
        patient=patient,
        service_id=main_service_id,
        start_at=proposal.start_at,
        device_key=proposal.device_key,
        package_usage=proposal.package_usage,
        requested_package_id=(UUID(proposal.package_id) if proposal.package_id else None),
    )
    try:
        appointment = create_appointment_operation(
            db,
            workspace=workspace,
            patient_id=patient.id,
            branch_id=UUID(proposal.branch_id),
            doctor_id=UUID(proposal.doctor_id),
            service_id=main_service_id,
            requested_start_at=proposal.start_at,
            created_by_user_id=None,
            patient_package_id=package_resolution.package_id,
            use_pulse_balance=False,
            visit_group_id=None,
            source="ai",
            laser_device_key=proposal.device_key,
            idempotency_key=(f"{idempotency_key}:main" if idempotency_key else None),
            actor_type="ai",
        )
    except AppointmentOperationError as exc:
        if isinstance(exc.__cause__, BookingRuleError):
            raise MultiServiceBookingUnavailable(
                "The proposed appointment time is no longer available.",
                alternatives=booking_proposal_alternatives(
                    db, workspace=workspace, proposal=proposal
                ),
            ) from exc
        raise

    lines = []
    for index, item in enumerate(additionals):
        line = add_additional_service(
            db,
            workspace_id=workspace.id,
            appointment_id=appointment.id,
            service_id=item.service_id,
            laser_device_key=item.device_key,
            created_by_user_id=None,
            actor_type="ai",
            idempotency_key=(
                f"{idempotency_key}:additional:{index}" if idempotency_key else None
            ),
        )
        lines.append(line)

    return {
        "ok": True,
        "write_kind": "booking_proposal",
        "appointment_id": str(appointment.id),
        "status": appointment.status,
        "main_service_id": str(appointment.service_id),
        "additional_service_ids": [str(line.service_id) for line in lines],
        "additional_service_row_ids": [str(line.id) for line in lines],
        "reserved_duration_minutes": int(appointment.duration_minutes),
        "proposal_id": proposal.proposal_id,
        "package_used": bool(package_resolution.package_used),
    }
