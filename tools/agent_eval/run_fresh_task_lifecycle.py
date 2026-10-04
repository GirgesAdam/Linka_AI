from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import Appointment
from app.models.booking_settings import BookingSettings
from app.models.clinic_inventory import ClinicLaserDevice, ServiceDevicePrice
from app.models.conversation import Conversation as ConversationRow
from app.models.conversation_flow_state import ConversationFlowState
from app.models.doctor import Doctor
from app.models.message import Message
from app.models.patient import Patient
from app.models.service import Service
from app.models.working_hours import DoctorAvailabilityWindow, DoctorWorkingHour
from app.models.workspace import Workspace
from app.services.agent_v2 import live_chat as live_chat_module
from app.services.agent_v2.live_chat import (
    _recent_automation_context,
    _recent_pending_choice_context,
    _recent_verified_action_context,
    _recent_verified_read_context,
)
from app.services.agent_v2.state_persistence import (
    V2StateConflictError,
    load_active_task,
)
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import (
    active_branch_id,
    assert_demo_only,
    send_turn,
    state_snapshot,
)
from tools.agent_eval.run_batch_03 import _seed_future_appointment

TZ = ZoneInfo("Africa/Cairo")
SESSION1 = datetime(2026, 10, 3, 10, 0, tzinfo=TZ)
SESSION2 = SESSION1 + timedelta(days=3)


def patient(db, ws, label):
    suffix = str(uuid4().int % 1_000_000_000).zfill(9)
    row = Patient(
        workspace_id=ws.id,
        first_name=label,
        last_name="FreshTask",
        phone=f"+209{suffix}",
        phone_normalized=f"+209{suffix}",
        preferred_language="ar",
        source="other",
        status="active",
        marketing_consent=False,
    )
    db.add(row)
    db.flush()
    return row


def service(db, ws, name):
    row = db.scalar(select(Service).where(Service.workspace_id == ws.id, Service.name == name))
    if row is None:
        raise RuntimeError(f"missing service {name}")
    return row


def doctor_from_catalog(db, ws, contains):
    catalog = build_clinic_catalog(db, ws)
    for raw in catalog.get("doctors", []):
        if isinstance(raw, dict) and raw.get("id") and contains in str(raw.get("name") or ""):
            row = db.get(Doctor, UUID(str(raw["id"])))
            if row is not None:
                return row
    raise RuntimeError(f"missing doctor {contains}")


def prepare(db, ws):
    # Use only the existing Demo catalog. Do not create doctor/service fixture rows:
    # concurrent evaluators can otherwise contend on unique assignment indexes.
    renames = {
        "ليزر إزالة الشعر - إبط": "Under Arm",
        "ليزر إزالة الشعر - جسم كامل سيدات": "Full Body",
    }
    for old, new in renames.items():
        row = db.scalar(select(Service).where(Service.workspace_id == ws.id, Service.name == old))
        if row is not None:
            row.name = new
    device = db.scalar(
        select(ClinicLaserDevice).where(
            ClinicLaserDevice.workspace_id == ws.id,
            ClinicLaserDevice.device_key == "prime_lase",
        )
    )
    if device is not None:
        device.name = "DEKA Again"
    for price in db.scalars(
        select(ServiceDevicePrice).where(
            ServiceDevicePrice.workspace_id == ws.id,
            ServiceDevicePrice.device_key == "prime_lase",
        )
    ):
        price.device_name = "DEKA Again"
    bs = db.scalar(select(BookingSettings).where(BookingSettings.workspace_id == ws.id))
    if bs is not None:
        bs.minimum_notice_minutes = 0
        bs.allow_same_day_booking = True
        bs.booking_horizon_days = max(bs.booking_horizon_days, 30)
    db.flush()
    under = service(db, ws, "Under Arm")
    full = service(db, ws, "Full Body")
    mary = doctor_from_catalog(db, ws, "مريم")
    youssef = doctor_from_catalog(db, ws, "يوسف")
    branch_id = UUID(active_branch_id(build_clinic_catalog(db, ws)))
    thursday = datetime(2026, 10, 8, tzinfo=TZ).date()
    weekday = thursday.weekday()
    mary_hours = db.scalar(
        select(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == ws.id,
            DoctorWorkingHour.doctor_id == mary.id,
            DoctorWorkingHour.branch_id == branch_id,
            DoctorWorkingHour.weekday == weekday,
        )
    )
    if mary_hours is None:
        raise RuntimeError("eval fixture is missing Mary's Thursday working hours")
    mary_hours.end_time = time(16, 30)
    youssef_hours = db.scalar(
        select(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == ws.id,
            DoctorWorkingHour.doctor_id == youssef.id,
            DoctorWorkingHour.branch_id == branch_id,
            DoctorWorkingHour.weekday == weekday,
        )
    )
    if youssef_hours is not None:
        youssef_hours.start_time = time(10, 0)
        youssef_hours.end_time = time(22, 0)
    start = datetime.combine(thursday, time.min, tzinfo=TZ).astimezone(UTC)
    end = start + timedelta(days=1)
    db.execute(
        delete(DoctorAvailabilityWindow).where(
            DoctorAvailabilityWindow.workspace_id == ws.id,
            DoctorAvailabilityWindow.doctor_id == mary.id,
            DoctorAvailabilityWindow.start_at < end,
            DoctorAvailabilityWindow.end_at > start,
        )
    )
    db.flush()
    return under, full, mary, youssef, branch_id


def seed_existing(db, ws, p, under, mary, branch_id):
    adapter = get_clinic_adapter(db=db, workspace=ws)
    day = datetime(2026, 10, 8, tzinfo=TZ).date()
    result = adapter.get_availability(
        AvailabilityRequest(
            branch_id=str(branch_id),
            service_id=str(under.id),
            booking_date=day,
            doctor_id=str(mary.id),
            laser_device_key="prime_lase",
            now=SESSION1,
        )
    )
    if not result.slots:
        raise RuntimeError("eval fixture has no Mary/DEKA Under Arm slot on 2026-10-08")
    return _seed_future_appointment(
        db,
        ws,
        p,
        service=under,
        doctor_id=mary.id,
        slot=result.slots[0],
        device_key="prime_lase",
    )


