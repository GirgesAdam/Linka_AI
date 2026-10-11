from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents import clinic_grounding
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from tools.agent_eval.harness import assert_demo_only, send_turn

CAIRO = ZoneInfo("Africa/Cairo")
AUDIT_AUTH_USER_ID = UUID("d6a87151-d0c5-4be0-93d8-8a3f0194b239")
AUDIT_EMAIL = "audit.reception@linka.local"
OUT = Path("audit-output")
OUT.mkdir(exist_ok=True)
engine = create_engine(settings.database_url, pool_pre_ping=True)

SCENARIOS = {
    "A": {
        "name": "E2E Multi A",
        "phone": "+200000240001",
        "messages": [
            "Ø¹Ø§ÙŠØ²Ø© Ø£Ø­Ø¬Ø² Ù„ÙŠØ²Ø± Ø¥Ø²Ø§Ù„Ø© Ø§Ù„Ø´Ø¹Ø± Ù„Ù„Ø¥Ø¨Ø· ÙˆØ§Ù„Ø¨ÙƒÙŠÙ†ÙŠ ÙˆØ§Ù„Ø°Ø±Ø§Ø¹ÙŠÙ† ÙƒØ§Ù…Ù„ÙŠÙ†",
            "Ø§Ù„Ø«Ù„Ø§Ø«Ø§Ø¡ Ø§Ù„Ø¬Ø§ÙŠ",
        ],
        "expected_slugs": ["laser-hair-removal-underarm", "laser-hair-removal-bikini", "laser-hair-removal-full-arms"],
    },
    "B": {
        "name": "E2E Multi B",
        "phone": "+200000240002",
        "messages": ["Ø¹Ø§ÙŠØ²Ø© Ø£Ø­Ø¬Ø² Ø¥Ø¨Ø· ÙˆØ¨ÙƒÙŠÙ†ÙŠ", "Ø§Ù„Ø«Ù„Ø§Ø«Ø§Ø¡ Ø§Ù„Ø¬Ø§ÙŠ"],
        "expected_slugs": ["laser-hair-removal-underarm", "laser-hair-removal-bikini"],
    },
    "C": {
        "name": "E2E Multi C",
        "phone": "+200000240003",
        "messages": [
            "Ø¹Ø§ÙŠØ²Ø© Ø¥Ø¨Ø· ÙˆØ¨ÙƒÙŠÙ†ÙŠ ÙˆØ°Ø±Ø§Ø¹ÙŠÙ† ÙƒØ§Ù…Ù„ÙŠÙ†",
            "Ù„Ø§ Ø®Ù„ÙŠÙ‡Ù… Ø¥Ø¨Ø· ÙˆØ¨ÙƒÙŠÙ†ÙŠ Ø¨Ø³",
            "Ø§Ù„Ø«Ù„Ø§Ø«Ø§Ø¡ Ø§Ù„Ø¬Ø§ÙŠ",
        ],
        "expected_slugs": ["laser-hair-removal-underarm", "laser-hair-removal-bikini"],
    },
}


def clear_catalog() -> None:
    with clinic_grounding._catalog_cache_lock:
        clinic_grounding._catalog_cache.clear()


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump(mode="json"))
    return str(value)


def summarize_capture(capture) -> dict[str, Any]:
    return {
        "customer": capture.user_message,
        "agent": capture.agent_response,
        "model": capture.model,
        "verified_reads": capture.verified_reads,
        "write_attempted": capture.write_attempted,
        "write_result": jsonable(capture.write_result),
        "actions": jsonable(capture.actions),
        "errors": jsonable(capture.errors),
        "structured_trace": jsonable(capture.structured_trace),
    }


def arabic_digits_to_ascii(text: str) -> str:
    return text.translate(str.maketrans("Ù Ù¡Ù¢Ù£Ù¤Ù¥Ù¦Ù§Ù¨Ù©Û°Û±Û²Û³Û´ÛµÛ¶Û·Û¸Û¹", "01234567890123456789"))


