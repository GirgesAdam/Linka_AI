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
OUT = Path("audit-output")
OUT.mkdir(exist_ok=True)
engine = create_engine(settings.database_url, pool_pre_ping=True)
AUDIT_AUTH_USER_ID = UUID("d6a87151-d0c5-4be0-93d8-8a3f0194b239")
AUDIT_EMAIL = "audit.reception@linka.local"

SCENARIOS = {
    "A": {
        "name": "E2E Multi A",
        "phone": "+200000240001",
        "messages": [
            "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0632\u0627\u0644\u0629 \u0627\u0644\u0634\u0639\u0631 \u0644\u0644\u0625\u0628\u0637 \u0648\u0627\u0644\u0628\u0643\u064a\u0646\u064a \u0648\u0627\u0644\u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646",
            "\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 \u0627\u0644\u062c\u0627\u064a",
        ],
        "expected_slugs": [
            "laser-hair-removal-underarm",
            "laser-hair-removal-bikini",
            "laser-hair-removal-full-arms",
        ],
    },
    "B": {
        "name": "E2E Multi B",
        "phone": "+200000240002",
        "messages": [
            "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0625\u0628\u0637 \u0648\u0628\u0643\u064a\u0646\u064a",
            "\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 \u0627\u0644\u062c\u0627\u064a",
        ],
        "expected_slugs": [
            "laser-hair-removal-underarm",
            "laser-hair-removal-bikini",
        ],
    },
    "C": {
        "name": "E2E Multi C",
        "phone": "+200000240003",
        "messages": [
            "\u0639\u0627\u064a\u0632\u0629 \u0625\u0628\u0637 \u0648\u0628\u0643\u064a\u0646\u064a \u0648\u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646",
            "\u0644\u0627 \u062e\u0644\u064a\u0647\u0645 \u0625\u0628\u0637 \u0648\u0628\u0643\u064a\u0646\u064a \u0628\u0633",
            "\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 \u0627\u0644\u062c\u0627\u064a",
        ],
        "expected_slugs": [
            "laser-hair-removal-underarm",
            "laser-hair-removal-bikini",
        ],
    },
}

ARABIC_DIGIT_MAP = str.maketrans(
    {
        "\u0660": "0", "\u0661": "1", "\u0662": "2", "\u0663": "3", "\u0664": "4",
        "\u0665": "5", "\u0666": "6", "\u0667": "7", "\u0668": "8", "\u0669": "9",
        "\u06f0": "0", "\u06f1": "1", "\u06f2": "2", "\u06f3": "3", "\u06f4": "4",
        "\u06f5": "5", "\u06f6": "6", "\u06f7": "7", "\u06f8": "8", "\u06f9": "9",
    }
)


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
        "verified_reads": jsonable(capture.verified_reads),
        "write_attempted": capture.write_attempted,
        "write_result": jsonable(capture.write_result),
        "actions": jsonable(capture.actions),
        "errors": jsonable(capture.errors),
        "structured_trace": jsonable(capture.structured_trace),
    }


def time_options_from_reply(reply: str) -> list[str]:
    text = reply.translate(ARABIC_DIGIT_MAP)
    am = "\u0635"
    pm = "\u0645"
    morning = "\u0635\u0628\u0627\u062d"
    evening = "\u0645\u0633\u0627\u0621"
    pattern = rf"(?<!\d)(\d{{1,2}})(?::(\d{{2}}))?\s*({morning}[^\s,]*|{evening}[^\s,]*|{am}|{pm})"
    options: list[str] = []
    for match in re.finditer(pattern, text):
        hour = int(match.group(1))
        minute_raw = match.group(2)
        marker = match.group(3) or ""
        if hour > 23 or (minute_raw is not None and int(minute_raw) > 59):
            continue
        clock = str(hour) if minute_raw is None else f"{hour}:{minute_raw}"
        option = f"\u0627\u0644\u0633\u0627\u0639\u0629 {clock} {marker}".strip()
        if option not in options:
            options.append(option)
    return options


def selection_message_for_reply(scenario: str, reply: str) -> str | None:
    options = time_options_from_reply(reply)
    if not options:
        return None
    if scenario == "A" and len(options) >= 2:
        return (
            f"{options[0]} \u0644\u0644\u0625\u0628\u0637 \u0648\u0627\u0644\u0628\u0643\u064a\u0646\u064a"
            f"\u060c \u0648{options[1]} \u0644\u0644\u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646"
        )
    return options[0]


