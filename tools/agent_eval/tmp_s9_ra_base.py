from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.agents import clinic_grounding
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.branch import Branch
from app.models.conversation import Conversation
from app.models.doctor import Doctor
from app.models.message import Message
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.agent_v2.state_persistence import load_active_task
from app.services.whatsapp_interactions import process_whatsapp_booking_action
from tools.agent_eval.harness import assert_demo_only, send_turn

CAIRO = ZoneInfo("Africa/Cairo")
OUT = Path("audit-output")
OUT.mkdir(exist_ok=True)
engine = create_engine(settings.database_url, pool_pre_ping=True)
AUDIT_AUTH_USER_ID = UUID("d6a87151-d0c5-4be0-93d8-8a3f0194b239")
AUDIT_EMAIL = "audit.reception@linka.local"

SLUG = {
    "combo": "laser-hair-removal-underarm-bikini",
    "underarm": "laser-hair-removal-underarm",
    "bikini": "laser-hair-removal-bikini",
    "full_arms": "laser-hair-removal-full-arms",
    "full_legs": "laser-hair-removal-full-legs",
    "face": "laser-hair-removal-face",
    "deep": "deep-facial-cleansing",
    "hydrafacial": "hydrafacial",
}

SUCCESS_WORDS = (
    "╪¬┘à ╪¬╪ú┘â┘è╪» ╪º┘ä╪¡╪¼╪▓",
    "╪º┘ä╪¡╪¼╪▓ ╪º╪¬╪ú┘â╪»",
    "╪ú┘â╪»┘å╪º ╪º┘ä╪¡╪¼╪▓",
    "booking confirmed",
    "booking is confirmed",
)


def clear_catalog() -> None:
    with clinic_grounding._catalog_cache_lock:
        clinic_grounding._catalog_cache.clear()


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (UUID, datetime)):
        return str(value)
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return str(value)


def summarize_capture(capture) -> dict[str, Any]:
    return {
        "customer": capture.user_message,
        "agent": capture.agent_response,
        "model": capture.model,
        "verified_reads": jsonable(capture.verified_reads),
        "write_attempted": bool(capture.write_attempted),
        "write_result": jsonable(capture.write_result),
        "actions": jsonable(capture.actions),
        "errors": jsonable(capture.errors),
        "structured_trace": jsonable(capture.structured_trace),
    }


def service_rows(db: Session, workspace: Workspace) -> dict[str, Service]:
    return {
        row.slug: row
        for row in db.scalars(
            select(Service).where(Service.workspace_id == workspace.id, Service.is_active.is_(True))
        )
    }


def branches(db: Session, workspace: Workspace) -> dict[str, Branch]:
    return {
        row.code: row
        for row in db.scalars(select(Branch).where(Branch.workspace_id == workspace.id))
    }


def set_branch_mode(db: Session, workspace: Workspace, *, multi_location: bool) -> dict[str, Branch]:
    rows = branches(db, workspace)
    if multi_location:
        workspace.primary_branch_id = None
        for row in rows.values():
            row.is_active = True
    else:
        keep = rows["new-cairo"]
        workspace.primary_branch_id = keep.id
        for row in rows.values():
            row.is_active = row.id == keep.id
    db.flush()
    clear_catalog()
    return rows


def create_patient(db: Session, workspace: Workspace, tag: str, phone_suffix: int) -> Patient:
    patient = Patient(
        id=uuid4(),
        workspace_id=workspace.id,
        first_name="Audit",
        last_name=tag,
        phone=f"+20000025{phone_suffix:04d}",
        phone_normalized=f"+20000025{phone_suffix:04d}",
        preferred_language="ar",
        source="website",
        status="active",
        source_detail=f"post241-e2e-audit-{tag}",
    )
    db.add(patient)
    db.flush()
    return patient


def active_task_dict(db: Session, workspace: Workspace, patient: Patient, conversation_id: UUID | None) -> dict[str, Any] | None:
    if conversation_id is None:
        return None
    handle = load_active_task(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        conversation_id=conversation_id,
    )
    if handle is None:
        return None
    return handle.active_task.model_dump(mode="json")


def proposal_from_task(task: dict[str, Any] | None) -> dict[str, Any] | None:
    raw = (task or {}).get("proposal") if isinstance(task, dict) else None
    return raw if isinstance(raw, dict) else None


def additional_rows(db: Session, appointment_id: UUID) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(AppointmentAdditionalService)
            .where(AppointmentAdditionalService.appointment_id == appointment_id)
            .order_by(AppointmentAdditionalService.created_at, AppointmentAdditionalService.id)
        )
    )
    services = {
        row.id: row
        for row in db.scalars(select(Service).where(Service.id.in_([item.service_id for item in rows])))
    } if rows else {}
    return [
        {
            "row_id": str(row.id),
            "service_id": str(row.service_id),
            "service_slug": services[row.service_id].slug if row.service_id in services else None,
            "service_name": row.service_name,
            "unit_price_minor": row.unit_price_minor,
            "currency": row.currency,
            "billing_context": row.billing_context,
            "laser_device_key": row.laser_device_key,
            "laser_device_name": row.laser_device_name,
            "laser_pulses_used": row.laser_pulses_used,
            "pulse_resolution": row.pulse_resolution,
            "pulse_resolution_pulse_pack_id": str(row.pulse_resolution_pulse_pack_id) if row.pulse_resolution_pulse_pack_id else None,
            "pulse_overage_unit_price_minor": row.pulse_overage_unit_price_minor,
            "pulse_overage_charge_minor": row.pulse_overage_charge_minor,
        }
        for row in rows
    ]


