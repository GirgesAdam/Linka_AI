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
from app.models.booking_settings import BookingSettings
from app.models.clinic_inventory import ClinicLaserDevice, ServiceDevicePrice
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.service import Service
from app.models.working_hours import DoctorAvailabilityWindow, DoctorWorkingHour
from app.models.workspace import Workspace
from app.services.agent_v2 import live_chat as live_chat_module
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
from tools.agent_eval.run_batch_02 import _ensure_batch2_catalog_fixtures
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
    _ensure_batch2_catalog_fixtures(db, ws)
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
    fresh = c.send("عايز احجز Full Body كمان")
    if fresh is not None:
        for message in ("الخميس", "كانديلا", "الساعة 7", "يوسف", "احجز"):
            if c.turns and "booking_completed" in c.turns[-1]["goals"]:
                break
            c.send(message)
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


def run_t4(db, ws):
    p = patient(db, ws, "T4")
    c = Conversation("T4", db, ws, p, SESSION1)
    c.send("عايز احجز Under Arm")
    c.send("الخميس")
    old_task = c.turns[-1]["active_task"]
    c.send("عايز احجز Full Body كمان كحجز تاني")
    return {
        "id": "T4",
        "turns": c.turns,
        "errors": c.errors,
        "old_task": old_task,
        "fresh_task": c.turns[-1]["active_task"],
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
    fresh = _turn(t1, "عايز احجز Full Body كمان")["active_task"]
    assert fresh["task_type"] == "booking"
    assert fresh["constraints"]["service_id"] != t1["pre_gap_task"]["replacement"]["service_id"]
    assert fresh["constraints"]["doctor_id"] is None
    assert fresh["constraints"]["device_key"] is None
    assert fresh["constraints"]["date"] is None
    assert fresh["constraints"]["time"] is None
    assert fresh["option_snapshot"] is None
    assert fresh["derived"]["selected_slot_ref"] is None
    assert (
        fresh["write_authorization"]["source_turn_id"]
        != t1["pre_gap_task"]["write_authorization"]["source_turn_id"]
    )
    thursday = _turn(t1, "الخميس")["active_task"]
    assert thursday["task_type"] == "booking"
    assert thursday["constraints"]["time"] is None
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
    results = [run_t1(db, ws, under, full, mary, branch_id)]
    if mode == "after":
        results.extend(
            [
                run_t2(db, ws, under, mary, branch_id),
                run_t3(db, ws),
                run_t4(db, ws),
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
    t1 = results[0]
    print("T1_ERRORS", t1["errors"])
    print("T1_NEW_APPOINTMENTS", len(t1["new_appointments"]))
    for turn in t1["turns"]:
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
