from __future__ import annotations

import json
from datetime import UTC, datetime

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.pulse_billing import (
    PatientPulsePack,
    PulseBillingSettings,
    PulsePackOffer,
)
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.pulse_billing import list_patient_pulse_balances
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import assert_demo_only


def _patient_name(row: Patient) -> str:
    return " ".join(part for part in (row.first_name, row.last_name) if part).strip()
def main() -> int:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as db:
        ws = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if ws is None:
            raise RuntimeError("Demo workspace not found")
        assert_demo_only(ws)
        catalog = build_clinic_catalog(db, ws)
        services = [row for row in catalog.get("services", []) if isinstance(row, dict)]
        doctors = [row for row in catalog.get("doctors", []) if isinstance(row, dict)]
        branches = [row for row in catalog.get("branches", []) if isinstance(row, dict)]
        service_names = {str(row.get("id")): str(row.get("name")) for row in services}

        service_rows = [{
            "name": row.get("name"),
            "slug": row.get("slug"),
            "duration_minutes": row.get("duration_minutes"),
            "price_minor": row.get("price_minor"),
            "currency": row.get("currency"),
            "requires_laser_device": row.get("requires_laser_device"),
            "laser_devices": row.get("laser_devices") or row.get("devices") or [],
            "doctor_count": len(row.get("doctor_ids") or []),
        } for row in services]
        doctor_rows = [{
            "name": row.get("name") or row.get("full_name"),
            "specialty": row.get("specialty"),
            "services": [service_names.get(str(value), str(value)) for value in (row.get("service_ids") or [])],
            "scheduled_branch_count": len(row.get("scheduled_branch_ids") or row.get("branch_ids") or []),
        } for row in doctors]
        branch_rows = [{
            "name": row.get("name"),
            "phone": row.get("phone"),
            "address": row.get("address"),
            "working_hours": row.get("working_hours"),
        } for row in branches]

        status_counts = dict(db.execute(
            select(Appointment.status, func.count()).where(
                Appointment.workspace_id == ws.id
            ).group_by(Appointment.status)
        ).all())
        upcoming = list(db.execute(
            select(Patient, func.count(Appointment.id))
            .join(Appointment, Appointment.patient_id == Patient.id)
            .where(
                Patient.workspace_id == ws.id,
                Appointment.workspace_id == ws.id,
                Appointment.start_at >= datetime.now(UTC),
                Appointment.status.in_(("pending", "confirmed", "checked_in", "in_progress")),
            )
            .group_by(Patient.id)
            .order_by(func.count(Appointment.id).desc(), Patient.created_at)
            .limit(12)
        ).all())
        package_rows = []
        for pkg, patient, service in db.execute(
            select(PatientPackage, Patient, Service)
            .join(Patient, Patient.id == PatientPackage.patient_id)
            .join(Service, Service.id == PatientPackage.service_id)
            .where(PatientPackage.workspace_id == ws.id)
            .order_by(PatientPackage.purchased_at.desc())
            .limit(30)
        ).all():
            used = int(db.scalar(select(func.coalesce(func.sum(PackageUsage.sessions_used), 0)).where(
                PackageUsage.workspace_id == ws.id,
                PackageUsage.patient_package_id == pkg.id,
                PackageUsage.status.in_(("reserved", "consumed")),
            )) or 0)
            remaining = None if pkg.opening_sessions_remaining is None else max(0, int(pkg.opening_sessions_remaining) - used)
            package_rows.append({
                "patient": _patient_name(patient), "service": service.name, "name": pkg.name,
                "status": pkg.status, "sessions_purchased": pkg.sessions_purchased,
                "remaining": remaining, "expires_at": pkg.expires_at.isoformat() if pkg.expires_at else None,
                "laser_device_name": pkg.laser_device_name,
            })

        pulse_offers = [{
            "device_key": row.device_key, "device_name": row.device_name,
            "pulses_count": row.pulses_count, "price_minor": row.price_minor,
            "currency": row.currency,
        } for row in db.scalars(select(PulsePackOffer).where(
            PulsePackOffer.workspace_id == ws.id, PulsePackOffer.is_active.is_(True)
        ).order_by(PulsePackOffer.device_key, PulsePackOffer.pulses_count))]
        overage = [{
            "device_key": row.device_key, "overage_price_minor": row.overage_price_minor,
            "currency": row.currency,
        } for row in db.scalars(select(PulseBillingSettings).where(
            PulseBillingSettings.workspace_id == ws.id
        ).order_by(PulseBillingSettings.device_key))]
        pulse_patients = []
        for patient_id in list(dict.fromkeys(db.scalars(select(PatientPulsePack.patient_id).where(
            PatientPulsePack.workspace_id == ws.id
        )).all()))[:12]:
            patient = db.get(Patient, patient_id)
            if patient is None:
                continue
            balances = [row.model_dump(mode="json") for row in list_patient_pulse_balances(
                db, workspace_id=ws.id, patient_id=patient.id
            )]
            pulse_patients.append({"patient": _patient_name(patient), "balances": balances})

        blocked = [_patient_name(row) for row in db.scalars(select(Patient).where(
            Patient.workspace_id == ws.id, Patient.status == "blocked"
        ).order_by(Patient.created_at).limit(10))]
        report = {
            "workspace": {"name": ws.name, "slug": ws.slug, "timezone": ws.timezone, "is_demo": ws.is_demo},
            "catalog_shapes": {
                "catalog_keys": sorted(catalog.keys()),
                "service_keys": sorted(services[0].keys()) if services else [],
                "doctor_keys": sorted(doctors[0].keys()) if doctors else [],
                "branch_keys": sorted(branches[0].keys()) if branches else [],
            },
            "branches": branch_rows, "services": service_rows, "doctors": doctor_rows,
            "appointment_status_counts": status_counts,
            "upcoming_patients": [{"patient": _patient_name(patient), "upcoming_count": int(count)} for patient, count in upcoming],
            "packages": package_rows,
            "pulse_offers": pulse_offers,
            "pulse_overage": overage,
            "pulse_patients": pulse_patients,
            "blocked_patients": blocked,
            "model": settings.openai_model,
            "reasoning_effort": settings.openai_reasoning_effort,
        }
        print("READINESS_DATASET=" + json.dumps(report, ensure_ascii=False, default=str), flush=True)
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
