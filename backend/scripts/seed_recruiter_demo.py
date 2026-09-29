from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings
from app.models.appointment import Appointment
from app.models.patient import Patient
from app.models.workspace import Workspace
from scripts import seed_realistic_aesthetic_clinic as realistic

DEMO_WORKSPACE_SLUG = "tia-demo"
DEMO_WORKSPACE_NAME = "Linka Demo Aesthetic Clinic"
DEMO_PATIENTS = (
    {
        "key": "recruiter-new-booking",
        "first_name": "مريم",
        "last_name": "تجربة الحجز",
        "phone": "+200000240001",
        "source": "website",
    },
    {
        "key": "recruiter-upcoming",
        "first_name": "نور",
        "last_name": "تجربة التعديل",
        "phone": "+200000240002",
        "source": "whatsapp",
    },
    {
        "key": "recruiter-history",
        "first_name": "سارة",
        "last_name": "تجربة السجل",
        "phone": "+200000240003",
        "source": "instagram",
    },
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the isolated Linka recruiter/admin demo workspace."
    )
    parser.add_argument("--workspace-slug", default=DEMO_WORKSPACE_SLUG)
    parser.add_argument("--workspace-name", default=DEMO_WORKSPACE_NAME)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def require_demo_environment() -> None:
    if settings.is_production:
        raise RuntimeError("Refusing to seed recruiter data into ENVIRONMENT=production.")
    if not settings.demo_mode:
        raise RuntimeError(
            "DEMO_MODE must be true before seeding the public recruiter demo."
        )


def ensure_workspace(db: Session, *, slug: str, name: str) -> Workspace:
    workspace = db.scalar(select(Workspace).where(Workspace.slug == slug))
    if workspace is None:
        workspace = Workspace(name=name, slug=slug, timezone="Africa/Cairo", is_active=True)
        db.add(workspace)
        db.flush()
    else:
        workspace.name = name
        workspace.timezone = "Africa/Cairo"
        workspace.is_active = True
        db.flush()
    return workspace


def upsert_demo_patient(db: Session, workspace: Workspace, spec: dict, preferred_branch_id: UUID) -> Patient:
    row_id = realistic.fixture_id(workspace.id, f"patient:{spec['key']}")
    patient = db.get(Patient, row_id)
    values = dict(
        workspace_id=workspace.id,
        first_name=spec["first_name"],
        last_name=spec["last_name"],
        phone=spec["phone"],
        phone_normalized=spec["phone"],
        gender=None,
        birth_date=None,
        preferred_language="ar",
        preferred_branch_id=preferred_branch_id,
        source=spec["source"],
        source_detail="recruiter-demo-v1",
        status="active",
        marketing_consent=False,
        marketing_consent_at=None,
        last_contact_at=datetime.now(timezone.utc) - timedelta(minutes=15),
    )
    if patient is None:
        patient = Patient(id=row_id, **values)
        db.add(patient)
    else:
        for key, value in values.items():
            setattr(patient, key, value)
    db.flush()
    return patient


def seed_recruiter_personas(
    db: Session,
    workspace: Workspace,
    *,
    branches: dict,
    doctors: dict,
    services: dict,
) -> dict[str, Patient]:
    ids = [realistic.fixture_id(workspace.id, f"patient:{spec['key']}") for spec in DEMO_PATIENTS]
    db.execute(
        delete(Appointment).where(
            Appointment.workspace_id == workspace.id,
            Appointment.patient_id.in_(ids),
        )
    )
    db.flush()

    result = {
        spec["key"]: upsert_demo_patient(
            db,
            workspace,
            spec,
            branches["new-cairo"].id,
        )
        for spec in DEMO_PATIENTS
    }

    realistic.make_appointment(
        db=db,
        workspace=workspace,
        patient=result["recruiter-upcoming"],
        branch=branches["new-cairo"],
        doctor=doctors["mariam-hassan"],
        service=services["hydrafacial"],
        key="recruiter-upcoming-hydrafacial",
        start_local=realistic.next_local_weekday(2, 18, 0),
        status="confirmed",
        source="whatsapp",
    )
    realistic.make_appointment(
        db=db,
        workspace=workspace,
        patient=result["recruiter-history"],
        branch=branches["nasr-city"],
        doctor=doctors["ahmed-mahmoud"],
        service=services["laser-hair-underarm"],
        key="recruiter-history-completed",
        start_local=realistic.previous_local_weekday(1, 17, 0),
        status="completed",
        source="instagram",
    )
    return result


def main() -> int:
    args = parse_args()
    require_demo_environment()

    if args.dry_run:
        print(json.dumps({
            "workspace_slug": args.workspace_slug,
            "workspace_name": args.workspace_name,
            "personas": [spec["key"] for spec in DEMO_PATIENTS],
            "external_dispatch": False,
        }, ensure_ascii=False, indent=2))
        return 0

    from app.database.session import SessionLocal

    with SessionLocal() as db:
        workspace = ensure_workspace(db, slug=args.workspace_slug.strip().lower(), name=args.workspace_name.strip())
        try:
            branches = {spec["key"]: realistic.upsert_branch(db, workspace, spec) for spec in realistic.BRANCHES}
            services = {spec["key"]: realistic.upsert_service(db, workspace, spec) for spec in realistic.SERVICES}
            doctors = {}
            for spec in realistic.DOCTORS:
                _, doctor = realistic.upsert_doctor(db, workspace, spec)
                doctors[spec["key"]] = doctor

            realistic.assert_unique_active_doctor_names(db, workspace)
            realistic.replace_branch_hours(db, workspace, branches)
            realistic.replace_doctor_assignments(db, workspace, doctors, branches, services)
            realistic.upsert_booking_settings(db, workspace)
            personas = seed_recruiter_personas(
                db,
                workspace,
                branches=branches,
                doctors=doctors,
                services=services,
            )
            if workspace.primary_branch_id is None:
                workspace.primary_branch_id = branches["new-cairo"].id
            db.commit()
        except Exception:
            db.rollback()
            raise

        print("Linka recruiter demo workspace is ready")
        print(json.dumps({
            "workspace_id": str(workspace.id),
            "workspace_slug": workspace.slug,
            "workspace_name": workspace.name,
            "demo_patient_ids": {key: str(value.id) for key, value in personas.items()},
            "recommended_tests": [
                "Book a new appointment as مريم تجربة الحجز",
                "Reschedule/cancel the upcoming appointment as نور تجربة التعديل",
                "Ask about appointment history as سارة تجربة السجل",
            ],
            "safety": {
                "demo_mode_required": True,
                "external_provider_dispatch_allowed": settings.demo_allow_external_dispatch,
            },
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