def time_from_reply(reply: str) -> str | None:
    text = arabic_digits_to_ascii(reply)
    # Prefer explicit clock tokens with Arabic day-part markers.
    candidates: list[tuple[int, int, str | None]] = []
    for m in re.finditer(r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(ØµØ¨Ø§Ø­(?:Ù‹Ø§)?|Ù…Ø³Ø§Ø¡(?:Ù‹)?|Øµ|Ù…)?", text):
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        marker = m.group(3)
        if hour > 23 or minute > 59:
            continue
        # Ignore obvious date numbers unless a day-part exists or a minute is present.
        if marker is None and m.group(2) is None:
            continue
        candidates.append((hour, minute, marker))
    if not candidates:
        return None
    hour, minute, marker = candidates[0]
    if marker and (marker.startswith("Ù…") or marker.startswith("Ù…Ø³Ø§Ø¡")) and hour < 12:
        hour += 12
    if marker and (marker.startswith("Øµ") or marker.startswith("ØµØ¨Ø§Ø­")) and hour == 12:
        hour = 0
    suffix = "Ù…Ø³Ø§Ø¡Ù‹" if hour >= 12 else "ØµØ¨Ø§Ø­Ù‹Ø§"
    display_hour = hour - 12 if hour > 12 else (12 if hour == 0 else hour)
    return f"Ø§Ù„Ø³Ø§Ø¹Ø© {display_hour}:{minute:02d} {suffix}"


def appointment_rows(db: Session, patient_id) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Appointment, Service, Branch, Doctor, Staff)
        .join(Service, (Service.workspace_id == Appointment.workspace_id) & (Service.id == Appointment.service_id))
        .join(Branch, (Branch.workspace_id == Appointment.workspace_id) & (Branch.id == Appointment.branch_id))
        .join(Doctor, (Doctor.workspace_id == Appointment.workspace_id) & (Doctor.id == Appointment.doctor_id))
        .join(Staff, (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id))
        .where(Appointment.patient_id == patient_id)
        .order_by(Appointment.start_at, Appointment.id)
    ).all()
    result = []
    for appt, service, branch, doctor, staff in rows:
        result.append({
            "appointment_id": str(appt.id),
            "patient_id": str(appt.patient_id),
            "service_id": str(appt.service_id),
            "service_slug": service.slug,
            "service_name": service.name,
            "branch_id": str(appt.branch_id),
            "branch_name": branch.name,
            "doctor_id": str(appt.doctor_id),
            "doctor_name": " ".join(part for part in [staff.first_name, staff.last_name] if part),
            "laser_device_key": appt.laser_device_key,
            "laser_device_name": appt.laser_device_name,
            "start_at": appt.start_at.isoformat(),
            "end_at": appt.end_at.isoformat(),
            "start_local": appt.start_at.astimezone(CAIRO).isoformat(),
            "end_local": appt.end_at.astimezone(CAIRO).isoformat(),
            "duration_minutes": appt.duration_minutes,
            "status": appt.status,
            "source": appt.source,
            "visit_group_id": str(appt.visit_group_id) if appt.visit_group_id else None,
        })
    return result


def create_patient(db: Session, workspace: Workspace, scenario: str, spec: dict[str, Any]) -> Patient:
    existing = db.scalar(select(Patient).where(Patient.workspace_id == workspace.id, Patient.phone == spec["phone"]))
    if existing:
        db.delete(existing)
        db.flush()
    first, *rest = spec["name"].split(" ")
    patient = Patient(
        id=uuid4(), workspace_id=workspace.id, first_name=first,
        last_name=" ".join(rest) or None, phone=spec["phone"], phone_normalized=spec["phone"],
        preferred_language="ar", source="website", status="active",
        source_detail=f"multiservice-ui-audit-{scenario}",
    )
    db.add(patient)
    db.flush()
    return patient


def overlap(rows: list[dict[str, Any]]) -> bool:
    spans = sorted((datetime.fromisoformat(r["start_at"]), datetime.fromisoformat(r["end_at"])) for r in rows)
    return any(spans[i][0] < spans[i - 1][1] for i in range(1, len(spans)))