def response_goals(cap):
    goals = []
    for trace in cap.structured_trace:
        if not isinstance(trace, dict):
            continue
        for outcome in trace.get("outcomes") or []:
            if isinstance(outcome, dict) and outcome.get("response_goal"):
                goals.append(outcome["response_goal"])
    return goals


def operation_relationship(cap):
    values = []
    for trace in cap.structured_trace:
        if not isinstance(trace, dict):
            continue
        understanding = trace.get("understanding") or {}
        for op in understanding.get("operations") or []:
            if isinstance(op, dict):
                values.append(
                    {
                        "type": op.get("type"),
                        "active_task_relationship": op.get("active_task_relationship"),
                        "automation_context_relationship": op.get("automation_context_relationship"),
                        "fresh_task": op.get("fresh_task"),
                        "fresh_task_explicit_fields": op.get("fresh_task_explicit_fields"),
                        "continues_previous": op.get("continues_previous"),
                    }
                )
    return values


class Conversation:
    def __init__(self, name, db, ws, p, now):
        self.name = name
        self.db = db
        self.ws = ws
        self.p = p
        self.now = now
        self.cid = None
        self.turns = []
        self.errors = []

    def set_now(self, now):
        self.now = now

    def send(self, text):
        live_chat_module._workspace_clock = lambda workspace: (
            workspace.timezone or "Africa/Cairo",
            self.now,
        )
        try:
            response, cap = send_turn(
                self.db,
                self.ws,
                self.p,
                self.name,
                len(self.turns) + 1,
                text,
                self.cid,
            )
            self.cid = response.conversation_id
            persisted = load_active_task(
                self.db,
                workspace_id=self.ws.id,
                conversation_id=self.cid,
                patient_id=self.p.id,
            )
            self.turns.append(
                {
                    "customer": text,
                    "linka": cap.agent_response,
                    "goals": response_goals(cap),
                    "operations": operation_relationship(cap),
                    "verified_reads": list(cap.verified_reads),
                    "actions": list(cap.actions),
                    "write_attempted": cap.write_attempted,
                    "write_result": cap.write_result,
                    "active_task": (
                        persisted.active_task.model_dump(mode="json")
                        if persisted is not None
                        else None
                    ),
                }
            )
            return cap
        except V2StateConflictError as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            self.turns.append(
                {
                    "customer": text,
                    "linka": None,
                    "goals": [],
                    "operations": [],
                    "write_attempted": False,
                    "write_result": None,
                    "active_task": None,
                    "error": self.errors[-1],
                }
            )
            return None


def run_t1(db, ws, under, full, mary, branch_id):
    p = patient(db, ws, "T1")
    old = seed_existing(db, ws, p, under, mary, branch_id)
    before = state_snapshot(db, ws, p)
    c = Conversation("T1", db, ws, p, SESSION1)
    c.send("ميعادي الجاي امتى؟")
    c.send("عايز اغيره")
    c.send("خليه الخميس")
    c.send("بعد 5")
    pre_gap_task = c.turns[-1]["active_task"]
    c.set_now(SESSION2)
    fresh = c.send("عايز أبدأ حجز جديد منفصل")
    replacement_turn = c.turns[-1]
    fresh_goal_task = replacement_turn["active_task"]
    if fresh is not None:
        c.send("Full Body")
        fresh_service_task = c.turns[-1]["active_task"]
        for message in ("الخميس", "كانديلا", "الساعة 7", "يوسف", "احجز"):
            if c.turns and "booking_completed" in c.turns[-1]["goals"]:
                break
            c.send(message)
    else:
        fresh_service_task = None
    after = state_snapshot(db, ws, p)
    old_after = next(
        (a for a in after.get("appointments", []) if a.get("id") == str(old.id)),
        None,
    )
    new_rows = [
        a
        for a in after.get("appointments", [])
        if a.get("id") != str(old.id)
    ]
    return {
        "id": "T1",
        "patient_id": str(p.id),
        "conversation_id": str(c.cid) if c.cid else None,
        "seeded_old_appointment_id": str(old.id),
        "turns": c.turns,
        "errors": c.errors,
        "pre_gap_task": pre_gap_task,
        "replacement_turn": replacement_turn,
        "fresh_goal_task": fresh_goal_task,
        "fresh_service_task": fresh_service_task,
        "old_appointment_after": old_after,
        "new_appointments": new_rows,
        "before": before,
        "after": after,
        "writes": sum(1 for t in c.turns if t.get("write_attempted")),
    }


def run_t2(db, ws, under, mary, branch_id):
    p = patient(db, ws, "T2")
    old = seed_existing(db, ws, p, under, mary, branch_id)
    c = Conversation("T2", db, ws, p, SESSION1)
    for message in (
        "ميعادي الجاي امتى؟",
        "عايز اغيره",
        "خليه الخميس",
        "بعد 5",
        "طب قبل 5؟",
    ):
        c.send(message)
    task = c.turns[-1]["active_task"]
    return {
        "id": "T2",
        "target_id": str(old.id),
        "turns": c.turns,
        "errors": c.errors,
        "final_task": task,
    }


def run_t3(db, ws):
    p = patient(db, ws, "T3")
    c = Conversation("T3", db, ws, p, SESSION1)
    for message in (
        "عايز احجز Under Arm",
        "الخميس",
        "لا خليها يوم التلات بدل الخميس",
    ):
        c.send(message)
    return {
        "id": "T3",
        "turns": c.turns,
        "errors": c.errors,
        "final_task": c.turns[-1]["active_task"],
    }