def appointment_rows(db: Session, patient_id) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Appointment, Service, Branch, Doctor, Staff)
        .join(
            Service,
            (Service.workspace_id == Appointment.workspace_id)
            & (Service.id == Appointment.service_id),
        )
        .join(
            Branch,
            (Branch.workspace_id == Appointment.workspace_id)
            & (Branch.id == Appointment.branch_id),
        )
        .join(
            Doctor,
            (Doctor.workspace_id == Appointment.workspace_id)
            & (Doctor.id == Appointment.doctor_id),
        )
        .join(
            Staff,
            (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
        )
        .where(Appointment.patient_id == patient_id)
        .order_by(Appointment.start_at, Appointment.id)
    ).all()
    result = []
    for appt, service, branch, doctor, staff in rows:
        result.append(
            {
                "appointment_id": str(appt.id),
                "patient_id": str(appt.patient_id),
                "service_id": str(appt.service_id),
                "service_slug": service.slug,
                "service_name": service.name,
                "branch_id": str(appt.branch_id),
                "branch_name": branch.name,
                "doctor_id": str(appt.doctor_id),
                "doctor_name": " ".join(
                    part for part in [staff.first_name, staff.last_name] if part
                ),
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
            }
        )
    return result


def create_patient(
    db: Session, workspace: Workspace, scenario: str, spec: dict[str, Any]
) -> Patient:
    existing = db.scalar(
        select(Patient).where(
            Patient.workspace_id == workspace.id, Patient.phone == spec["phone"]
        )
    )
    if existing:
        db.delete(existing)
        db.flush()
    first, *rest = spec["name"].split(" ")
    patient = Patient(
        id=uuid4(),
        workspace_id=workspace.id,
        first_name=first,
        last_name=" ".join(rest) or None,
        phone=spec["phone"],
        phone_normalized=spec["phone"],
        preferred_language="ar",
        source="website",
        status="active",
        source_detail=f"multiservice-ui-audit-{scenario}",
    )
    db.add(patient)
    db.flush()
    return patient


def overlap(rows: list[dict[str, Any]]) -> bool:
    spans = sorted(
        (datetime.fromisoformat(row["start_at"]), datetime.fromisoformat(row["end_at"]))
        for row in rows
    )
    return any(spans[index][0] < spans[index - 1][1] for index in range(1, len(spans)))


def save_evidence(evidence: dict[str, Any]) -> None:
    (OUT / "agent-db-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_turn(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    scenario: str,
    turn_no: int,
    message: str,
    conversation_id,
    transcript: list[dict[str, Any]],
):
    response, capture = send_turn(
        db,
        workspace,
        patient,
        f"UI-{scenario}",
        turn_no,
        message,
        conversation_id,
    )
    transcript.append(summarize_capture(capture))
    db.commit()
    return response.conversation_id


def run_scenario(
    db: Session, workspace: Workspace, key: str, spec: dict[str, Any]
) -> dict[str, Any]:
    patient = create_patient(db, workspace, key, spec)
    db.commit()
    conversation_id = None
    transcript: list[dict[str, Any]] = []
    turn_no = 0

    for message in spec["messages"]:
        turn_no += 1
        conversation_id = run_turn(
            db,
            workspace,
            patient,
            key,
            turn_no,
            message,
            conversation_id,
            transcript,
        )

    rows = appointment_rows(db, patient.id)
    chosen_time_messages: list[str] = []
    confirmation_sent = False
    for _ in range(6):
        if rows:
            break
        reply = transcript[-1]["agent"] or ""
        selection = selection_message_for_reply(key, reply)
        if selection is not None and selection not in chosen_time_messages:
            turn_no += 1
            conversation_id = run_turn(
                db,
                workspace,
                patient,
                key,
                turn_no,
                selection,
                conversation_id,
                transcript,
            )
            chosen_time_messages.append(selection)
            rows = appointment_rows(db, patient.id)
            continue
        if not confirmation_sent:
            turn_no += 1
            conversation_id = run_turn(
                db,
                workspace,
                patient,
                key,
                turn_no,
                "\u0623\u064a\u0648\u0647 \u0623\u0643\u062f\u064a \u0627\u0644\u062d\u062c\u0632",
                conversation_id,
                transcript,
            )
            confirmation_sent = True
            rows = appointment_rows(db, patient.id)
            continue
        break

    actual_slugs = [row["service_slug"] for row in rows]
    expected = spec["expected_slugs"]
    visit_ids = sorted(
        {row["visit_group_id"] for row in rows if row["visit_group_id"]}
    )
    return {
        "scenario": key,
        "patient": {
            "id": str(patient.id),
            "name": spec["name"],
            "phone": spec["phone"],
        },
        "conversation_id": str(conversation_id) if conversation_id else None,
        "chosen_time_messages": chosen_time_messages,
        "transcript": transcript,
        "appointments": rows,
        "appointment_count": len(rows),
        "actual_service_slugs": actual_slugs,
        "expected_service_slugs": expected,
        "missing": sorted(set(expected) - set(actual_slugs)),
        "unexpected": sorted(set(actual_slugs) - set(expected)),
        "duplicates": sorted(
            {slug for slug in actual_slugs if actual_slugs.count(slug) > 1}
        ),
        "visit_group_ids": visit_ids,
        "all_rows_share_one_visit_group": (
            len(rows) > 1
            and len(visit_ids) == 1
            and all(row["visit_group_id"] == visit_ids[0] for row in rows)
        ),
        "internal_time_overlap": overlap(rows),
        "booking_created": bool(rows),
        "expected_service_match": (
            bool(rows)
            and not (set(expected) - set(actual_slugs))
            and not (set(actual_slugs) - set(expected))
            and len(actual_slugs) == len(set(actual_slugs))
        ),
    }


