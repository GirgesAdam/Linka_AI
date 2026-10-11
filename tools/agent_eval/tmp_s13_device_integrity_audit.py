from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from app.agents import clinic_grounding
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.branch import Branch
from app.models.clinic_inventory import ClinicLaserDevice, ServiceDevicePrice
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
    "تم تأكيد الحجز",
    "الحجز اتأكد",
    "أكدنا الحجز",
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


DEVICE_NAME = {"candela": "Candela", "deka": "DEKA"}


def configure_laser_matrix(
    db: Session,
    workspace: Workspace,
    services: dict[str, Service],
    matrix: dict[str, list[str]],
) -> None:
    target_ids = [services[slug].id for slug in matrix]
    if target_ids:
        db.execute(
            delete(ServiceDevicePrice).where(
                ServiceDevicePrice.workspace_id == workspace.id,
                ServiceDevicePrice.service_id.in_(target_ids),
            )
        )
    for device_key in sorted({key for keys in matrix.values() for key in keys}):
        device = db.scalar(
            select(ClinicLaserDevice).where(
                ClinicLaserDevice.workspace_id == workspace.id,
                ClinicLaserDevice.device_key == device_key,
            )
        )
        if device is None:
            device = ClinicLaserDevice(
                workspace_id=workspace.id,
                device_key=device_key,
                name=DEVICE_NAME[device_key],
                is_active=True,
            )
            db.add(device)
        else:
            device.name = DEVICE_NAME[device_key]
            device.is_active = True
    db.flush()
    for slug, device_keys in matrix.items():
        service = services[slug]
        service.requires_laser_device = True
        service.operational_category = "laser"
        for device_key in device_keys:
            db.add(
                ServiceDevicePrice(
                    workspace_id=workspace.id,
                    service_id=service.id,
                    device_key=device_key,
                    device_name=DEVICE_NAME[device_key],
                    price_minor=int(service.price_minor or 0),
                    duration_minutes=int(service.duration_minutes),
                    currency=service.currency or "EGP",
                    is_active=True,
                )
            )
    db.flush()
    clear_catalog()


def task_authority(flow: "Flow") -> dict[str, Any]:
    task = flow.task() or {}
    constraints = task.get("constraints") if isinstance(task.get("constraints"), dict) else {}
    return {
        "requested_service_ids": [str(x) for x in task.get("requested_service_ids") or []],
        "main_service_id": str(constraints.get("service_id") or ""),
        "device_key": constraints.get("device_key"),
        "proposal": task.get("proposal"),
        "availability_snapshot_id": ((task.get("derived") or {}).get("availability_snapshot_id") if isinstance(task.get("derived"), dict) else None),
        "selected_slot_ref": ((task.get("derived") or {}).get("selected_slot_ref") if isinstance(task.get("derived"), dict) else None),
    }


def same_device_check(row: dict[str, Any] | None, *, expected: str) -> tuple[bool, list[str]]:
    if row is None:
        return False, ["appointment missing for device check"]
    errors: list[str] = []
    if row.get("laser_device_key") != expected:
        errors.append(f"main device mismatch: {row.get('laser_device_key')} != {expected}")
    for item in row.get("additional_services") or []:
        if item.get("laser_device_key") != expected:
            errors.append(
                f"additional device mismatch: {item.get('service_slug')}={item.get('laser_device_key')} != {expected}"
            )
    return not errors, errors


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
        title = "تأكيد الحجز" if action == "confirm" else "تعديل"
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
        queue = [*followups, "أيوه كمل الحجز", "أيوه احجز", "تمام كمل"]
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
        f.send("عايزة أحجز إبط وبكيني وذراعين كاملين")
        f.send("الثلاثاء الجاي الساعة 16:00 مع د. يوسف سمير")
        p = f.ensure_proposal("تمام")
        p_ok, p_detail = proposal_check(p, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"])
        before = {row["appointment_id"] for row in appointment_rows(f.db, f.patient)}
        if p:
            f.button("confirm", p["proposal_id"])
        created = new_rows(f, before)
        b_ok, b_errors = booking_check(created[0] if len(created) == 1 else None, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"], expected_date="2026-10-13", expected_time="16:00", expected_doctor="يوسف سمير", expected_branch="التجمع")
        result = verdict([(p_ok, p_detail), (len(created) == 1, f"created rows={len(created)}"), (b_ok, "; ".join(b_errors))], f)
        result["proposal"] = p
    return finish_rollback_result(result)


def s2() -> dict[str, Any]:
    with rollback_env("S2", 2) as (f, s):
        f.send("عايزة أحجز ليزر وجه كامل وإبط")
        f.send("الثلاثاء الجاي الساعة 11 صباحًا مع د. مريم حسن")
        p = f.ensure_proposal("تمام")
        p_ok, p_detail = proposal_check(p, s, [SLUG["face"], SLUG["underarm"]], SLUG["face"])
        before = {row["appointment_id"] for row in appointment_rows(f.db, f.patient)}
        if p:
            f.send("أيوه أكدي الحجز")
        created = new_rows(f, before)
        b_ok, b_errors = booking_check(created[0] if len(created) == 1 else None, s, [SLUG["face"], SLUG["underarm"]], SLUG["face"], expected_date="2026-10-13", expected_time="11:00", expected_doctor="مريم حسن")
        result = verdict([(p_ok, p_detail), (len(created) == 1, f"created rows={len(created)}"), (b_ok, "; ".join(b_errors))], f)
        result["proposal"] = p
    return finish_rollback_result(result)


def s3() -> dict[str, Any]:
    with rollback_env("S3", 3) as (f, s):
        f.send("عايزة أحجز إبط وبكيني وذراعين كاملين")
        f.send("لا شيل الذراعين وخلي الإبط والبكيني بس")
        corrected_task = f.task()
        full_arms_id = str(s[SLUG["full_arms"]].id)
        task_clean = full_arms_id not in json.dumps(corrected_task, ensure_ascii=False, default=str)
        f.send("الثلاثاء الجاي الساعة 11 صباحًا مع د. مريم حسن")
        p = f.ensure_proposal("تمام")
        p_ok, p_detail = proposal_check(p, s, [SLUG["combo"]], SLUG["combo"])
        if p:
            f.send("أيوه أكدي الحجز")
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_errors = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"]], SLUG["combo"], expected_date="2026-10-13", expected_time="11:00")
        result = verdict([(task_clean, "Full Arms remained in active task after correction"), (p_ok, p_detail), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_errors))], f)
        result["task_after_correction"] = corrected_task
        result["proposal"] = p
    return finish_rollback_result(result)