def _latest_verified_action_context(db, conversation_id):
    previous = db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "outbound",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    metadata = dict(previous.metadata_json or {}) if previous is not None else {}
    value = metadata.get("v2_action_context")
    return dict(value) if isinstance(value, dict) else None


def run_t4(db, ws):
    p = patient(db, ws, "T4")
    c = Conversation("T4", db, ws, p, SESSION1)
    c.send("عايز احجز Under Arm")
    c.send("الخميس")
    old_task = c.turns[-1]["active_task"]
    c.send("عايز أبدأ حجز جديد منفصل")
    after_replace = c.turns[-1]["active_task"]
    c.send("Full Body")
    return {
        "id": "T4",
        "turns": c.turns,
        "errors": c.errors,
        "old_task": old_task,
        "replace_turn": c.turns[-2],
        "after_replace": after_replace,
        "fresh_task": c.turns[-1]["active_task"],
    }


def run_t5_completed_then_fresh(db, ws):
    p = patient(db, ws, "T5")
    before = state_snapshot(db, ws, p)
    c = Conversation("T5", db, ws, p, SESSION1)

    c.send("عايز احجز Under Arm")
    completed_booking_authorization_source = c.turns[-1]["active_task"][
        "write_authorization"
    ]["source_turn_id"]
    for message in ("الخميس", "ديكا", "مريم", "الساعة 4"):
        if c.turns and "booking_completed" in c.turns[-1]["goals"]:
            break
        c.send(message)

    completed_state = state_snapshot(db, ws, p)
    completed_rows = completed_state.get("appointments", [])
    completed_action = _latest_verified_action_context(db, c.cid)

    c.set_now(SESSION1 + timedelta(hours=2))
    c.send("عايز احجز جلسة جديدة")
    new_goal_task = c.turns[-1]["active_task"]
    c.send("Full Body")
    after_service_task = c.turns[-1]["active_task"]
    after_service_state = state_snapshot(db, ws, p)

    for message in ("الخميس", "كانديلا", "احجز الساعة 5"):
        if c.turns and "booking_completed" in c.turns[-1]["goals"]:
            break
        c.send(message)

    final_state = state_snapshot(db, ws, p)
    return {
        "id": "T5",
        "patient_id": str(p.id),
        "conversation_id": str(c.cid) if c.cid else None,
        "turns": c.turns,
        "errors": c.errors,
        "before": before,
        "completed_state": completed_state,
        "completed_appointments": completed_rows,
        "completed_action_context": completed_action,
        "completed_booking_authorization_source": completed_booking_authorization_source,
        "new_goal_task": new_goal_task,
        "after_service_task": after_service_task,
        "after_service_state": after_service_state,
        "final_state": final_state,
        "writes": sum(1 for turn in c.turns if turn.get("write_attempted")),
    }


def _turn(result, customer_text):
    return next(turn for turn in result["turns"] if turn["customer"] == customer_text)