def run_scenario(db: Session, workspace: Workspace, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    patient = create_patient(db, workspace, key, spec)
    db.commit()
    conversation_id = None
    transcript: list[dict[str, Any]] = []
    turn_no = 0

    for message in spec["messages"]:
        turn_no += 1
        response, capture = send_turn(db, workspace, patient, f"UI-{key}", turn_no, message, conversation_id)
        conversation_id = response.conversation_id
        transcript.append(summarize_capture(capture))
        db.commit()

    rows = appointment_rows(db, patient.id)
    if not rows:
        offered = time_from_reply(transcript[-1]["agent"] or "")
        if offered is None:
            turn_no += 1
            response, capture = send_turn(db, workspace, patient, f"UI-{key}", turn_no, "أيوه، كلهم في نفس الزيارة", conversation_id)
            conversation_id = response.conversation_id
            transcript.append(summarize_capture(capture))
            db.commit()
            offered = time_from_reply(transcript[-1]["agent"] or "")
        if offered is None:
            raise RuntimeError(f"Scenario {key}: could not derive an offered time from reply: {transcript[-1]['agent']!r}")
        turn_no += 1
        response, capture = send_turn(db, workspace, patient, f"UI-{key}", turn_no, offered, conversation_id)
        conversation_id = response.conversation_id
        transcript.append(summarize_capture(capture))
        db.commit()
        rows = appointment_rows(db, patient.id)

    if not rows:
        turn_no += 1
        response, capture = send_turn(db, workspace, patient, f"UI-{key}", turn_no, "Ø£ÙŠÙˆÙ‡ Ø£ÙƒØ¯ÙŠ Ø§Ù„Ø­Ø¬Ø²", conversation_id)
        conversation_id = response.conversation_id
        transcript.append(summarize_capture(capture))
        db.commit()
        rows = appointment_rows(db, patient.id)

    actual_slugs = [r["service_slug"] for r in rows]
    expected = spec["expected_slugs"]
    visit_ids = sorted({r["visit_group_id"] for r in rows if r["visit_group_id"]})
    return {
        "scenario": key,
        "patient": {"id": str(patient.id), "name": spec["name"], "phone": spec["phone"]},
        "conversation_id": str(conversation_id) if conversation_id else None,
        "transcript": transcript,
        "appointments": rows,
        "appointment_count": len(rows),
        "actual_service_slugs": actual_slugs,
        "expected_service_slugs": expected,
        "missing": sorted(set(expected) - set(actual_slugs)),
        "unexpected": sorted(set(actual_slugs) - set(expected)),
        "duplicates": sorted({slug for slug in actual_slugs if actual_slugs.count(slug) > 1}),
        "visit_group_ids": visit_ids,
        "all_rows_share_one_visit_group": len(rows) > 1 and len(visit_ids) == 1 and all(r["visit_group_id"] == visit_ids[0] for r in rows),
        "internal_time_overlap": overlap(rows),
        "booking_success": bool(rows) and not (set(expected) - set(actual_slugs)) and not (set(actual_slugs) - set(expected)),
    }


def main() -> int:
    with Session(engine, expire_on_commit=False) as db:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if workspace is None:
            raise RuntimeError("Synthetic workspace tia not found")
        assert_demo_only(workspace)
        user = db.scalar(select(User).where(User.auth_user_id == AUDIT_AUTH_USER_ID))
        if user is None:
            user = User(auth_user_id=AUDIT_AUTH_USER_ID, email=AUDIT_EMAIL, full_name="Audit Reception", is_active=True)
            db.add(user)
            db.flush()
        membership = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace.id, WorkspaceMember.user_id == user.id))
        if membership is None:
            membership = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="admin", is_active=True)
            db.add(membership)
        else:
            membership.role = "admin"
            membership.is_active = True
        db.flush()
        branches = list(db.scalars(select(Branch).where(Branch.workspace_id == workspace.id)))
        keep = next(row for row in branches if row.code == "new-cairo")
        for row in branches:
            row.is_active = row.id == keep.id
        workspace.primary_branch_id = keep.id
        db.commit()
        clear_catalog()

        evidence = {
            "main_sha": "66f4190c3450e3adbf581fdbe3b40b784a7e2718",
            "environment": settings.environment,
            "agent_runtime": "Agent V2 live",
            "workspace": {"id": str(workspace.id), "slug": workspace.slug, "single_active_branch_id": str(keep.id), "single_active_branch_name": keep.name},
            "run_started_at": datetime.now(UTC).isoformat(),
            "scenarios": {},
        }
        for key, spec in SCENARIOS.items():
            evidence["scenarios"][key] = run_scenario(db, workspace, key, spec)
            clear_catalog()
        evidence["run_finished_at"] = datetime.now(UTC).isoformat()
        (OUT / "agent-db-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("AUDIT_EVIDENCE=" + json.dumps(evidence, ensure_ascii=False))
        failed = [key for key, result in evidence["scenarios"].items() if not result["booking_success"]]
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())




