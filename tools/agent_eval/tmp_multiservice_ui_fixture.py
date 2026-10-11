from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.workspace import Workspace

CAIRO = ZoneInfo("Africa/Cairo")
OUT = Path("audit-output")
AGENT_EVIDENCE = OUT / "agent-db-evidence.json"
UI_EVIDENCE = OUT / "ui-probe-evidence.json"

SCENARIOS = {
    "A": {"slugs": ["laser-hair-removal-underarm", "laser-hair-removal-bikini", "laser-hair-removal-full-arms"], "times": [(16, 0), (16, 30), (17, 0)]},
    "B": {"slugs": ["laser-hair-removal-underarm", "laser-hair-removal-bikini"], "times": [(18, 0), (18, 30)]},
    "C": {"slugs": ["laser-hair-removal-underarm", "laser-hair-removal-bikini"], "times": [(19, 0), (19, 30)]},
}


def next_tuesday(hour: int, minute: int) -> datetime:
    now = datetime.now(CAIRO)
    days = (1 - now.weekday()) % 7
    if days == 0:
        days = 7
    target = (now + timedelta(days=days)).date()
    return datetime(target.year, target.month, target.day, hour, minute, tzinfo=CAIRO)


def row_payload(row: Appointment, service: Service, branch: Branch, doctor_name: str) -> dict:
    return {
        "appointment_id": str(row.id),
        "patient_id": str(row.patient_id),
        "service_id": str(row.service_id),
        "service_name": service.name,
        "service_slug": service.slug,
        "branch_id": str(row.branch_id),
        "branch_name": branch.name,
        "doctor_id": str(row.doctor_id),
        "doctor_name": doctor_name,
        "start_at": row.start_at.isoformat(),
        "end_at": row.end_at.isoformat(),
        "start_local": row.start_at.astimezone(CAIRO).isoformat(),
        "end_local": row.end_at.astimezone(CAIRO).isoformat(),
        "status": row.status,
        "visit_group_id": str(row.visit_group_id) if row.visit_group_id else None,
        "source": row.source,
    }


def main() -> None:
    agent = json.loads(AGENT_EVIDENCE.read_text(encoding="utf-8")) if AGENT_EVIDENCE.exists() else {"scenarios": {}}
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    result = {"purpose": "supplemental-ui-only-probe", "not_agent_created": True, "scenarios": {}}

    with Session(engine) as db:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if workspace is None:
            raise RuntimeError("Synthetic workspace tia not found")

        branch = db.scalar(select(Branch).where(Branch.workspace_id == workspace.id, Branch.code == "new-cairo"))
        if branch is None:
            raise RuntimeError("No active synthetic branch")

        doctor_pair = db.execute(
            select(Doctor, Staff)
            .join(Staff, Staff.id == Doctor.staff_id)
            .where(
                Doctor.workspace_id == workspace.id,
                Doctor.is_active.is_(True),
                Staff.first_name == "يوسف",
                Staff.last_name == "سمير",
            )
        ).first()
        if doctor_pair is None:
            raise RuntimeError("Synthetic doctor يوسف سمير not found")
        doctor, staff = doctor_pair
        doctor_name = f"{staff.first_name} {staff.last_name}".strip()

        for key, spec in SCENARIOS.items():
            actual = ((agent.get("scenarios") or {}).get(key) or {}).get("appointments") or []
            if actual:
                result["scenarios"][key] = {"evidence_source": "agent", "appointments": actual}
                continue

            patient = db.scalar(
                select(Patient).where(
                    Patient.workspace_id == workspace.id,
                    Patient.source_detail == f"multiservice-ui-audit-{key}",
                )
            )
            if patient is None:
                patient = Patient(
                    id=uuid4(),
                    workspace_id=workspace.id,
                    first_name="E2E",
                    last_name=f"Multi {key}",
                    phone=f"+20000024000{ord(key) - 64}",
                    phone_normalized=f"+20000024000{ord(key) - 64}",
                    preferred_language="ar",
                    preferred_branch_id=branch.id,
                    source="other",
                    source_detail=f"multiservice-ui-audit-{key}",
                    status="active",
                )
                db.add(patient)
                db.flush()

            group_id = uuid4()
            rows = []
            for index, (slug, clock) in enumerate(zip(spec["slugs"], spec["times"], strict=True)):
                service = db.scalar(select(Service).where(Service.workspace_id == workspace.id, Service.slug == slug))
                if service is None:
                    raise RuntimeError(f"Service {slug} missing")
                start_local = next_tuesday(*clock)
                start_at = start_local.astimezone(ZoneInfo("UTC"))
                end_at = start_at + timedelta(minutes=service.duration_minutes)
                row = Appointment(
                    id=uuid4(),
                    workspace_id=workspace.id,
                    patient_id=patient.id,
                    branch_id=branch.id,
                    doctor_id=doctor.id,
                    doctor_assignment_known=True,
                    service_id=service.id,
                    visit_group_id=group_id,
                    status="confirmed",
                    source="ai",
                    start_at=start_at,
                    end_at=end_at,
                    busy_start_at=start_at - timedelta(minutes=service.buffer_before_minutes),
                    busy_end_at=end_at + timedelta(minutes=service.buffer_after_minutes),
                    duration_minutes=service.duration_minutes,
                    price_minor=service.price_minor,
                    discount_minor=0,
                    currency=service.currency,
                    payment_status="unknown",
                    payment_method="unknown",
                    billing_context="standard",
                    customer_note=f"multiservice-ui-supplemental-fixture:{key}:{index}",
                    idempotency_key=f"multiservice-ui-supplemental:{key}:{group_id}:{index}",
                    confirmed_at=datetime.now(ZoneInfo("UTC")),
                )
                db.add(row)
                db.flush()
                rows.append(row_payload(row, service, branch, doctor_name))

            result["scenarios"][key] = {
                "evidence_source": "supplemental-ui-fixture",
                "patient": {"id": str(patient.id), "name": f"{patient.first_name} {patient.last_name or ''}".strip()},
                "visit_group_id": str(group_id),
                "appointments": rows,
            }

        db.commit()

    UI_EVIDENCE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("UI_PROBE_EVIDENCE=" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