def assert_after_regressions(results):
    by_id = {result["id"]: result for result in results}

    t1 = by_id["T1"]
    assert t1["errors"] == []
    assert t1["pre_gap_task"]["task_type"] == "reschedule"
    assert t1["pre_gap_task"]["replacement"]["time"]["mode"] == "after"
    assert t1["pre_gap_task"]["replacement"]["time"]["start_time"] == "17:00"
    assert t1["old_appointment_after"]["status"] == "confirmed"
    assert len(t1["new_appointments"]) == 1
    assert t1["writes"] == 1
    replacement_operation = t1["replacement_turn"]["operations"][0]
    assert replacement_operation["type"] == "book"
    assert replacement_operation["active_task_relationship"] == "replace"
    assert replacement_operation["fresh_task"] is True
    assert replacement_operation["fresh_task_explicit_fields"] == []
    assert "Under Arm" not in (t1["replacement_turn"]["linka"] or "")
    fresh = t1["fresh_goal_task"]
    assert fresh["task_type"] == "booking"
    assert fresh["constraints"]["service_id"] is None
    assert fresh["constraints"]["doctor_id"] is None
    assert fresh["constraints"]["device_key"] is None
    assert fresh["constraints"]["date"] is None
    assert fresh["constraints"]["time"] is None
    assert fresh["option_snapshot"] is None
    assert fresh["derived"]["selected_slot_ref"] is None
    assert "target" not in fresh
    assert (
        fresh["write_authorization"]["source_turn_id"]
        != t1["pre_gap_task"]["write_authorization"]["source_turn_id"]
    )
    fresh_service = t1["fresh_service_task"]
    assert fresh_service["task_type"] == "booking"
    assert fresh_service["constraints"]["service_id"] != t1["pre_gap_task"]["replacement"]["service_id"]
    assert fresh_service["constraints"]["doctor_id"] is None
    assert fresh_service["constraints"]["device_key"] is None
    assert fresh_service["constraints"]["date"] is None
    assert fresh_service["constraints"]["time"] is None
    assert "booking_completed" in t1["turns"][-1]["goals"]

    t2 = by_id["T2"]
    assert t2["errors"] == []
    assert t2["final_task"]["task_type"] == "reschedule"
    assert t2["final_task"]["target"]["appointment_id"] == t2["target_id"]
    assert t2["final_task"]["replacement"]["time"]["mode"] == "before"
    assert t2["final_task"]["replacement"]["time"]["start_time"] == "17:00"

    t3 = by_id["T3"]
    assert t3["errors"] == []
    first_booking = _turn(t3, "عايز احجز Under Arm")["active_task"]
    thursday_booking = _turn(t3, "الخميس")["active_task"]
    corrected_booking = t3["final_task"]
    assert corrected_booking["task_type"] == "booking"
    assert (
        corrected_booking["write_authorization"]["source_turn_id"]
        == first_booking["write_authorization"]["source_turn_id"]
    )
    assert corrected_booking["constraints"]["date"] != thursday_booking["constraints"]["date"]

    t4 = by_id["T4"]
    assert t4["errors"] == []
    assert t4["old_task"]["task_type"] == "booking"
    replacement = t4["replace_turn"]
    replacement_operation = replacement["operations"][0]
    assert replacement_operation["type"] == "book"
    assert replacement_operation["active_task_relationship"] == "replace"
    assert replacement_operation["fresh_task"] is True
    assert replacement_operation["fresh_task_explicit_fields"] == []
    assert "Under Arm" not in (replacement["linka"] or "")
    assert t4["after_replace"]["task_type"] == "booking"
    assert t4["after_replace"]["constraints"]["service_id"] is None
    assert t4["after_replace"]["constraints"]["doctor_id"] is None
    assert t4["after_replace"]["constraints"]["device_key"] is None
    assert t4["after_replace"]["constraints"]["date"] is None
    assert t4["after_replace"]["constraints"]["time"] is None
    assert t4["after_replace"]["option_snapshot"] is None
    assert t4["after_replace"]["derived"]["selected_slot_ref"] is None
    assert t4["fresh_task"]["task_type"] == "booking"
    assert t4["fresh_task"]["constraints"]["service_id"] != t4["old_task"]["constraints"]["service_id"]
    assert t4["fresh_task"]["constraints"]["doctor_id"] is None
    assert t4["fresh_task"]["constraints"]["device_key"] is None
    assert t4["fresh_task"]["constraints"]["date"] is None
    assert t4["fresh_task"]["constraints"]["time"] is None
    assert t4["fresh_task"]["option_snapshot"] is None
    assert (
        t4["fresh_task"]["write_authorization"]["source_turn_id"]
        != t4["old_task"]["write_authorization"]["source_turn_id"]
    )

    t5 = by_id["T5"]
    assert t5["errors"] == []
    assert len(t5["completed_appointments"]) == 1
    completed = t5["completed_action_context"]
    assert completed["operation_type"] == "book"
    assert completed["appointment_id"]
    assert completed["service_id"]
    assert completed["doctor_id"]
    assert completed["device_key"]

    fresh_turn = _turn(t5, "عايز احجز جلسة جديدة")
    fresh_operation = fresh_turn["operations"][0]
    assert fresh_operation["type"] == "book"
    assert fresh_operation["fresh_task"] is True
    assert fresh_operation["fresh_task_explicit_fields"] == []
    assert fresh_operation["continues_previous"] is False
    assert fresh_turn["write_attempted"] is False
    assert "Under Arm" not in (fresh_turn["linka"] or "")

    fresh_task = t5["new_goal_task"]
    assert fresh_task["task_type"] == "booking"
    assert fresh_task["constraints"]["service_id"] is None
    assert fresh_task["constraints"]["doctor_id"] is None
    assert fresh_task["constraints"]["device_key"] is None
    assert fresh_task["constraints"]["date"] is None
    assert fresh_task["constraints"]["time"] is None
    assert fresh_task["option_snapshot"] is None
    assert fresh_task["derived"]["selected_slot_ref"] is None
    assert "target" not in fresh_task
    assert (
        fresh_task["write_authorization"]["source_turn_id"]
        != t5["completed_booking_authorization_source"]
    )

    service_turn = _turn(t5, "Full Body")
    assert service_turn["write_attempted"] is False
    service_task = t5["after_service_task"]
    assert service_task["task_type"] == "booking"
    assert service_task["constraints"]["service_id"] != completed["service_id"]
    assert service_task["constraints"]["doctor_id"] is None
    assert service_task["constraints"]["device_key"] is None
    assert service_task["constraints"]["date"] is None
    assert service_task["constraints"]["time"] is None
    assert service_task["option_snapshot"] is None
    assert service_task["derived"]["selected_slot_ref"] is None
    assert (
        service_task["write_authorization"]["source_turn_id"]
        == fresh_task["write_authorization"]["source_turn_id"]
    )
    assert len(t5["after_service_state"].get("appointments", [])) == 1
    assert len(t5["final_state"].get("appointments", [])) == 2
    assert t5["writes"] == 2
    assert "booking_completed" in t5["turns"][-1]["goals"]



RESUME_START = datetime(2026, 10, 5, 14, 0, tzinfo=TZ)


def _raw_flow_snapshot(db, ws, conversation_id):
    if conversation_id is None:
        return None
    flow = db.scalar(
        select(ConversationFlowState)
        .where(
            ConversationFlowState.workspace_id == ws.id,
            ConversationFlowState.conversation_id == conversation_id,
        )
        .order_by(ConversationFlowState.created_at.desc())
        .limit(1)
    )
    if flow is None:
        return None
    return {
        "id": str(flow.id),
        "flow_type": flow.flow_type,
        "status": flow.status,
        "is_active": flow.is_active,
        "version": flow.version,
        "expires_at": flow.expires_at.isoformat(),
        "last_turn_at": flow.last_turn_at.isoformat(),
        "task": (flow.entity_state or {}).get("agent_core_v2", {}).get("active_task"),
    }


def _expire_current_flow(db, ws, conversation_id):
    flow = db.scalar(
        select(ConversationFlowState).where(
            ConversationFlowState.workspace_id == ws.id,
            ConversationFlowState.conversation_id == conversation_id,
            ConversationFlowState.is_active.is_(True),
        )
    )
    if flow is None:
        raise RuntimeError("expected active flow before expiry")
    flow.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    return str(flow.id)


def _latest_ai_metadata(db, conversation_id):
    row = db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.sender_type == "ai",
            Message.direction == "outbound",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    return dict(row.metadata_json or {}) if row is not None else {}