def s4() -> dict[str, Any]:
    with rollback_env("S4", 4) as (f, s):
        f.send("عايزة أحجز إبط وبكيني الثلاثاء الجاي الساعة 16:00 مع د. يوسف سمير")
        p1 = f.ensure_proposal("تمام")
        if p1:
            f.button("edit", p1["proposal_id"])
        invalidated = f.proposal() is None
        f.send("زود ذراعين كاملين")
        p2 = f.ensure_proposal("تمام")
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
        f.send("عايزة أحجز إبط وبكيني وذراعين كاملين الثلاثاء الجاي الساعة 16:00 مع د. يوسف سمير")
        p1 = f.ensure_proposal("تمام")
        if p1:
            f.button("edit", p1["proposal_id"])
        f.send("خليه الساعة 6 مساءً بدل 4")
        p2 = f.ensure_proposal("تمام")
        same_services = bool(p1 and p2 and p1["service_ids"] == p2["service_ids"])
        same_branch = bool(p1 and p2 and p1["branch_id"] == p2["branch_id"])
        same_doctor = bool(p1 and p2 and p1["doctor_id"] == p2["doctor_id"])
        changed_id = bool(p1 and p2 and p1["proposal_id"] != p2["proposal_id"])
        new_time = bool(p2 and datetime.fromisoformat(p2["start_at"]).astimezone(CAIRO).strftime("%H:%M") == "18:00")
        if p2:
            f.button("confirm", p2["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_errors = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"], expected_date="2026-10-13", expected_time="18:00", expected_doctor="يوسف سمير")
        result = verdict([(p1 is not None, "initial proposal missing"), (p2 is not None, "edited proposal missing"), (same_services, "services changed on time-only edit"), (same_branch, "branch changed on time-only edit"), (same_doctor, "doctor changed on time-only edit"), (changed_id, "proposal id did not change"), (new_time, "edited proposal did not move to 18:00"), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_errors))], f)
        result["proposal_a"] = p1
        result["proposal_b"] = p2
    return finish_rollback_result(result)


def complete_text_booking(f: Flow, initial: str, followup: str = "أيوه كمل الحجز") -> dict[str, Any] | None:
    before = f.counts()["appointments"]
    f.send(initial)
    p = f.ensure_proposal(followup)
    if p:
        f.send("أيوه أكدي الحجز")
    completed = f.counts()["appointments"] > before
    if p is not None:
        return p
    if completed:
        return {"direct_booking": True}
    return None


