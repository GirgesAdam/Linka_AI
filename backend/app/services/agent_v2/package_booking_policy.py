from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.patient import Patient
from app.models.workspace import Workspace
from app.schemas.patient_packages import PatientPackageRead
from app.services.patient_packages import list_patient_packages


class BookingPackagePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class BookingPackageResolution:
    package_id: UUID | None
    package_name: str | None
    package_used: bool
    usage_mode: str


def _eligible_packages(
    db: Session,
    *,
    workspace: Workspace,
    patient: Patient,
    service_id: UUID,
    appointment_date: date,
    device_key: str | None,
) -> list[PatientPackageRead]:
    rows = list_patient_packages(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service_id,
        usable_only=True,
        on_date=appointment_date,
        include_financials=False,
    )
    eligible = []
    for row in rows:
        package_device = str(row.laser_device_key) if row.laser_device_key else None
        if device_key is not None:
            if package_device != device_key:
                continue
        elif package_device is not None:
            continue
        eligible.append(row)
    return eligible


def _selection_key(package: PatientPackageRead) -> tuple[date, object, str]:
    # Consume the entitlement that expires first. If expiry is equal/absent,
    # prefer the older purchase so equivalent packages are handled predictably.
    expiry = package.expires_at or date.max
    return (expiry, package.purchased_at, str(package.id))


def resolve_booking_package(
    db: Session,
    *,
    workspace: Workspace,
    patient: Patient,
    service_id: UUID,
    start_at,
    device_key: str | None,
    package_usage: str | None,
    requested_package_id: UUID | None = None,
) -> BookingPackageResolution:
    """Resolve package consumption for one already-verified V2 booking.

    Policy:
    - unspecified/missing => automatically use a compatible active package when available;
    - use_existing => a compatible package is mandatory, never silently fall back;
    - avoid_existing => explicitly book a standalone session.

    The LLM never selects a database package id. Python resolves eligible packages from
    canonical patient/package data and the canonical appointment service/device/date.
    """
    usage_mode = package_usage or "unspecified"
    if usage_mode not in {"unspecified", "use_existing", "avoid_existing"}:
        raise BookingPackagePolicyError("Unsupported package usage policy for booking.")

    if usage_mode == "avoid_existing":
        if requested_package_id is not None:
            raise BookingPackagePolicyError(
                "Standalone booking cannot also require an existing package."
            )
        return BookingPackageResolution(
            package_id=None,
            package_name=None,
            package_used=False,
            usage_mode=usage_mode,
        )

    timezone_name = str(getattr(workspace, "timezone", None) or "UTC")
    local_date = start_at.astimezone(ZoneInfo(timezone_name)).date()
    eligible = _eligible_packages(
        db,
        workspace=workspace,
        patient=patient,
        service_id=service_id,
        appointment_date=local_date,
        device_key=device_key,
    )

    if requested_package_id is not None:
        requested = next((row for row in eligible if row.id == requested_package_id), None)
        if requested is None:
            raise BookingPackagePolicyError(
                "The requested package is not usable for this service, device, or appointment date."
            )
        return BookingPackageResolution(
            package_id=requested.id,
            package_name=requested.name,
            package_used=True,
            usage_mode=usage_mode,
        )

    if not eligible:
        if usage_mode == "use_existing":
            raise BookingPackagePolicyError(
                "No usable existing package matches this service, device, and appointment date."
            )
        return BookingPackageResolution(
            package_id=None,
            package_name=None,
            package_used=False,
            usage_mode=usage_mode,
        )

    if usage_mode == "use_existing" and len(eligible) > 1:
        raise BookingPackagePolicyError(
            "Multiple usable packages match this booking; an exact package selection is required."
        )

    selected = min(eligible, key=_selection_key)
    return BookingPackageResolution(
        package_id=selected.id,
        package_name=selected.name,
        package_used=True,
        usage_mode=usage_mode,
    )