def _inject_automation(db, ws, c, appointment, *, rule_key, content):
    if c.cid is None:
        # Force a stable conversation identity before system automation.
        c.send("تمام")
    conversation = db.get(ConversationRow, c.cid)
    if conversation is None:
        raise RuntimeError("conversation missing before automation injection")

    flow_before = _raw_flow_snapshot(db, ws, c.cid)
    now = datetime.now(UTC)
    metadata = {
        "source": "automation_engine",
        "automation_job_id": str(uuid4()),
        "automation_rule_key": rule_key,
        "appointment_id": str(appointment.id),
        "whatsapp_template": {
            "name": f"eval_{rule_key}",
            "language_code": "ar",
            "body_parameters": [],
            "variant_count": 1,
        },
        "appointment": {
            "service_name": "eval",
            "date": appointment.start_at.astimezone(TZ).date().isoformat(),
            "time": appointment.start_at.astimezone(TZ).strftime("%H:%M"),
        },
    }
    message = Message(
        workspace_id=ws.id,
        conversation_id=c.cid,
        channel_connection_id=conversation.channel_connection_id,
        sender_type="system",
        direction="outbound",
        message_type="template",
        content=content,
        delivery_status="sent",
        metadata_json=metadata,
        created_at=now,
    )
    db.add(message)
    conversation.last_message_at = now
    db.flush()

    preview_inbound = Message(
        workspace_id=ws.id,
        conversation_id=c.cid,
        channel_connection_id=conversation.channel_connection_id,
        sender_type="patient",
        direction="inbound",
        message_type="text",
        content="preview",
        delivery_status="received",
        created_at=now + timedelta(seconds=1),
    )
    preview = {
        "recent_verified_read": _recent_verified_read_context(
            db, conversation=conversation, inbound=preview_inbound
        ),
        "recent_verified_action": _recent_verified_action_context(
            db, conversation=conversation, inbound=preview_inbound
        ),
        "pending_choice": _recent_pending_choice_context(
            db, conversation=conversation, inbound=preview_inbound
        ),
        "automation_context": _recent_automation_context(
            db,
            conversation=conversation,
            patient=c.p,
            inbound=preview_inbound,
        ),
    }
    return {
        "message_id": str(message.id),
        "metadata": metadata,
        "flow_before": flow_before,
        "flow_after": _raw_flow_snapshot(db, ws, c.cid),
        "context_preview": preview,
    }


def _seed_appointment(
    db,
    ws,
    p,
    service_row,
    doctor,
    branch_id,
    *,
    day,
    device_key="prime_lase",
    now=RESUME_START,
):
    adapter = get_clinic_adapter(db=db, workspace=ws)
    result = adapter.get_availability(
        AvailabilityRequest(
            branch_id=str(branch_id),
            service_id=str(service_row.id),
            booking_date=day,
            doctor_id=str(doctor.id),
            laser_device_key=device_key,
            now=now,
        )
    )
    if not result.slots:
        raise RuntimeError(
            f"no eval slot for {service_row.name} / {doctor.id} / {device_key} / {day}"
        )
    return _seed_future_appointment(
        db,
        ws,
        p,
        service=service_row,
        doctor_id=doctor.id,
        slot=result.slots[0],
        device_key=device_key,
    )


def _complete_underarm_booking(c):
    for message in ("عايز احجز Under Arm", "الخميس", "ديكا", "مريم", "الساعة 4"):
        if c.turns and "booking_completed" in c.turns[-1]["goals"]:
            break
        c.send(message)
    if not c.turns or "booking_completed" not in c.turns[-1]["goals"]:
        raise AssertionError("fixture booking did not complete")
    rows = c.db.scalars(
        select(Appointment)
        .where(
            Appointment.workspace_id == c.ws.id,
            Appointment.patient_id == c.p.id,
        )
        .order_by(Appointment.created_at.desc())
    ).all()
    if not rows:
        raise AssertionError("completed booking created no appointment")
    return rows[0]


def _fields(task):
    if not isinstance(task, dict):
        return {}
    constraints = task.get("constraints") or task.get("replacement") or {}
    return {
        "service_id": constraints.get("service_id"),
        "device_key": constraints.get("device_key"),
        "doctor_id": constraints.get("doctor_id"),
        "date": constraints.get("date"),
        "time": constraints.get("time"),
        "selected_slot_ref": (task.get("derived") or {}).get("selected_slot_ref"),
        "option_snapshot": task.get("option_snapshot"),
        "write_authorization": task.get("write_authorization"),
    }


def run_r1_same_day_resume(db, ws):
    p = patient(db, ws, "R1")
    c = Conversation("R1", db, ws, p, RESUME_START)
    c.send("عايز احجز Under Arm")
    c.send("ديكا")
    before = c.turns[-1]["active_task"]
    flow_before = _raw_flow_snapshot(db, ws, c.cid)

    c.set_now(RESUME_START + timedelta(hours=2))
    c.send("طب فيه يوم السبت؟")
    after = c.turns[-1]["active_task"]
    return {
        "id": "R1",
        "time_gap": "2 hours",
        "flow_before": flow_before,
        "flow_after": _raw_flow_snapshot(db, ws, c.cid),
        "active_task_before": before,
        "active_task_after": after,
        "fields_before": _fields(before),
        "fields_after": _fields(after),
        "turns": c.turns,
        "writes": sum(bool(t.get("write_attempted")) for t in c.turns),
        "db_after": state_snapshot(db, ws, p),
    }


def run_r2_next_day_resume(db, ws):
    p = patient(db, ws, "R2")
    c = Conversation("R2", db, ws, p, RESUME_START)
    for message in ("عايز احجز Under Arm", "ديكا", "السبت"):
        c.send(message)
    before = c.turns[-1]["active_task"]
    flow_before = _raw_flow_snapshot(db, ws, c.cid)
    c.set_now(RESUME_START + timedelta(hours=18))
    c.send("طب بعد الساعة 6؟")
    after = c.turns[-1]["active_task"]
    return {
        "id": "R2",
        "time_gap": "18 hours / next calendar day / within configured TTL",
        "flow_before": flow_before,
        "flow_after": _raw_flow_snapshot(db, ws, c.cid),
        "active_task_before": before,
        "active_task_after": after,
        "fields_before": _fields(before),
        "fields_after": _fields(after),
        "turns": c.turns,
        "writes": sum(bool(t.get("write_attempted")) for t in c.turns),
        "db_after": state_snapshot(db, ws, p),
    }