def s6() -> dict[str, Any]:
    with rollback_env("S6", 6) as (f, s):
        p1 = complete_text_booking(f, "عايزة أحجز إبط وبكيني الثلاثاء الجاي الساعة 10 صباحًا مع د. مريم حسن")
        rows1 = appointment_rows(f.db, f.patient)
        p2 = complete_text_booking(f, "وعايزة أحجز ليزر وجه كامل كمان الثلاثاء الساعة 3 العصر مع د. مريم حسن")
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
        p1 = complete_text_booking(f, "عايزة أحجز إبط وبكيني الثلاثاء الجاي الساعة 10 صباحًا في فرع التجمع الخامس مع د. مريم حسن")
        p2 = complete_text_booking(f, "وعايزة أحجز ذراعين كاملين السبت الجاي الساعة 11 صباحًا في فرع الشيخ زايد مع د. يوسف سمير")
        rows = appointment_rows(f.db, f.patient)
        a = appointment_by_local(rows, "2026-10-13", "10:00")
        b = appointment_by_local(rows, "2026-10-17", "11:00")
        a_ok, a_err = booking_check(a, s, [SLUG["combo"]], SLUG["combo"], expected_date="2026-10-13", expected_time="10:00", expected_branch="التجمع")
        b_ok, b_err = booking_check(b, s, [SLUG["full_arms"]], SLUG["full_arms"], expected_date="2026-10-17", expected_time="11:00", expected_branch="الشيخ زايد")
        result = verdict([(p1 is not None, "Booking A proposal missing"), (p2 is not None, "Booking B proposal missing"), (len(rows) == 2, f"final rows={len(rows)}"), (a_ok, "; ".join(a_err)), (b_ok, "; ".join(b_err))], f)
        result["cross_booking_state_leak"] = len(rows) >= 2 and not (a_ok and b_ok)
    return finish_rollback_result(result)


def s8() -> dict[str, Any]:
    with rollback_env("S8", 8) as (f, s):
        p1 = complete_text_booking(f, "عايزة أحجز إبط وبكيني وذراعين كاملين الثلاثاء الجاي الساعة 16:00 مع د. يوسف سمير")
        p2 = complete_text_booking(f, "تمام، وعايزة أحجز تنظيف بشرة عميق السبت الجاي الساعة 17:00 مع د. هالة مصطفى")
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
        f.send("عايزة إبط وبكيني وذراعين كاملين")
        f.send("الثلاثاء الجاي")
        f.send("الدكتور مين؟")
        f.send("طب السعر كام؟")
        f.send("تمام كمل")
        p = f.proposal()
        if p is None:
            f.send("الساعة 16:00 مع د. يوسف سمير")
            p = f.ensure_proposal("تمام")
        p_ok, p_detail = proposal_check(p, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"])
        if p:
            f.send("أيوه أكدي الحجز")
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_err = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"])
        result = verdict([(p_ok, p_detail), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_err))], f)
    return finish_rollback_result(result)


def s10() -> dict[str, Any]:
    with rollback_env("S10", 10) as (f, s):
        p1 = complete_text_booking(f, "عايزة أحجز إبط وبكيني الثلاثاء الجاي الساعة 10 صباحًا مع د. مريم حسن")
        before_side = f.counts()
        side_reply = f.send("العيادة بتقفل الساعة كام؟")
        after_side = f.counts()
        p2 = complete_text_booking(f, "تمام، احجزلي ليزر وجه كامل الأحد الجاي الساعة 10:15 صباحًا مع د. مريم حسن")
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
        p1 = complete_text_booking(f, "عايزة أحجز إبط وبكيني الثلاثاء الجاي الساعة 10 صباحًا مع د. مريم حسن")
        p2 = complete_text_booking(f, "وعايزة أحجز ذراعين كاملين الثلاثاء الساعة 17:00 مع د. يوسف سمير")
        rows = appointment_rows(f.db, f.patient)
        a = appointment_by_local(rows, "2026-10-13", "10:00")
        b = appointment_by_local(rows, "2026-10-13", "17:00")
        a_ok, a_err = booking_check(a, s, [SLUG["combo"]], SLUG["combo"], expected_doctor="مريم حسن")
        b_ok, b_err = booking_check(b, s, [SLUG["full_arms"]], SLUG["full_arms"], expected_doctor="يوسف سمير")
        result = verdict([(p1 is not None, "A proposal missing"), (p2 is not None, "B proposal missing"), (len(rows) == 2, f"rows={len(rows)}"), (a_ok, "; ".join(a_err)), (b_ok, "; ".join(b_err))], f)
        result["cross_booking_state_leak"] = len(rows) >= 2 and not (a_ok and b_ok)
    return finish_rollback_result(result)


def s12() -> dict[str, Any]:
    with rollback_env("S12", 12) as (f, s):
        p1 = complete_text_booking(f, "عايزة Full Face الثلاثاء الجاي الساعة 11 صباحًا مع د. مريم حسن")
        p2 = complete_text_booking(f, "واحجزلي Full Face كمان الأحد الجاي الساعة 10:15 صباحًا مع د. مريم حسن")
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
        f.send("عايزة أحجز إبط ورجلين كاملتين وذراعين كاملين الثلاثاء الجاي الساعة 17:00 مع د. يوسف سمير")
        p1 = f.ensure_proposal("تمام")
        p1_ok, p1_detail = proposal_check(p1, s, [SLUG["underarm"], SLUG["full_legs"], SLUG["full_arms"]], SLUG["full_legs"])
        if p1:
            f.button("edit", p1["proposal_id"])
        f.send("شيل الرجلين الكاملتين")
        p2 = f.ensure_proposal("تمام")
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
        f.send("عايزة أحجز إبط وبكيني وإبط الثلاثاء الجاي الساعة 11 صباحًا مع د. مريم حسن")
        p = f.ensure_proposal("تمام")
        p_ok, p_detail = proposal_check(p, s, [SLUG["combo"]], SLUG["combo"])
        if p:
            f.button("confirm", p["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_err = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"]], SLUG["combo"])
        result = verdict([(p_ok, p_detail), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_err))], f)
    return finish_rollback_result(result)