def main() -> int:
    evidence: dict[str, Any] = {
        "main_sha": "66f4190c3450e3adbf581fdbe3b40b784a7e2718",
        "environment": settings.environment,
        "agent_runtime": "Agent V2 live",
        "run_started_at": datetime.now(UTC).isoformat(),
        "scenarios": {},
    }
    try:
        with Session(engine, expire_on_commit=False) as db:
            workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
            if workspace is None:
                raise RuntimeError("Synthetic workspace tia not found")
            assert_demo_only(workspace)

            user = db.scalar(
                select(User).where(User.auth_user_id == AUDIT_AUTH_USER_ID)
            )
            if user is None:
                user = User(
                    auth_user_id=AUDIT_AUTH_USER_ID,
                    email=AUDIT_EMAIL,
                    full_name="Audit Reception",
                    is_active=True,
                )
                db.add(user)
                db.flush()
            membership = db.scalar(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspace.id,
                    WorkspaceMember.user_id == user.id,
                )
            )
            if membership is None:
                membership = WorkspaceMember(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    role="admin",
                    is_active=True,
                )
                db.add(membership)
            else:
                membership.role = "admin"
                membership.is_active = True
            db.flush()

            branches = list(
                db.scalars(select(Branch).where(Branch.workspace_id == workspace.id))
            )
            keep = next(row for row in branches if row.code == "new-cairo")
            for row in branches:
                row.is_active = row.id == keep.id
            workspace.primary_branch_id = keep.id
            db.commit()
            clear_catalog()

            evidence["workspace"] = {
                "id": str(workspace.id),
                "slug": workspace.slug,
                "single_active_branch_id": str(keep.id),
                "single_active_branch_name": keep.name,
            }
            for key, spec in SCENARIOS.items():
                evidence["scenarios"][key] = run_scenario(
                    db, workspace, key, spec
                )
                save_evidence(evidence)
                clear_catalog()
    except Exception as exc:
        evidence["runner_error"] = f"{type(exc).__name__}: {exc}"
        evidence["run_finished_at"] = datetime.now(UTC).isoformat()
        save_evidence(evidence)
        raise

    evidence["run_finished_at"] = datetime.now(UTC).isoformat()
    save_evidence(evidence)
    missing_booking = [
        key
        for key, result in evidence["scenarios"].items()
        if not result["booking_created"]
    ]
    print(
        "AUDIT_SUMMARY="
        + json.dumps(
            {
                key: {
                    "booking_created": value["booking_created"],
                    "expected_service_match": value["expected_service_match"],
                    "appointment_count": value["appointment_count"],
                    "services": value["actual_service_slugs"],
                    "visit_group_ids": value["visit_group_ids"],
                    "internal_time_overlap": value["internal_time_overlap"],
                }
                for key, value in evidence["scenarios"].items()
            },
            ensure_ascii=True,
        )
    )
    return 1 if missing_booking else 0


if __name__ == "__main__":
    raise SystemExit(main())