def _expired_booking_fixture(db, ws, label):
    p = patient(db, ws, label)
    c = Conversation(label, db, ws, p, RESUME_START)
    for message in ("عايز احجز Under Arm", "ديكا", "السبت"):
        if c.turns and c.turns[-1]["active_task"] is None:
            break
        c.send(message)
        # Keep it unfinished even if a write-ready exact slot would complete.
        if c.turns[-1].get("write_attempted"):
            raise AssertionError("expiry fixture unexpectedly wrote a booking")
    before = c.turns[-1]["active_task"]
    flow_before = _raw_flow_snapshot(db, ws, c.cid)
    old_flow_id = _expire_current_flow(db, ws, c.cid)
    return p, c, before, flow_before, old_flow_id


def run_r3_expired_flow(db, ws):
    p, c, before, flow_before, old_flow_id = _expired_booking_fixture(db, ws, "R3")
    c.set_now(RESUME_START + timedelta(hours=settings.agent_flow_ttl_hours + 2))
    c.send("عايز احجز")
    after = c.turns[-1]["active_task"]
    old_flow = db.get(ConversationFlowState, UUID(old_flow_id))
    return {
        "id": "R3",
        "time_gap": f">{settings.agent_flow_ttl_hours}h configured flow TTL",
        "configured_ttl_hours": settings.agent_flow_ttl_hours,
        "flow_before": flow_before,
        "expired_flow": {
            "id": old_flow_id,
            "status": old_flow.status,
            "is_active": old_flow.is_active,
        },
        "active_task_before": before,
        "active_task_after": after,
        "fields_before": _fields(before),
        "fields_after": _fields(after),
        "turns": c.turns,
        "writes": sum(bool(t.get("write_attempted")) for t in c.turns),
        "db_after": state_snapshot(db, ws, p),
    }


def run_r4_expired_explicit_service(db, ws):
    p, c, before, flow_before, old_flow_id = _expired_booking_fixture(db, ws, "R4")
    c.set_now(RESUME_START + timedelta(hours=settings.agent_flow_ttl_hours + 2))
    c.send("عايز أكمل حجز Under Arm")
    after = c.turns[-1]["active_task"]
    old_flow = db.get(ConversationFlowState, UUID(old_flow_id))
    return {
        "id": "R4",
        "time_gap": f">{settings.agent_flow_ttl_hours}h configured flow TTL",
        "configured_ttl_hours": settings.agent_flow_ttl_hours,
        "flow_before": flow_before,
        "expired_flow": {
            "id": old_flow_id,
            "status": old_flow.status,
            "is_active": old_flow.is_active,
        },
        "active_task_before": before,
        "active_task_after": after,
        "fields_before": _fields(before),
        "fields_after": _fields(after),
        "turns": c.turns,
        "writes": sum(bool(t.get("write_attempted")) for t in c.turns),
        "db_after": state_snapshot(db, ws, p),
    }


def run_r5_completed_reminder_ack(db, ws):
    p = patient(db, ws, "R5")
    c = Conversation("R5", db, ws, p, RESUME_START)
    appointment = _complete_underarm_booking(c)
    action_before = _latest_ai_metadata(db, c.cid).get("v2_action_context")
    automation = _inject_automation(
        db,
        ws,
        c,
        appointment,
        rule_key="appointment_reminder_6h",
        content="فكرك بميعادك النهاردة.",
    )
    c.send("تمام")
    return {
        "id": "R5",
        "time_gap": "automation after completed booking",
        "automation": automation,
        "recent_action_before_automation": action_before,
        "active_task_after": c.turns[-1]["active_task"],
        "turns": c.turns,
        "writes": sum(bool(t.get("write_attempted")) for t in c.turns[-1:]),
        "db_after": state_snapshot(db, ws, p),
        "reply_metadata": _latest_ai_metadata(db, c.cid),
    }


def run_r6_reminder_reschedule(db, ws, under, youssef, branch_id):
    p = patient(db, ws, "R6")
    appointment = _seed_appointment(
        db,
        ws,
        p,
        under,
        youssef,
        branch_id,
        day=datetime(2026, 10, 8, tzinfo=TZ).date(),
    )
    c = Conversation("R6", db, ws, p, RESUME_START)
    c.send("ميعادي الجاي امتى؟")
    automation = _inject_automation(
        db,
        ws,
        c,
        appointment,
        rule_key="appointment_reminder_6h",
        content="فكرك بميعادك النهاردة.",
    )
    c.send("ممكن أخليه الساعة 6؟")
    first_action_task = c.turns[-1]["active_task"]
    # If normal reschedule flow asks for the implied date explicitly, answer naturally.
    if c.turns[-1]["active_task"] is not None and "booking_completed" not in c.turns[-1]["goals"] and "reschedule_completed" not in c.turns[-1]["goals"]:
        needed = []
        for action in c.turns[-1].get("actions") or []:
            if isinstance(action, dict):
                needed.append(action)
        reply = c.turns[-1]["linka"] or ""
        if "يوم" in reply or "تاريخ" in reply:
            c.send("نفس اليوم")
        if "reschedule_completed" not in c.turns[-1]["goals"] and c.turns[-1]["active_task"] is not None:
            # Selection/confirmation of the verified exact-time option.
            c.send("الساعة 6")
    final = state_snapshot(db, ws, p)
    target_after = next(
        (
            row
            for row in final.get("appointments", [])
            if row.get("id") == str(appointment.id)
        ),
        None,
    )
    return {
        "id": "R6",
        "automation": automation,
        "target_appointment_id": str(appointment.id),
        "first_action_task": first_action_task,
        "turns": c.turns,
        "writes": sum(bool(t.get("write_attempted")) for t in c.turns),
        "target_after": target_after,
        "db_after": final,
    }