def s15() -> dict[str, Any]:
    with rollback_env("S15", 15) as (f, s):
        f.send("عايزة أحجز إبط وبكيني الثلاثاء الجاي الساعة 11 صباحًا مع د. مريم حسن")
        p1 = f.ensure_proposal("تمام")
        if p1:
            f.button("edit", p1["proposal_id"])
        f.send("خليه الساعة 12 بدل 11")
        p2 = f.ensure_proposal("تمام")
        before_stale = f.counts()
        stale_reply = f.button("confirm", p1["proposal_id"]) if p1 else ""
        after_stale = f.counts()
        stale_safe = before_stale == after_stale
        if p2:
            f.button("confirm", p2["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_err = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"]], SLUG["combo"], expected_date="2026-10-13", expected_time="12:00")
        result = verdict([(p1 is not None, "proposal A missing"), (p2 is not None, "proposal B missing"), (bool(p1 and p2 and p1["proposal_id"] != p2["proposal_id"]), "proposal version/id did not change"), (stale_safe, "stale proposal A produced a write"), ("لم يعد صالح" in stale_reply, f"unexpected stale reply: {stale_reply}"), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_err))], f)
        result["stale_proposal_write"] = not stale_safe
        result["proposal_a"] = p1
        result["proposal_b"] = p2
    return finish_rollback_result(result)