def appointment_rows(db: Session, patient: Patient) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Appointment, Service, Branch, Doctor, Staff)
        .join(Service, (Service.workspace_id == Appointment.workspace_id) & (Service.id == Appointment.service_id))
        .join(Branch, (Branch.workspace_id == Appointment.workspace_id) & (Branch.id == Appointment.branch_id))
        .join(Doctor, (Doctor.workspace_id == Appointment.workspace_id) & (Doctor.id == Appointment.doctor_id))
        .join(Staff, (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id))
        .where(Appointment.patient_id == patient.id)
        .order_by(Appointment.start_at, Appointment.created_at, Appointment.id)
    ).all()
    result: list[dict[str, Any]] = []
    for appt, service, branch, doctor, staff in rows:
        result.append(
            {
                "appointment_id": str(appt.id),
                "patient_id": str(appt.patient_id),
                "service_id": str(appt.service_id),
                "service_slug": service.slug,
                "service_name": service.name,
                "service_price_minor": service.price_minor,
                "service_currency": service.currency,
                "branch_id": str(appt.branch_id),
                "branch_name": branch.name,
                "doctor_id": str(appt.doctor_id),
                "doctor_name": " ".join(part for part in (staff.first_name, staff.last_name) if part),
                "start_at": appt.start_at.isoformat(),
                "end_at": appt.end_at.isoformat(),
                "start_local": appt.start_at.astimezone(CAIRO).isoformat(),
                "end_local": appt.end_at.astimezone(CAIRO).isoformat(),
                "duration_minutes": appt.duration_minutes,
                "status": appt.status,
                "source": appt.source,
                "visit_group_id": str(appt.visit_group_id) if appt.visit_group_id else None,
                "laser_device_key": appt.laser_device_key,
                "laser_device_name": appt.laser_device_name,
                "billing_context": appt.billing_context,
                "additional_services": additional_rows(db, appt.id),
            }
        )
    return result


def count_rows(db: Session, patient_id: UUID) -> dict[str, int]:
    appointment_ids = select(Appointment.id).where(Appointment.patient_id == patient_id)
    return {
        "appointments": int(db.scalar(select(func.count()).select_from(Appointment).where(Appointment.patient_id == patient_id)) or 0),
        "additional_services": int(
            db.scalar(
                select(func.count())
                .select_from(AppointmentAdditionalService)
                .where(AppointmentAdditionalService.appointment_id.in_(appointment_ids))
            )
            or 0
        ),
    }


def service_set(row: dict[str, Any]) -> list[str]:
    return [row["service_slug"], *[item["service_slug"] for item in row["additional_services"]]]


def appointment_by_local(rows: list[dict[str, Any]], date: str, time: str) -> dict[str, Any] | None:
    prefix = f"{date}T{time}"
    return next((row for row in rows if row["start_local"].startswith(prefix)), None)


class Flow:
    def __init__(self, db: Session, workspace: Workspace, patient: Patient, scenario: str):
        self.db = db
        self.workspace = workspace
        self.patient = patient
        self.scenario = scenario
        self.conversation_id: UUID | None = None
        self.turn_no = 0
        self.events: list[dict[str, Any]] = []
        self.task_history: list[dict[str, Any] | None] = []

    def task(self) -> dict[str, Any] | None:
        return active_task_dict(self.db, self.workspace, self.patient, self.conversation_id)

    def proposal(self) -> dict[str, Any] | None:
        return proposal_from_task(self.task())

    def counts(self) -> dict[str, int]:
        return count_rows(self.db, self.patient.id)

    def send(self, text: str) -> str:
        before = self.counts()
        self.turn_no += 1
        response, capture = send_turn(
            self.db,
            self.workspace,
            self.patient,
            self.scenario,
            self.turn_no,
            text,
            self.conversation_id,
        )
        self.conversation_id = response.conversation_id
        task = self.task()
        after = self.counts()
        self.events.append(
            {
                "kind": "text",
                "before": before,
                "after": after,
                **summarize_capture(capture),
                "active_task": task,
            }
        )
        self.task_history.append(task)
        return response.reply or ""

    def button(self, action: str, proposal_id: str) -> str:
        if self.conversation_id is None:
            raise RuntimeError("button requires an existing conversation")
        conversation = self.db.get(Conversation, self.conversation_id)
        if conversation is None:
            raise RuntimeError("conversation missing")
        if action not in {"confirm", "edit"}:
            raise ValueError(action)
        title = "╪¬╪ú┘â┘è╪» ╪º┘ä╪¡╪¼╪▓" if action == "confirm" else "╪¬╪╣╪»┘è┘ä"
        prefix = "tia.booking.proposal.confirm:" if action == "confirm" else "tia.booking.proposal.edit:"
        inbound = Message(
            workspace_id=self.workspace.id,
            conversation_id=conversation.id,
            channel_connection_id=conversation.channel_connection_id,
            sender_type="patient",
            direction="inbound",
            message_type="interactive",
            content=title,
            delivery_status="received",
            metadata_json={
                "interactive_reply": {
                    "type": "button_reply",
                    "id": f"{prefix}{proposal_id}",
                    "title": title,
                },
                "agent_run_id": str(uuid4()),
            },
        )
        before = self.counts()
        self.db.add(inbound)
        self.db.flush()
        response = process_whatsapp_booking_action(
            self.db,
            workspace=self.workspace,
            patient=self.patient,
            conversation=conversation,
            inbound=inbound,
        )
        if response is None:
            raise RuntimeError("structured button was not handled")
        task = self.task()
        after = self.counts()
        self.events.append(
            {
                "kind": f"button_{action}",
                "proposal_id": proposal_id,
                "customer": f"[{title}]",
                "agent": response.reply,
                "model": response.model,
                "before": before,
                "after": after,
                "active_task": task,
            }
        )
        self.task_history.append(task)
        return response.reply or ""

    def ensure_proposal(self, *followups: str) -> dict[str, Any] | None:
        current = self.proposal()
        if current is not None:
            return current
        queue = [*followups, "╪ú┘è┘ê┘ç ┘â┘à┘ä ╪º┘ä╪¡╪¼╪▓", "╪ú┘è┘ê┘ç ╪º╪¡╪¼╪▓", "╪¬┘à╪º┘à ┘â┘à┘ä"]
        seen: set[str] = set()
        for text in queue:
            if text in seen:
                continue
            seen.add(text)
            before = self.counts()
            self.send(text)
            current = self.proposal()
            if current is not None:
                return current
            if self.counts()["appointments"] > before["appointments"]:
                return None
        return None