def run_r7_active_booking_unrelated_reminder(db, ws, under, mary, branch_id):
    p = patient(db, ws, "R7")
    appointment = _seed_appointment(
        db,
        ws,
        p,
        under,
        mary,
        branch_id,
        day=datetime(2026, 10, 8, tzinfo=TZ).date(),
    )
    c = Conversation("R7", db, ws, p, RESUME_START)
    c.send("عايز احجز Full Body")
    c.send("ديكا")
    task_before = c.turns[-1]["active_task"]
    flow_before = _raw_flow_snapshot(db, ws, c.cid)
    automation = _inject_automation(
        db,
        ws,
        c,
        appointment,
        rule_key="appointment_reminder_6h",
        content="فكرك بميعاد Under Arm.",
    )
    c.send("تمام")
    task_after_ack = c.turns[-1]["active_task"]
    flow_after_ack = _raw_flow_snapshot(db, ws, c.cid)
    ack_turn = c.turns[-1]
    c.send("طب نكمل الحجز")
    task_after_resume = c.turns[-1]["active_task"]
    return {
        "id": "R7",
        "automation": automation,
        "active_task_before": task_before,
        "active_task_after_ack": task_after_ack,
        "active_task_after_resume": task_after_resume,
        "fields_before": _fields(task_before),
        "fields_after_ack": _fields(task_after_ack),
        "fields_after_resume": _fields(task_after_resume),
        "flow_before": flow_before,
        "flow_after_ack": flow_after_ack,
        "ack_turn": ack_turn,
        "turns": c.turns,
        "writes": sum(bool(t.get("write_attempted")) for t in c.turns[-2:]),
        "db_after": state_snapshot(db, ws, p),
    }


def _completed_appointment_fixture(db, ws, p, under, mary, branch_id):
    appointment = _seed_appointment(
        db,
        ws,
        p,
        under,
        mary,
        branch_id,
        day=datetime(2026, 10, 8, tzinfo=TZ).date(),
    )
    appointment.status = "completed"
    appointment.completed_at = appointment.end_at
    db.flush()
    return appointment


def run_r8_post_visit_ack(db, ws, under, mary, branch_id):
    p = patient(db, ws, "R8")
    appointment = _completed_appointment_fixture(db, ws, p, under, mary, branch_id)
    c = Conversation("R8", db, ws, p, datetime(2026, 10, 9, 10, 0, tzinfo=TZ))
    c.send("أهلًا")
    automation = _inject_automation(
        db,
        ws,
        c,
        appointment,
        rule_key="post_visit_followup",
        content="حبيت أطمن عليكي بعد الجلسة.",
    )
    c.send("تمام الحمد لله")
    return {
        "id": "R8",
        "time_gap": "+1 day after completed appointment",
        "automation": automation,
        "active_task_after": c.turns[-1]["active_task"],
        "turns": c.turns,
        "writes": sum(bool(t.get("write_attempted")) for t in c.turns[-1:]),
        "db_after": state_snapshot(db, ws, p),
        "reply_metadata": _latest_ai_metadata(db, c.cid),
    }


def run_r9_post_visit_next_session(db, ws, under, mary, branch_id):
    p = patient(db, ws, "R9")
    appointment = _completed_appointment_fixture(db, ws, p, under, mary, branch_id)
    c = Conversation("R9", db, ws, p, datetime(2026, 10, 9, 10, 0, tzinfo=TZ))
    c.send("أهلًا")
    automation = _inject_automation(
        db,
        ws,
        c,
        appointment,
        rule_key="post_visit_followup",
        content="حبيت أطمن عليكي بعد الجلسة.",
    )
    c.send("عايز احجز الجلسة الجاية")
    after_request = c.turns[-1]["active_task"]
    after_request_state = state_snapshot(db, ws, p)
    # Finish the fresh next-session booking with only newly supplied temporal/doctor facts.
    for message in ("السبت", "يوسف", "الساعة 4"):
        if c.turns and "booking_completed" in c.turns[-1]["goals"]:
            break
        c.send(message)
    return {
        "id": "R9",
        "automation": automation,
        "old_appointment_id": str(appointment.id),
        "active_task_after_request": after_request,
        "fields_after_request": _fields(after_request),
        "state_after_request": after_request_state,
        "turns": c.turns,
        "writes": sum(bool(t.get("write_attempted")) for t in c.turns),
        "db_after": state_snapshot(db, ws, p),
    }