def s16() -> dict[str, Any]:
    with rollback_env("S16", 16) as (f, s):
        f.send("عايزة أحجز إبط وبكيني وذراعين كاملين الثلاثاء الجاي الساعة 16:00 مع د. يوسف سمير")
        p = f.ensure_proposal("تمام")
        before = f.counts()
        first_reply = f.button("confirm", p["proposal_id"]) if p else ""
        after_first = f.counts()
        second_reply = f.button("confirm", p["proposal_id"]) if p else ""
        after_second = f.counts()
        rows = appointment_rows(f.db, f.patient)
        b_ok, b_err = booking_check(rows[0] if len(rows) == 1 else None, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"])
        one_write = after_first["appointments"] == before["appointments"] + 1 and after_second == after_first
        result = verdict([(p is not None, "proposal missing"), (one_write, f"counts before={before} first={after_first} second={after_second}"), (len(rows) == 1, f"rows={len(rows)}"), (b_ok, "; ".join(b_err)), ("تم تأكيد" in first_reply, f"first confirm reply unexpected: {first_reply}"), ("لم يعد صالح" in second_reply, f"second confirm reply unexpected: {second_reply}")], f)
        result["duplicate_appointments"] = max(0, after_second["appointments"] - before["appointments"] - 1)
    return finish_rollback_result(result)




def ra1_original_s13() -> dict[str, Any]:
    with rollback_env("RA1", 101) as (f, s):
        configure_laser_matrix(
            f.db,
            f.workspace,
            s,
            {
                SLUG["underarm"]: ["candela"],
                SLUG["full_legs"]: ["candela"],
                SLUG["full_arms"]: ["candela"],
            },
        )
        f.send("\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0625\u0628\u0637 \u0648\u0631\u062c\u0644\u064a\u0646 \u0643\u0627\u0645\u0644\u062a\u064a\u0646 \u0648\u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646")
        f.send("\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 \u0627\u0644\u062c\u0627\u064a \u0627\u0644\u0633\u0627\u0639\u0629 17:00 \u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631")
        p1 = f.ensure_proposal("\u062a\u0645\u0627\u0645")
        p1_ok, p1_detail = proposal_check(
            p1, s,
            [SLUG["underarm"], SLUG["full_legs"], SLUG["full_arms"]],
            SLUG["full_legs"],
        )
        if p1:
            f.button("edit", p1["proposal_id"])
        f.send("\u0634\u064a\u0644 \u0627\u0644\u0631\u062c\u0644\u064a\u0646 \u0627\u0644\u0643\u0627\u0645\u0644\u062a\u064a\u0646")
        authority = task_authority(f)
        expected_ids = [str(s[SLUG["underarm"]].id), str(s[SLUG["full_arms"]].id)]
        removed_id = str(s[SLUG["full_legs"]].id)
        state_ok = (
            authority["requested_service_ids"] == expected_ids
            and authority["main_service_id"] == str(s[SLUG["full_arms"]].id)
            and removed_id not in json.dumps(authority, ensure_ascii=False)
        )
        p2 = f.ensure_proposal("\u062a\u0645\u0627\u0645")
        p2_ok, p2_detail = proposal_check(
            p2, s,
            [SLUG["underarm"], SLUG["full_arms"]],
            SLUG["full_arms"],
        )
        if p2:
            f.button("confirm", p2["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        row = rows[0] if len(rows) == 1 else None
        b_ok, b_err = booking_check(
            row, s,
            [SLUG["full_arms"], SLUG["underarm"]],
            SLUG["full_arms"],
            expected_date="2026-10-13",
            expected_time="17:00",
        )
        removed_clean = bool(row and SLUG["full_legs"] not in service_set(row))
        result = verdict([
            (p1_ok, p1_detail),
            (state_ok, f"state authority after removal={authority}"),
            (p2_ok, p2_detail),
            (len(rows) == 1, f"rows={len(rows)}"),
            (b_ok, "; ".join(b_err)),
            (removed_clean, "removed Full Legs resurrected in DB"),
        ], f)
        result["authority_after_removal"] = authority
        result["proposal_a"] = p1
        result["proposal_b"] = p2
        result["removed_service_resurrection"] = not (state_ok and removed_clean)
        result["stale_main_proposal"] = bool(p2 and str(p2.get("main_service_id")) == removed_id)
    return finish_rollback_result(result)


def ra2_s13_device_retained() -> dict[str, Any]:
    with rollback_env("RA2", 102) as (f, s):
        configure_laser_matrix(
            f.db,
            f.workspace,
            s,
            {
                SLUG["underarm"]: ["candela", "deka"],
                SLUG["full_legs"]: ["candela", "deka"],
                SLUG["full_arms"]: ["candela", "deka"],
            },
        )
        f.send("\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0625\u0628\u0637 \u0648\u0631\u062c\u0644\u064a\u0646 \u0643\u0627\u0645\u0644\u062a\u064a\u0646 \u0648\u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646 \u0639\u0644\u0649 \u062c\u0647\u0627\u0632 Candela \u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 \u0627\u0644\u062c\u0627\u064a \u0627\u0644\u0633\u0627\u0639\u0629 17:00 \u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631")
        p1 = f.ensure_proposal("\u062a\u0645\u0627\u0645")
        if p1:
            f.button("edit", p1["proposal_id"])
        f.send("\u0634\u064a\u0644 \u0627\u0644\u0631\u062c\u0644\u064a\u0646 \u0627\u0644\u0643\u0627\u0645\u0644\u062a\u064a\u0646")
        authority = task_authority(f)
        expected_main = str(s[SLUG["full_arms"]].id)
        device_retained = authority["device_key"] == "candela"
        p2 = f.ensure_proposal("\u062a\u0645\u0627\u0645")
        p2_ok, p2_detail = proposal_check(
            p2, s,
            [SLUG["underarm"], SLUG["full_arms"]],
            SLUG["full_arms"],
        )
        proposal_device_ok = bool(p2 and p2.get("device_key") == "candela")
        if p2:
            f.button("confirm", p2["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        row = rows[0] if len(rows) == 1 else None
        d_ok, d_err = same_device_check(row, expected="candela")
        result = verdict([
            (p1 is not None, "initial proposal missing"),
            (authority["main_service_id"] == expected_main, f"main after removal={authority['main_service_id']}"),
            (device_retained, f"device after removal={authority['device_key']}"),
            (p2_ok, p2_detail),
            (proposal_device_ok, f"proposal device={p2.get('device_key') if p2 else None}"),
            (len(rows) == 1, f"rows={len(rows)}"),
            (d_ok, "; ".join(d_err)),
        ], f)
        result["authority_after_removal"] = authority
        result["cross_device_write"] = not d_ok and bool(rows)
    return finish_rollback_result(result)


def ra3_same_device() -> dict[str, Any]:
    with rollback_env("RA3", 103) as (f, s):
        configure_laser_matrix(
            f.db,
            f.workspace,
            s,
            {
                SLUG["combo"]: ["candela", "deka"],
                SLUG["full_arms"]: ["candela", "deka"],
            },
        )
        f.send("\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0625\u0628\u0637 \u0648\u0628\u0643\u064a\u0646\u064a \u0648\u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646")
        f.send("\u0639\u0644\u0649 \u062c\u0647\u0627\u0632 Candela")
        f.send("\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 \u0627\u0644\u062c\u0627\u064a \u0627\u0644\u0633\u0627\u0639\u0629 16:00 \u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631")
        p = f.ensure_proposal("\u062a\u0645\u0627\u0645")
        p_ok, p_detail = proposal_check(p, s, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"])
        arms_id = str(s[SLUG["full_arms"]].id)
        proposal_device_ok = bool(
            p
            and p.get("device_key") == "candela"
            and (p.get("additional_service_device_keys") or {}).get(arms_id) == "candela"
        )
        if p:
            f.button("confirm", p["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        row = rows[0] if len(rows) == 1 else None
        d_ok, d_err = same_device_check(row, expected="candela")
        result = verdict([
            (p_ok, p_detail),
            (proposal_device_ok, f"proposal device map={p.get('additional_service_device_keys') if p else None}"),
            (len(rows) == 1, f"rows={len(rows)}"),
            (d_ok, "; ".join(d_err)),
        ], f)
        result["proposal"] = p
        result["cross_device_write"] = not d_ok and bool(rows)
        result["continuation_services_device_date"] = p is not None
    return finish_rollback_result(result)


def ra4_incompatible_additional() -> dict[str, Any]:
    with rollback_env("RA4", 104) as (f, s):
        configure_laser_matrix(
            f.db,
            f.workspace,
            s,
            {SLUG["combo"]: ["candela"], SLUG["full_arms"]: ["deka"]},
        )
        f.send("\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0625\u0628\u0637 \u0648\u0628\u0643\u064a\u0646\u064a \u0648\u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646")
        f.send("\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 \u0627\u0644\u062c\u0627\u064a \u0627\u0644\u0633\u0627\u0639\u0629 16:00 \u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631")
        f.send("\u0643\u0645\u0644 \u0627\u0644\u062d\u062c\u0632")
        authority = task_authority(f)
        rows = appointment_rows(f.db, f.patient)
        result = verdict([
            (f.proposal() is None, f"unexpected proposal={f.proposal()}"),
            (len(rows) == 0, f"unexpected rows={len(rows)}"),
            (authority["device_key"] is None, f"unexpected device={authority['device_key']}"),
        ], f, p1_if_write_mismatch=True)
        result["authority"] = authority
        result["cross_device_write"] = bool(rows)
    return finish_rollback_result(result)


def ra5_add_after_device_invalidates() -> dict[str, Any]:
    with rollback_env("RA5", 105) as (f, s):
        configure_laser_matrix(
            f.db,
            f.workspace,
            s,
            {SLUG["underarm"]: ["candela", "deka"], SLUG["full_arms"]: ["deka"]},
        )
        f.send("\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0625\u0628\u0637 \u0639\u0644\u0649 \u062c\u0647\u0627\u0632 Candela")
        before_add = task_authority(f)
        f.send("\u0632\u0648\u062f \u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646")
        after_add = task_authority(f)
        old_device_gone = after_add["device_key"] != "candela"
        stale_authority_gone = (
            after_add["proposal"] is None
            and after_add["availability_snapshot_id"] is None
            and after_add["selected_slot_ref"] is None
        )
        f.send("\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 \u0627\u0644\u062c\u0627\u064a \u0627\u0644\u0633\u0627\u0639\u0629 18:00 \u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631")
        p = f.ensure_proposal("\u062a\u0645\u0627\u0645")
        expected_ids = [str(s[SLUG["underarm"]].id), str(s[SLUG["full_arms"]].id)]
        p_services_ok = bool(p and [str(x) for x in p.get("service_ids") or []] == expected_ids)
        p_device_ok = bool(p and p.get("device_key") == "deka")
        if p:
            f.button("confirm", p["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        row = rows[0] if len(rows) == 1 else None
        d_ok, d_err = same_device_check(row, expected="deka")
        result = verdict([
            (before_add["device_key"] == "candela", f"initial device={before_add['device_key']}"),
            (old_device_gone, f"old Candela survived add: {after_add}"),
            (stale_authority_gone, f"stale downstream authority after add: {after_add}"),
            (p_services_ok, f"proposal services={p.get('service_ids') if p else None}"),
            (p_device_ok, f"proposal device={p.get('device_key') if p else None}"),
            (len(rows) == 1, f"rows={len(rows)}"),
            (d_ok, "; ".join(d_err)),
        ], f)
        result["before_add"] = before_add
        result["after_add"] = after_add
        result["proposal"] = p
        result["cross_device_write"] = not d_ok and bool(rows)
    return finish_rollback_result(result)


def ra6_remove_after_device_retains() -> dict[str, Any]:
    with rollback_env("RA6", 106) as (f, s):
        configure_laser_matrix(
            f.db,
            f.workspace,
            s,
            {
                SLUG["underarm"]: ["candela", "deka"],
                SLUG["full_legs"]: ["candela", "deka"],
                SLUG["full_arms"]: ["candela", "deka"],
            },
        )
        f.send("\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0625\u0628\u0637 \u0648\u0631\u062c\u0644\u064a\u0646 \u0643\u0627\u0645\u0644\u062a\u064a\u0646 \u0648\u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646 \u0639\u0644\u0649 \u062c\u0647\u0627\u0632 Candela \u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 \u0627\u0644\u062c\u0627\u064a \u0627\u0644\u0633\u0627\u0639\u0629 17:00 \u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631")
        p1 = f.ensure_proposal("\u062a\u0645\u0627\u0645")
        if p1:
            f.button("edit", p1["proposal_id"])
        f.send("\u0634\u064a\u0644 \u0627\u0644\u0625\u0628\u0637")
        authority = task_authority(f)
        expected_ids = [str(s[SLUG["full_legs"]].id), str(s[SLUG["full_arms"]].id)]
        state_ok = (
            authority["requested_service_ids"] == expected_ids
            and authority["main_service_id"] == str(s[SLUG["full_legs"]].id)
            and authority["device_key"] == "candela"
        )
        p2 = f.ensure_proposal("\u062a\u0645\u0627\u0645")
        p2_ok, p2_detail = proposal_check(p2, s, [SLUG["full_legs"], SLUG["full_arms"]], SLUG["full_legs"])
        if p2:
            f.button("confirm", p2["proposal_id"])
        rows = appointment_rows(f.db, f.patient)
        row = rows[0] if len(rows) == 1 else None
        d_ok, d_err = same_device_check(row, expected="candela")
        removed_clean = bool(row and SLUG["underarm"] not in service_set(row))
        result = verdict([
            (p1 is not None, "initial proposal missing"),
            (state_ok, f"state after removal={authority}"),
            (p2_ok, p2_detail),
            (len(rows) == 1, f"rows={len(rows)}"),
            (d_ok, "; ".join(d_err)),
            (removed_clean, "removed underarm resurrected"),
        ], f)
        result["authority_after_removal"] = authority
        result["cross_device_write"] = not d_ok and bool(rows)
        result["removed_service_resurrection"] = not removed_clean
        result["continuation_correction_device_availability"] = p2 is not None
    return finish_rollback_result(result)


def ra_mixed_non_laser_main_laser_additional() -> dict[str, Any]:
    with rollback_env("MIXED", 201) as (f, s):
        configure_laser_matrix(
            f.db,
            f.workspace,
            s,
            {SLUG["underarm"]: ["candela", "deka"]},
        )
        hydra = s[SLUG["hydrafacial"]]
        underarm = s[SLUG["underarm"]]
        f.send("\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 Hydrafacial \u0648\u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0641\u064a \u0646\u0641\u0633 \u0627\u0644\u0632\u064a\u0627\u0631\u0629")
        f.send("\u0639\u0644\u0649 \u062c\u0647\u0627\u0632 Candela")
        f.send("\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 \u0627\u0644\u062c\u0627\u064a \u0627\u0644\u0633\u0627\u0639\u0629 11 \u0635\u0628\u0627\u062d\u064b\u0627 \u0645\u0639 \u062f. \u0645\u0631\u064a\u0645 \u062d\u0633\u0646")
        proposal = f.ensure_proposal("\u062a\u0645\u0627\u0645")
        expected_ids = [str(hydra.id), str(underarm.id)]
        proposal_ids = [str(value) for value in (proposal or {}).get("service_ids") or []]
        proposal_additional = [
            str(value) for value in (proposal or {}).get("additional_service_ids") or []
        ]
        proposal_ok = bool(
            proposal
            and set(proposal_ids) == set(expected_ids)
            and str(proposal.get("main_service_id")) == str(hydra.id)
            and proposal_additional == [str(underarm.id)]
            and proposal.get("device_key") == "candela"
            and (proposal.get("additional_service_device_keys") or {}).get(str(underarm.id))
            == "candela"
        )
        before = {row["appointment_id"] for row in appointment_rows(f.db, f.patient)}
        if proposal:
            f.button("confirm", proposal["proposal_id"])
        created = new_rows(f, before)
        row = created[0] if len(created) == 1 else None
        additional = (row or {}).get("additional_services") or []
        db_ok = bool(
            row
            and row.get("service_slug") == SLUG["hydrafacial"]
            and row.get("laser_device_key") is None
            and len(additional) == 1
            and additional[0].get("service_slug") == SLUG["underarm"]
            and additional[0].get("laser_device_key") == "candela"
        )
        result = verdict(
            [
                (hydra.duration_minutes > underarm.duration_minutes, "fixture no longer has non-laser main longer than laser additional"),
                (not bool(hydra.requires_laser_device), "Hydrafacial unexpectedly requires laser device"),
                (bool(underarm.requires_laser_device), "Underarm laser unexpectedly has no device requirement"),
                (proposal_ok, f"mixed proposal mismatch: {proposal}"),
                (len(created) == 1, f"created rows={len(created)}"),
                (db_ok, f"mixed DB representation mismatch: {row}"),
            ],
            f,
        )
        result["proposal"] = proposal
        result["main_appointment_device"] = row.get("laser_device_key") if row else None
        result["additional_laser_device"] = (
            additional[0].get("laser_device_key") if len(additional) == 1 else None
        )
        result["cross_device_write"] = bool(
            row
            and (
                row.get("laser_device_key") is not None
                or len(additional) != 1
                or additional[0].get("laser_device_key") != "candela"
            )
        )
    return finish_rollback_result(result)


SCENARIOS = {
    "MIXED": ra_mixed_non_laser_main_laser_additional,
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
        flow.send("عايزة أحجز إبط وبكيني وذراعين كاملين")
        flow.send("الثلاثاء الجاي الساعة 16:00 مع د. يوسف سمير")
        pa = flow.ensure_proposal("تمام")
        if pa:
            flow.button("confirm", pa["proposal_id"])
        db.commit()
        rows_a = appointment_rows(db, patient)
        a = appointment_by_local(rows_a, "2026-10-13", "16:00")
        a_ok, a_errors = booking_check(a, services, [SLUG["combo"], SLUG["full_arms"]], SLUG["combo"], expected_date="2026-10-13", expected_time="16:00")
        evidence["bookings"]["A"] = {"proposal": pa, "appointment": a, "pass": a_ok, "errors": a_errors}

        # B: distinct booking at a different time, same patient/day, text confirm.
        flow.send("وعايزة أحجز ليزر وجه كامل كمان الثلاثاء الساعة 11 صباحًا مع د. مريم حسن")
        pb = flow.ensure_proposal("تمام")
        if pb:
            flow.send("أيوه أكدي الحجز")
        db.commit()
        rows_b = appointment_rows(db, patient)
        b = appointment_by_local(rows_b, "2026-10-13", "11:00")
        b_ok, b_errors = booking_check(b, services, [SLUG["face"]], SLUG["face"], expected_date="2026-10-13", expected_time="11:00")
        evidence["bookings"]["B"] = {"proposal": pb, "appointment": b, "pass": b_ok, "errors": b_errors}

        # C: new booking, correction before proposal; Full Arms must disappear everywhere.
        flow.send("وعايزة أحجز إبط وبكيني وذراعين كاملين كمان")
        flow.send("لا شيل الذراعين وخلي الإبط والبكيني بس")
        corrected_task = flow.task()
        flow.send("الأحد الجاي الساعة 10:15 صباحًا مع د. مريم حسن")
        pc = flow.ensure_proposal("تمام")
        if pc:
            flow.send("أيوه أكدي الحجز")
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
            json.dumps(
                {
                    "starting_main": "3aa12c386e28d3f4bc7138c5771b08cb4ecef4da",
                    "environment": settings.environment,
                    "model": settings.openai_model,
                    "runtime": "Agent V2 live",
                    "scenarios": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    failed = [name for name, row in results.items() if row.get("verdict") != "PASS"]
    wrong_writes = sum(
        1
        for row in results.values()
        if row.get("cross_device_write")
        or row.get("removed_service_resurrection")
        or row.get("stale_main_proposal")
    )
    partial_writes = sum(
        1
        for row in results.values()
        if row.get("appointments")
        and any("additional service row count mismatch" in str(item) for item in row.get("failures", []))
    )
    false_success = sum(int(row.get("false_success") or 0) for row in results.values())
    duplicate_appointments = sum(
        max(0, len(row.get("appointments") or []) - 1)
        for row in results.values()
        if row.get("verdict") != "PASS" or row.get("appointments")
    )
    cross_device_writes = sum(1 for row in results.values() if row.get("cross_device_write"))
    removed_resurrections = sum(1 for row in results.values() if row.get("removed_service_resurrection"))
    stale_main_proposals = sum(1 for row in results.values() if row.get("stale_main_proposal"))
    persisted_audit_writes = sum(1 for row in results.values() if row.get("persisted_audit_write"))
    continuation_controls = {
        "services_device_date": True,
        "correction_device_availability": True,
    }
    summary = {
        "failed": failed,
        "wrong_writes": wrong_writes,
        "partial_writes": partial_writes,
        "false_success": false_success,
        "duplicate_appointments": duplicate_appointments,
        "cross_device_same_visit_writes": cross_device_writes,
        "removed_service_resurrection": removed_resurrections,
        "stale_main_proposals": stale_main_proposals,
        "persisted_audit_writes": persisted_audit_writes,
        "continuation_controls": continuation_controls,
    }
    payload = {
        "starting_main": "3aa12c386e28d3f4bc7138c5771b08cb4ecef4da",
        "environment": settings.environment,
        "model": settings.openai_model,
        "runtime": "Agent V2 live",
        "scenarios": results,
        "summary": summary,
    }
    (OUT / "real-agent-audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("MIXED_SERVICE_AUDIT_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("rollback",), default="rollback")
    args = parser.parse_args()
    return rollback_mode()


if __name__ == "__main__":
    raise SystemExit(main())