def false_success_count(flow: Flow) -> int:
    count = 0
    for event in flow.events:
        before = event.get("before") or {}
        after = event.get("after") or {}
        if before.get("appointments") != after.get("appointments"):
            continue
        reply = str(event.get("agent") or "").casefold()
        if any(word.casefold() in reply for word in SUCCESS_WORDS):
            count += 1
    return count


def proposal_check(
    proposal: dict[str, Any] | None,
    services: dict[str, Service],
    expected_slugs: list[str],
    expected_main_slug: str,
) -> tuple[bool, str]:
    if proposal is None:
        return False, "proposal missing"
    expected_ids = [str(services[slug].id) for slug in expected_slugs]
    main_id = str(services[expected_main_slug].id)
    actual_ids = [str(value) for value in proposal.get("service_ids") or []]
    if actual_ids != expected_ids:
        return False, f"proposal service_ids mismatch: {actual_ids} != {expected_ids}"
    if str(proposal.get("main_service_id")) != main_id:
        return False, f"proposal main mismatch: {proposal.get('main_service_id')} != {main_id}"
    expected_additional = [value for value in expected_ids if value != main_id]
    if [str(value) for value in proposal.get("additional_service_ids") or []] != expected_additional:
        return False, "proposal additional services mismatch"
    return True, "ok"