def assert_task1b_regressions(results):
    by_id = {row["id"]: row for row in results}

    r1 = by_id["R1"]
    assert r1["fields_before"]["service_id"] == r1["fields_after"]["service_id"]
    assert r1["fields_before"]["device_key"] == r1["fields_after"]["device_key"]
    assert r1["fields_after"]["date"] is not None
    assert r1["writes"] == 0

    r2 = by_id["R2"]
    assert r2["fields_before"]["service_id"] == r2["fields_after"]["service_id"]
    assert r2["fields_before"]["device_key"] == r2["fields_after"]["device_key"]
    assert r2["fields_after"]["time"] is not None
    assert r2["writes"] == 0

    r3 = by_id["R3"]
    assert r3["expired_flow"]["status"] == "expired"
    assert r3["expired_flow"]["is_active"] is False
    for key in ("service_id", "device_key", "doctor_id", "date", "time", "selected_slot_ref", "option_snapshot"):
        assert r3["fields_after"][key] is None
    assert r3["writes"] == 0

    r4 = by_id["R4"]
    assert r4["expired_flow"]["status"] == "expired"
    assert r4["fields_after"]["service_id"] is not None
    for key in ("device_key", "doctor_id", "date", "time", "selected_slot_ref", "option_snapshot"):
        assert r4["fields_after"][key] is None
    assert r4["writes"] == 0

    r5 = by_id["R5"]
    assert r5["automation"]["metadata"]["source"] == "automation_engine"
    preview = r5["automation"]["context_preview"]
    assert preview["recent_verified_read"] is None
    assert preview["recent_verified_action"] is None
    assert preview["pending_choice"] is None
    assert preview["automation_context"]["appointment_id"] == r5["automation"]["metadata"]["appointment_id"]
    assert r5["active_task_after"] is None
    assert r5["writes"] == 0
    assert not r5["reply_metadata"].get("v2_action_context")

    r6 = by_id["R6"]
    assert r6["automation"]["context_preview"]["automation_context"]["appointment_id"] == r6["target_appointment_id"]
    # The first lifecycle action after the reminder must be grounded to reminder metadata.
    reminder_turn = next(t for t in r6["turns"] if t["customer"] == "ممكن أخليه الساعة 6؟")
    assert reminder_turn["operations"][0]["automation_context_relationship"] == "appointment_action"
    assert any("appointments" in read for read in reminder_turn["verified_reads"])
    assert r6["target_after"] is not None

    r7 = by_id["R7"]
    assert r7["fields_after_ack"] == r7["fields_before"]
    assert r7["flow_after_ack"]["version"] == r7["flow_before"]["version"]
    assert r7["flow_after_ack"]["expires_at"] == r7["flow_before"]["expires_at"]
    assert r7["ack_turn"]["write_attempted"] is False
    assert r7["fields_after_resume"]["service_id"] == r7["fields_before"]["service_id"]
    assert r7["fields_after_resume"]["device_key"] == r7["fields_before"]["device_key"]

    r8 = by_id["R8"]
    assert r8["automation"]["context_preview"]["recent_verified_read"] is None
    assert r8["automation"]["context_preview"]["recent_verified_action"] is None
    assert r8["active_task_after"] is None
    assert r8["writes"] == 0
    assert not r8["reply_metadata"].get("v2_action_context")

    r9 = by_id["R9"]
    task = r9["active_task_after_request"]
    assert task["task_type"] == "booking"
    assert r9["fields_after_request"]["service_id"] is not None
    assert r9["fields_after_request"]["device_key"] == "prime_lase"
    for key in ("doctor_id", "date", "time", "selected_slot_ref", "option_snapshot"):
        assert r9["fields_after_request"][key] is None
    assert len(r9["state_after_request"].get("appointments", [])) == 1


output = Path(sys.argv[1])
mode = sys.argv[2] if len(sys.argv) > 2 else "after"
engine = create_engine(settings.database_url, pool_pre_ping=True)
conn = engine.connect()
outer = conn.begin()
db = Session(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
try:
    ws = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
    assert_demo_only(ws)
    under, full, mary, youssef, branch_id = prepare(db, ws)
    if mode == "task1b_r1":
        results = [run_r1_same_day_resume(db, ws)]
    elif mode == "task1b_r2":
        results = [run_r2_next_day_resume(db, ws)]
    elif mode == "task1b_r3":
        results = [run_r3_expired_flow(db, ws)]
    elif mode == "task1b_r4":
        results = [run_r4_expired_explicit_service(db, ws)]
    elif mode == "task1b_r5":
        results = [run_r5_completed_reminder_ack(db, ws)]
    elif mode == "task1b_r6":
        results = [run_r6_reminder_reschedule(db, ws, under, youssef, branch_id)]
    elif mode == "task1b_r7":
        results = [run_r7_active_booking_unrelated_reminder(db, ws, under, mary, branch_id)]
    elif mode == "task1b_r8":
        results = [run_r8_post_visit_ack(db, ws, under, mary, branch_id)]
    elif mode == "task1b_r9":
        results = [run_r9_post_visit_next_session(db, ws, under, mary, branch_id)]
    elif mode == "task1b":
        results = [
            run_r1_same_day_resume(db, ws),
            run_r2_next_day_resume(db, ws),
            run_r3_expired_flow(db, ws),
            run_r4_expired_explicit_service(db, ws),
            run_r5_completed_reminder_ack(db, ws),
            run_r6_reminder_reschedule(db, ws, under, youssef, branch_id),
            run_r7_active_booking_unrelated_reminder(db, ws, under, mary, branch_id),
            run_r8_post_visit_ack(db, ws, under, mary, branch_id),
            run_r9_post_visit_next_session(db, ws, under, mary, branch_id),
        ]
    elif mode == "t5_probe":
        results = [run_t5_completed_then_fresh(db, ws)]
    elif mode == "t4_probe":
        results = [run_t4(db, ws)]
    else:
        results = [run_t1(db, ws, under, full, mary, branch_id)]
        if mode in {"after", "probe"}:
            results.extend(
                [
                    run_t2(db, ws, under, mary, branch_id),
                    run_t3(db, ws),
                    run_t4(db, ws),
                    run_t5_completed_then_fresh(db, ws),
                ]
            )
        if mode == "after":
            assert_after_regressions(results)
    payload = {
        "mode": mode,
        "results": results,
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    if mode == "task1b":
        assert_task1b_regressions(results)
    selected = results[0]
    print("RESULT", selected["id"], "ERRORS", selected.get("errors", []))
    for turn in selected["turns"]:
        print("C:", turn["customer"])
        print("L:", turn["linka"])
        if turn.get("error"):
            print("E:", turn["error"])
        print("OPS:", turn["operations"])
        print("GOALS:", turn["goals"])
finally:
    db.close()
    if outer.is_active:
        outer.rollback()
    conn.close()
    engine.dispose()