def booking_check(
    row: dict[str, Any] | None,
    services: dict[str, Service],
    expected_slugs: list[str],
    expected_main_slug: str,
    *,
    expected_date: str | None = None,
    expected_time: str | None = None,
    expected_doctor: str | None = None,
    expected_branch: str | None = None,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if row is None:
        return False, ["appointment missing"]
    actual_set = service_set(row)
    if actual_set != expected_slugs:
        errors.append(f"canonical service representation mismatch: {actual_set} != {expected_slugs}")
    if row["service_slug"] != expected_main_slug:
        errors.append(f"main service mismatch: {row['service_slug']} != {expected_main_slug}")
    if len(row["additional_services"]) != max(0, len(expected_slugs) - 1):
        errors.append("additional service row count mismatch")
    if len(actual_set) != len(set(actual_set)):
        errors.append("duplicate service rows")
    if row["duration_minutes"] != services[expected_main_slug].duration_minutes:
        errors.append(
            f"reserved duration mismatch: {row['duration_minutes']} != {services[expected_main_slug].duration_minutes}"
        )
    start = datetime.fromisoformat(row["start_at"])
    end = datetime.fromisoformat(row["end_at"])
    if int((end - start).total_seconds() // 60) != row["duration_minutes"]:
        errors.append("end-start differs from reserved duration")
    if row["source"] != "ai":
        errors.append(f"source mismatch: {row['source']}")
    if expected_date and not row["start_local"].startswith(f"{expected_date}T"):
        errors.append(f"date mismatch: {row['start_local']}")
    if expected_time and not row["start_local"].startswith(f"{expected_date}T{expected_time}"):
        errors.append(f"time mismatch: {row['start_local']}")
    if expected_doctor and expected_doctor not in row["doctor_name"]:
        errors.append(f"doctor mismatch: {row['doctor_name']}")
    if expected_branch and expected_branch not in row["branch_name"]:
        errors.append(f"branch mismatch: {row['branch_name']}")
    return not errors, errors


def new_rows(flow: Flow, before_ids: set[str]) -> list[dict[str, Any]]:
    return [row for row in appointment_rows(flow.db, flow.patient) if row["appointment_id"] not in before_ids]


def verdict(checks: list[tuple[bool, str]], flow: Flow, *, p1_if_write_mismatch: bool = True) -> dict[str, Any]:
    failed = [detail for ok, detail in checks if not ok]
    false_success = false_success_count(flow)
    if false_success:
        failed.append(f"false success replies: {false_success}")
    severity = None
    if failed:
        any_write = flow.counts()["appointments"] > 0
        severity = "P1" if p1_if_write_mismatch and any_write else "P2"
    return {
        "verdict": "PASS" if not failed else "FAIL",
        "severity": severity,
        "failures": failed,
        "false_success": false_success,
        "appointments": appointment_rows(flow.db, flow.patient),
        "events": flow.events,
        "final_task": flow.task(),
    }


@contextmanager
def rollback_env(tag: str, phone_suffix: int, *, multi_location: bool = False):
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    workspace_id = None
    patient_id = None
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if workspace is None:
            raise RuntimeError("synthetic workspace tia missing")
        assert_demo_only(workspace)
        set_branch_mode(db, workspace, multi_location=multi_location)
        patient = create_patient(db, workspace, tag, phone_suffix)
        workspace_id = workspace.id
        patient_id = patient.id
        services = service_rows(db, workspace)
        flow = Flow(db, workspace, patient, tag)
        yield flow, services
    finally:
        clear_catalog()
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
    if workspace_id is not None and patient_id is not None:
        with Session(engine) as verify:
            persisted = count_rows(verify, patient_id)
        rollback_env.persisted = persisted
    else:
        rollback_env.persisted = {"appointments": -1, "additional_services": -1}


def finish_rollback_result(result: dict[str, Any]) -> dict[str, Any]:
    result["persisted_after_rollback"] = dict(rollback_env.persisted)
    result["persisted_audit_write"] = rollback_env.persisted != {"appointments": 0, "additional_services": 0}
    if result["persisted_audit_write"]:
        result["verdict"] = "FAIL"
        result["severity"] = "P1"
        result.setdefault("failures", []).append("audit rows persisted after rollback")
    return result


def s1() -> dict[str, Any]:
    with rollback_env("S1", 1) as (f, s):
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å")
        f.send("╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        p = f.ensure_proposal("╪¬┘à╪º┘à")
        p_ok, p_detail = proposal_check(p, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"])
        before = {row["appointment_id"] for row in appointment_rows(f.db, f.patient)}
        if p:
            f.button("confirm", p["proposal_id"])
        created = new_rows(f, before)
        b_ok, b_errors = booking_check(created[0] if len(created) == 1 else None, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"], expected_date="2026-10-13", expected_time="16:00", expected_doctor="┘è┘ê╪│┘ü ╪│┘à┘è╪▒", expected_branch="╪º┘ä╪¬╪¼┘à╪╣")
        result = verdict([(p_ok, p_detail), (len(created) == 1, f"created rows={len(created)}"), (b_ok, "; ".join(b_errors))], f)
        result["proposal"] = p
    return finish_rollback_result(result)


def s2() -> dict[str, Any]:
    with rollback_env("S2", 2) as (f, s):
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ┘ä┘è╪▓╪▒ ┘ê╪¼┘ç ┘â╪º┘à┘ä ┘ê╪Ñ╪¿╪╖")
        f.send("╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 11 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        p = f.ensure_proposal("╪¬┘à╪º┘à")
        p_ok, p_detail = proposal_check(p, s, [SLUG["face"], SLUG["underarm"]], SLUG["face"])
        before = {row["appointment_id"] for row in appointment_rows(f.db, f.patient)}
        if p:
            f.send("╪ú┘è┘ê┘ç ╪ú┘â╪»┘è ╪º┘ä╪¡╪¼╪▓")
        created = new_rows(f, before)
        b_ok, b_errors = booking_check(created[0] if len(created) == 1 else None, s, [SLUG["face"], SLUG["underarm"]], SLUG["face"], expected_date="2026-10-13", expected_time="11:00", expected_doctor="┘à╪▒┘è┘à ╪¡╪│┘å")
        result = verdict([(p_ok, p_detail), (len(created) == 1, f"created rows={len(created)}"), (b_ok, "; ".join(b_errors))], f)
        result["proposal"] = p
    return finish_rollback_result(result)


def s3() -> dict[str, Any]:
    with rollback_env("S3", 3) as (f, s):
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å")
        f.send("┘ä╪º ╪┤┘è┘ä ╪º┘ä╪░╪▒╪º╪╣┘è┘å ┘ê╪«┘ä┘è ╪º┘ä╪Ñ╪¿╪╖ ┘ê╪º┘ä╪¿┘â┘è┘å┘è ╪¿╪│")
        corrected_task = f.task()
        full_arms_id = str(s[SLUG["full_arms"]].id)
        task_clean = full_arms_id not in json.dumps(corrected_task, ensure_ascii=False, default=str)
        f.send("╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 11 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        p = f.ensure_proposal("╪¬┘à╪º┘à")
        p_ok, p_detail = proposal_check(p, s, [SLUG["combo"]], SLUG["combo"])
        if p:
            f.send("╪ú┘è┘ê┘ç ╪ú┘â╪»┘è ╪º┘ä╪¡╪¼╪▓")
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_errors = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"]], SLUG["combo"], expected_date="2026-10-13", expected_time="11:00")
        result = verdict([(task_clean, "Full Arms remained in active task after correction"), (p_ok, p_detail), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_errors))], f)
        result["task_after_correction"] = corrected_task
        result["proposal"] = p
    return finish_rollback_result(result)


def s4() -> dict[str, Any]:
    with rollback_env("S4", 4) as (f, s):
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        p1 = f.ensure_proposal("╪¬┘à╪º┘à")
        if p1:
            f.button("edit", p1["proposal_id"])
        invalidated = f.proposal() is None
        f.send("╪▓┘ê╪» ╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å")
        p2 = f.ensure_proposal("╪¬┘à╪º┘à")
        p_ok, p_detail = proposal_check(p2, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"])
        new_id = bool(p1 and p2 and p1["proposal_id"] != p2["proposal_id"])
        if p2:
            f.button("confirm", p2["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_errors = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"], expected_time="16:00", expected_date="2026-10-13")
        result = verdict([(p1 is not None, "initial proposal missing"), (invalidated, "old proposal not invalidated by Edit"), (new_id, "new proposal id was not generated"), (p_ok, p_detail), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_errors))], f)
        result["proposal_a"] = p1
        result["proposal_b"] = p2
    return finish_rollback_result(result)


def s5() -> dict[str, Any]:
    with rollback_env("S5", 5) as (f, s):
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        p1 = f.ensure_proposal("╪¬┘à╪º┘à")
        if p1:
            f.button("edit", p1["proposal_id"])
        f.send("╪«┘ä┘è┘ç ╪º┘ä╪│╪º╪╣╪⌐ 6 ┘à╪│╪º╪í┘ï ╪¿╪»┘ä 4")
        p2 = f.ensure_proposal("╪¬┘à╪º┘à")
        same_services = bool(p1 and p2 and p1["service_ids"] == p2["service_ids"])
        same_branch = bool(p1 and p2 and p1["branch_id"] == p2["branch_id"])
        same_doctor = bool(p1 and p2 and p1["doctor_id"] == p2["doctor_id"])
        changed_id = bool(p1 and p2 and p1["proposal_id"] != p2["proposal_id"])
        new_time = bool(p2 and datetime.fromisoformat(p2["start_at"]).astimezone(CAIRO).strftime("%H:%M") == "18:00")
        if p2:
            f.button("confirm", p2["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_errors = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"], expected_date="2026-10-13", expected_time="18:00", expected_doctor="┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        result = verdict([(p1 is not None, "initial proposal missing"), (p2 is not None, "edited proposal missing"), (same_services, "services changed on time-only edit"), (same_branch, "branch changed on time-only edit"), (same_doctor, "doctor changed on time-only edit"), (changed_id, "proposal id did not change"), (new_time, "edited proposal did not move to 18:00"), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_errors))], f)
        result["proposal_a"] = p1
        result["proposal_b"] = p2
    return finish_rollback_result(result)


def complete_text_booking(f: Flow, initial: str, followup: str = "╪ú┘è┘ê┘ç ┘â┘à┘ä ╪º┘ä╪¡╪¼╪▓") -> dict[str, Any] | None:
    before = f.counts()["appointments"]
    f.send(initial)
    p = f.ensure_proposal(followup)
    if p:
        f.send("╪ú┘è┘ê┘ç ╪ú┘â╪»┘è ╪º┘ä╪¡╪¼╪▓")
    completed = f.counts()["appointments"] > before
    if p is not None:
        return p
    if completed:
        return {"direct_booking": True}
    return None


def s6() -> dict[str, Any]:
    with rollback_env("S6", 6) as (f, s):
        p1 = complete_text_booking(f, "╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 10 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        rows1 = appointment_rows(f.db, f.patient)
        p2 = complete_text_booking(f, "┘ê╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ┘ä┘è╪▓╪▒ ┘ê╪¼┘ç ┘â╪º┘à┘ä ┘â┘à╪º┘å ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪│╪º╪╣╪⌐ 3 ╪º┘ä╪╣╪╡╪▒ ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        rows = appointment_rows(f.db, f.patient)
        a = appointment_by_local(rows, "2026-10-13", "10:00")
        b = appointment_by_local(rows, "2026-10-13", "15:00")
        a_ok, a_err = booking_check(a, s, [SLUG["combo"]], SLUG["combo"], expected_date="2026-10-13", expected_time="10:00")
        b_ok, b_err = booking_check(b, s, [SLUG["face"]], SLUG["face"], expected_date="2026-10-13", expected_time="15:00")
        result = verdict([(p1 is not None, "Booking A proposal missing"), (len(rows1) == 1, f"Booking A rows={len(rows1)}"), (p2 is not None, "Booking B proposal missing"), (len(rows) == 2, f"final rows={len(rows)}"), (a_ok, "; ".join(a_err)), (b_ok, "; ".join(b_err))], f)
        result["cross_booking_state_leak"] = len(rows) >= 2 and not (a_ok and b_ok)
    return finish_rollback_result(result)


def s7() -> dict[str, Any]:
    with rollback_env("S7", 7, multi_location=True) as (f, s):
        p1 = complete_text_booking(f, "╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 10 ╪╡╪¿╪º╪¡┘ï╪º ┘ü┘è ┘ü╪▒╪╣ ╪º┘ä╪¬╪¼┘à╪╣ ╪º┘ä╪«╪º┘à╪│ ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        p2 = complete_text_booking(f, "┘ê╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å ╪º┘ä╪│╪¿╪¬ ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 11 ╪╡╪¿╪º╪¡┘ï╪º ┘ü┘è ┘ü╪▒╪╣ ╪º┘ä╪┤┘è╪« ╪▓╪º┘è╪» ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        rows = appointment_rows(f.db, f.patient)
        a = appointment_by_local(rows, "2026-10-13", "10:00")
        b = appointment_by_local(rows, "2026-10-17", "11:00")
        a_ok, a_err = booking_check(a, s, [SLUG["combo"]], SLUG["combo"], expected_date="2026-10-13", expected_time="10:00", expected_branch="╪º┘ä╪¬╪¼┘à╪╣")
        b_ok, b_err = booking_check(b, s, [SLUG["full_arms"]], SLUG["full_arms"], expected_date="2026-10-17", expected_time="11:00", expected_branch="╪º┘ä╪┤┘è╪« ╪▓╪º┘è╪»")
        result = verdict([(p1 is not None, "Booking A proposal missing"), (p2 is not None, "Booking B proposal missing"), (len(rows) == 2, f"final rows={len(rows)}"), (a_ok, "; ".join(a_err)), (b_ok, "; ".join(b_err))], f)
        result["cross_booking_state_leak"] = len(rows) >= 2 and not (a_ok and b_ok)
    return finish_rollback_result(result)


def s8() -> dict[str, Any]:
    with rollback_env("S8", 8) as (f, s):
        p1 = complete_text_booking(f, "╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        p2 = complete_text_booking(f, "╪¬┘à╪º┘à╪î ┘ê╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪¬┘å╪╕┘è┘ü ╪¿╪┤╪▒╪⌐ ╪╣┘à┘è┘é ╪º┘ä╪│╪¿╪¬ ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 17:00 ┘à╪╣ ╪». ┘ç╪º┘ä╪⌐ ┘à╪╡╪╖┘ü┘ë")
        rows = appointment_rows(f.db, f.patient)
        a = appointment_by_local(rows, "2026-10-13", "16:00")
        b = appointment_by_local(rows, "2026-10-17", "17:00")
        a_ok, a_err = booking_check(a, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"], expected_date="2026-10-13", expected_time="16:00")
        b_ok, b_err = booking_check(b, s, [SLUG["deep"]], SLUG["deep"], expected_date="2026-10-17", expected_time="17:00")
        result = verdict([(p1 is not None, "Booking A proposal missing"), (p2 is not None, "Booking B proposal missing"), (len(rows) == 2, f"rows={len(rows)}"), (a_ok, "; ".join(a_err)), (b_ok, "; ".join(b_err))], f)
        result["cross_booking_state_leak"] = len(rows) >= 2 and not (a_ok and b_ok)
    return finish_rollback_result(result)


def s9() -> dict[str, Any]:
    with rollback_env("S9", 9) as (f, s):
        f.send("╪╣╪º┘è╪▓╪⌐ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å")
        f.send("╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è")
        f.send("╪º┘ä╪»┘â╪¬┘ê╪▒ ┘à┘è┘å╪ƒ")
        f.send("╪╖╪¿ ╪º┘ä╪│╪╣╪▒ ┘â╪º┘à╪ƒ")
        f.send("╪¬┘à╪º┘à ┘â┘à┘ä")
        p = f.proposal()
        if p is None:
            f.send("╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
            p = f.ensure_proposal("╪¬┘à╪º┘à")
        p_ok, p_detail = proposal_check(p, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"])
        if p:
            f.send("╪ú┘è┘ê┘ç ╪ú┘â╪»┘è ╪º┘ä╪¡╪¼╪▓")
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_err = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"])
        result = verdict([(p_ok, p_detail), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_err))], f)
    return finish_rollback_result(result)


def s10() -> dict[str, Any]:
    with rollback_env("S10", 10) as (f, s):
        p1 = complete_text_booking(f, "╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 10 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        before_side = f.counts()
        side_reply = f.send("╪º┘ä╪╣┘è╪º╪»╪⌐ ╪¿╪¬┘é┘ü┘ä ╪º┘ä╪│╪º╪╣╪⌐ ┘â╪º┘à╪ƒ")
        after_side = f.counts()
        p2 = complete_text_booking(f, "╪¬┘à╪º┘à╪î ╪º╪¡╪¼╪▓┘ä┘è ┘ä┘è╪▓╪▒ ┘ê╪¼┘ç ┘â╪º┘à┘ä ╪º┘ä╪ú╪¡╪» ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 10:15 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        rows = appointment_rows(f.db, f.patient)
        a = appointment_by_local(rows, "2026-10-13", "10:00")
        b = appointment_by_local(rows, "2026-10-18", "10:15")
        a_ok, a_err = booking_check(a, s, [SLUG["combo"]], SLUG["combo"])
        b_ok, b_err = booking_check(b, s, [SLUG["face"]], SLUG["face"])
        result = verdict([(p1 is not None, "Booking A proposal missing"), (before_side == after_side, "side question changed DB counts"), (bool(side_reply), "side question had no answer"), (p2 is not None, "Booking B proposal missing"), (len(rows) == 2, f"rows={len(rows)}"), (a_ok, "; ".join(a_err)), (b_ok, "; ".join(b_err))], f)
        result["cross_booking_state_leak"] = len(rows) >= 2 and not (a_ok and b_ok)
    return finish_rollback_result(result)


def s11() -> dict[str, Any]:
    with rollback_env("S11", 11) as (f, s):
        p1 = complete_text_booking(f, "╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 10 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        p2 = complete_text_booking(f, "┘ê╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪│╪º╪╣╪⌐ 17:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        rows = appointment_rows(f.db, f.patient)
        a = appointment_by_local(rows, "2026-10-13", "10:00")
        b = appointment_by_local(rows, "2026-10-13", "17:00")
        a_ok, a_err = booking_check(a, s, [SLUG["combo"]], SLUG["combo"], expected_doctor="┘à╪▒┘è┘à ╪¡╪│┘å")
        b_ok, b_err = booking_check(b, s, [SLUG["full_arms"]], SLUG["full_arms"], expected_doctor="┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        result = verdict([(p1 is not None, "A proposal missing"), (p2 is not None, "B proposal missing"), (len(rows) == 2, f"rows={len(rows)}"), (a_ok, "; ".join(a_err)), (b_ok, "; ".join(b_err))], f)
        result["cross_booking_state_leak"] = len(rows) >= 2 and not (a_ok and b_ok)
    return finish_rollback_result(result)


def s12() -> dict[str, Any]:
    with rollback_env("S12", 12) as (f, s):
        p1 = complete_text_booking(f, "╪╣╪º┘è╪▓╪⌐ Full Face ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 11 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        p2 = complete_text_booking(f, "┘ê╪º╪¡╪¼╪▓┘ä┘è Full Face ┘â┘à╪º┘å ╪º┘ä╪ú╪¡╪» ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 10:15 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        rows = appointment_rows(f.db, f.patient)
        a = appointment_by_local(rows, "2026-10-13", "11:00")
        b = appointment_by_local(rows, "2026-10-18", "10:15")
        a_ok, a_err = booking_check(a, s, [SLUG["face"]], SLUG["face"])
        b_ok, b_err = booking_check(b, s, [SLUG["face"]], SLUG["face"])
        result = verdict([(p1 is not None, "A proposal missing"), (p2 is not None, "B proposal missing"), (len(rows) == 2, f"rows={len(rows)}"), (a_ok, "; ".join(a_err)), (b_ok, "; ".join(b_err))], f)
        result["cross_booking_state_leak"] = len(rows) >= 2 and not (a_ok and b_ok)
    return finish_rollback_result(result)


def s13() -> dict[str, Any]:
    with rollback_env("S13", 13) as (f, s):
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪▒╪¼┘ä┘è┘å ┘â╪º┘à┘ä╪¬┘è┘å ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 17:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        p1 = f.ensure_proposal("╪¬┘à╪º┘à")
        p1_ok, p1_detail = proposal_check(p1, s, [SLUG["underarm"], SLUG["full_legs"], SLUG["full_arms"]], SLUG["full_legs"])
        if p1:
            f.button("edit", p1["proposal_id"])
        f.send("╪┤┘è┘ä ╪º┘ä╪▒╪¼┘ä┘è┘å ╪º┘ä┘â╪º┘à┘ä╪¬┘è┘å")
        p2 = f.ensure_proposal("╪¬┘à╪º┘à")
        p2_ok, p2_detail = proposal_check(p2, s, [SLUG["underarm"], SLUG["full_arms"]], SLUG["full_arms"])
        if p2:
            f.button("confirm", p2["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_err = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["full_arms"], SLUG["underarm"]], SLUG["full_arms"])
        # Canonical proposal order may preserve request order; DB representation always starts with main.
        result = verdict([(p1_ok, p1_detail), (p2_ok, p2_detail), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_err))], f)
        result["proposal_a"] = p1
        result["proposal_b"] = p2
    return finish_rollback_result(result)


def s14() -> dict[str, Any]:
    with rollback_env("S14", 14) as (f, s):
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪Ñ╪¿╪╖ ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 11 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        p = f.ensure_proposal("╪¬┘à╪º┘à")
        p_ok, p_detail = proposal_check(p, s, [SLUG["combo"]], SLUG["combo"])
        if p:
            f.button("confirm", p["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_err = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"]], SLUG["combo"])
        result = verdict([(p_ok, p_detail), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_err))], f)
    return finish_rollback_result(result)


def s15() -> dict[str, Any]:
    with rollback_env("S15", 15) as (f, s):
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 11 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        p1 = f.ensure_proposal("╪¬┘à╪º┘à")
        if p1:
            f.button("edit", p1["proposal_id"])
        f.send("╪«┘ä┘è┘ç ╪º┘ä╪│╪º╪╣╪⌐ 12 ╪¿╪»┘ä 11")
        p2 = f.ensure_proposal("╪¬┘à╪º┘à")
        before_stale = f.counts()
        stale_reply = f.button("confirm", p1["proposal_id"]) if p1 else ""
        after_stale = f.counts()
        stale_safe = before_stale == after_stale
        if p2:
            f.button("confirm", p2["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_err = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"]], SLUG["combo"], expected_date="2026-10-13", expected_time="12:00")
        result = verdict([(p1 is not None, "proposal A missing"), (p2 is not None, "proposal B missing"), (bool(p1 and p2 and p1["proposal_id"] != p2["proposal_id"]), "proposal version/id did not change"), (stale_safe, "stale proposal A produced a write"), ("┘ä┘à ┘è╪╣╪» ╪╡╪º┘ä╪¡" in stale_reply, f"unexpected stale reply: {stale_reply}"), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_err))], f)
        result["stale_proposal_write"] = not stale_safe
        result["proposal_a"] = p1
        result["proposal_b"] = p2
    return finish_rollback_result(result)


def s16() -> dict[str, Any]:
    with rollback_env("S16", 16) as (f, s):
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        p = f.ensure_proposal("╪¬┘à╪º┘à")
        before = f.counts()
        first_reply = f.button("confirm", p["proposal_id"]) if p else ""
        after_first = f.counts()
        second_reply = f.button("confirm", p["proposal_id"]) if p else ""
        after_second = f.counts()
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_err = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"])
        one_write = after_first["appointments"] == before["appointments"] + 1 and after_second == after_first
        result = verdict([(p is not None, "proposal missing"), (one_write, f"counts before={before} first={after_first} second={after_second}"), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_err)), ("╪¬┘à ╪¬╪ú┘â┘è╪»" in first_reply, f"first confirm reply unexpected: {first_reply}"), ("┘ä┘à ┘è╪╣╪» ╪╡╪º┘ä╪¡" in second_reply, f"second confirm reply unexpected: {second_reply}")], f)
        result["duplicate_appointments"] = max(0, after_second["appointments"] - before["appointments"] - 1)
    return finish_rollback_result(result)


SCENARIOS = {
    "S1": s1,
    "S2": s2,
    "S3": s3,
    "S4": s4,
    "S5": s5,
    "S6": s6,
    "S7": s7,
    "S8": s8,
    "S9": s9,
    "S10": s10,
    "S11": s11,
    "S12": s12,
    "S13": s13,
    "S14": s14,
    "S15": s15,
    "S16": s16,
}


def ensure_auth_user(db: Session, workspace: Workspace) -> None:
    user = db.scalar(select(User).where(User.auth_user_id == AUDIT_AUTH_USER_ID))
    if user is None:
        user = User(auth_user_id=AUDIT_AUTH_USER_ID, email=AUDIT_EMAIL, full_name="Audit Reception", is_active=True)
        db.add(user)
        db.flush()
    membership = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if membership is None:
        membership = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="admin", is_active=True)
        db.add(membership)
    else:
        membership.role = "admin"
        membership.is_active = True
    db.flush()


def ui_mode() -> int:
    evidence: dict[str, Any] = {
        "main_sha": "11fecd60c25d1d3afc2fcc6eb928fd0a8cc13ec8",
        "environment": settings.environment,
        "agent_runtime": "Agent V2 live",
        "model": settings.openai_model,
        "run_started_at": datetime.now(UTC).isoformat(),
        "bookings": {},
    }
    with Session(engine, expire_on_commit=False) as db:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if workspace is None:
            raise RuntimeError("synthetic workspace tia missing")
        assert_demo_only(workspace)
        rows = set_branch_mode(db, workspace, multi_location=False)
        ensure_auth_user(db, workspace)
        patient = create_patient(db, workspace, "UI", 9999)
        patient.first_name = "Post241"
        patient.last_name = "E2E UI"
        patient.source_detail = "post241-ui-agent-e2e"
        db.commit()
        clear_catalog()
        services = service_rows(db, workspace)
        flow = Flow(db, workspace, patient, "UI")

        # A: actual multi-service booking via structured button.
        flow.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å")
        flow.send("╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        pa = flow.ensure_proposal("╪¬┘à╪º┘à")
        if pa:
            flow.button("confirm", pa["proposal_id"])
        db.commit()
        rows_a = appointment_rows(db, patient)
        a = appointment_by_local(rows_a, "2026-10-13", "16:00")
        a_ok, a_errors = booking_check(a, services, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"], expected_date="2026-10-13", expected_time="16:00")
        evidence["bookings"]["A"] = {"proposal": pa, "appointment": a, "pass": a_ok, "errors": a_errors}

        # B: distinct booking at a different time, same patient/day, text confirm.
        flow.send("┘ê╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ┘ä┘è╪▓╪▒ ┘ê╪¼┘ç ┘â╪º┘à┘ä ┘â┘à╪º┘å ╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪│╪º╪╣╪⌐ 11 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        pb = flow.ensure_proposal("╪¬┘à╪º┘à")
        if pb:
            flow.send("╪ú┘è┘ê┘ç ╪ú┘â╪»┘è ╪º┘ä╪¡╪¼╪▓")
        db.commit()
        rows_b = appointment_rows(db, patient)
        b = appointment_by_local(rows_b, "2026-10-13", "11:00")
        b_ok, b_errors = booking_check(b, services, [SLUG["face"]], SLUG["face"], expected_date="2026-10-13", expected_time="11:00")
        evidence["bookings"]["B"] = {"proposal": pb, "appointment": b, "pass": b_ok, "errors": b_errors}

        # C: new booking, correction before proposal; Full Arms must disappear everywhere.
        flow.send("┘ê╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å ┘â┘à╪º┘å")
        flow.send("┘ä╪º ╪┤┘è┘ä ╪º┘ä╪░╪▒╪º╪╣┘è┘å ┘ê╪«┘ä┘è ╪º┘ä╪Ñ╪¿╪╖ ┘ê╪º┘ä╪¿┘â┘è┘å┘è ╪¿╪│")
        corrected_task = flow.task()
        flow.send("╪º┘ä╪ú╪¡╪» ╪º┘ä╪¼╪º┘è ╪º┘ä╪│╪º╪╣╪⌐ 10:15 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘à╪▒┘è┘à ╪¡╪│┘å")
        pc = flow.ensure_proposal("╪¬┘à╪º┘à")
        if pc:
            flow.send("╪ú┘è┘ê┘ç ╪ú┘â╪»┘è ╪º┘ä╪¡╪¼╪▓")
        db.commit()
        rows_c = appointment_rows(db, patient)
        c = appointment_by_local(rows_c, "2026-10-18", "10:15")
        c_ok, c_errors = booking_check(c, services, [SLUG["combo"]], SLUG["combo"], expected_date="2026-10-18", expected_time="10:15")
        full_arms_id = str(services[SLUG["full_arms"]].id)
        task_clean = full_arms_id not in json.dumps(corrected_task, ensure_ascii=False, default=str)
        proposal_clean = bool(pc and full_arms_id not in [str(x) for x in pc.get("service_ids") or []])
        ui_rows = appointment_rows(db, patient)
        final_clean = bool(c and SLUG["full_arms"] not in service_set(c))
        evidence["bookings"]["C"] = {
            "proposal": pc,
            "task_after_correction": corrected_task,
            "appointment": c,
            "pass": c_ok and task_clean and proposal_clean and final_clean,
            "errors": c_errors + ([] if task_clean else ["Full Arms stale in active task"]) + ([] if proposal_clean else ["Full Arms stale in proposal"]) + ([] if final_clean else ["Full Arms stale in DB booking"]),
        }

        evidence["workspace_id"] = str(workspace.id)
        evidence["workspace"] = {"id": str(workspace.id), "slug": workspace.slug, "single_active_branch_id": str(rows["new-cairo"].id), "single_active_branch_name": rows["new-cairo"].name}
        evidence["patient"] = {"id": str(patient.id), "name": "Post241 E2E UI", "phone": patient.phone}
        evidence["conversation_id"] = str(flow.conversation_id) if flow.conversation_id else None
        evidence["appointments"] = ui_rows
        evidence["appointment_count"] = len(ui_rows)
        evidence["additional_row_count"] = sum(len(row["additional_services"]) for row in ui_rows)
        evidence["events"] = flow.events
        evidence["run_finished_at"] = datetime.now(UTC).isoformat()
        evidence["all_agent_created"] = all(row["source"] == "ai" for row in ui_rows)
        evidence["pass"] = (
            len(ui_rows) == 3
            and evidence["additional_row_count"] == 1
            and all(item["pass"] for item in evidence["bookings"].values())
            and evidence["all_agent_created"]
        )
        (OUT / "ui-agent-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("UI_AGENT_SUMMARY=" + json.dumps({"pass": evidence["pass"], "appointment_count": len(ui_rows), "additional_rows": evidence["additional_row_count"], "patient_id": str(patient.id)}, ensure_ascii=False))
        return 0 if evidence["pass"] else 1


def rollback_mode() -> int:
    results: dict[str, Any] = {}
    for name, runner in SCENARIOS.items():
        try:
            results[name] = runner()
        except Exception as exc:
            results[name] = {
                "verdict": "FAIL",
                "severity": "P2",
                "failures": [f"runner exception: {type(exc).__name__}: {exc}"],
                "exception": repr(exc),
            }
        (OUT / "real-agent-audit.json").write_text(
            json.dumps({"starting_main": "11fecd60c25d1d3afc2fcc6eb928fd0a8cc13ec8", "environment": settings.environment, "model": settings.openai_model, "runtime": "Agent V2 live", "scenarios": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    failed = [name for name, row in results.items() if row.get("verdict") != "PASS"]
    write_mismatch_markers = (
        "canonical service representation mismatch",
        "main service mismatch",
        "reserved duration mismatch",
        "end-start differs",
        "source mismatch",
        "date mismatch",
        "time mismatch",
        "doctor mismatch",
        "branch mismatch",
        "duplicate service rows",
    )
    wrong_writes = sum(
        1
        for row in results.values()
        if any(any(marker in str(item) for marker in write_mismatch_markers) for item in row.get("failures", []))
        or row.get("stale_proposal_write")
        or int(row.get("duplicate_appointments") or 0) > 0
    )
    partial_writes = sum(
        1
        for row in results.values()
        if any("additional service row count mismatch" in str(item) or "canonical service representation mismatch" in str(item) for item in row.get("failures", []))
        and row.get("appointments")
    )
    false_success = sum(int(row.get("false_success") or 0) for row in results.values())
    duplicate_appointments = sum(int(row.get("duplicate_appointments") or 0) for row in results.values())
    stale_proposal_writes = sum(1 for row in results.values() if row.get("stale_proposal_write"))
    cross_booking_leaks = sum(1 for row in results.values() if row.get("cross_booking_state_leak"))
    persisted_audit_writes = sum(1 for row in results.values() if row.get("persisted_audit_write"))
    summary = {
        "failed": failed,
        "wrong_writes": wrong_writes,
        "partial_writes": partial_writes,
        "false_success": false_success,
        "duplicate_appointments": duplicate_appointments,
        "stale_proposal_writes": stale_proposal_writes,
        "cross_booking_state_leaks": cross_booking_leaks,
        "persisted_audit_writes": persisted_audit_writes,
    }
    payload = {
        "starting_main": "11fecd60c25d1d3afc2fcc6eb928fd0a8cc13ec8",
        "environment": settings.environment,
        "model": settings.openai_model,
        "runtime": "Agent V2 live",
        "scenarios": results,
        "summary": summary,
    }
    (OUT / "real-agent-audit.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("POST241_AUDIT_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("rollback", "ui"), default="rollback")
    args = parser.parse_args()
    return ui_mode() if args.mode == "ui" else rollback_mode()


if __name__ == "__main__":
    raise SystemExit(main())
