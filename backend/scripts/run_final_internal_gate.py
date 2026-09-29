from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from supabase import create_client

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings
from app.models.appointment import Appointment
from app.models.automation_job import AutomationJob
from app.models.automation_rule import AutomationRule
from app.models.conversation_flow_event import ConversationFlowEvent
from app.models.conversation_flow_state import ConversationFlowState
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.services.conversation_flows import (
    FlowStateConflictError,
    get_active_flow,
    start_flow,
    transition_flow,
)
from final_gate_scenarios import gate_ids, member_email_for
from staging_scenarios import REGRESSION_WORKSPACE_ID, MOCK_AUTOMATION_TOKEN, MOCK_CHANNEL_TOKEN, sid

DEFAULT_EMAIL = "adam1ezzat1@gmail.com"
DEFAULT_WORKSPACE_ID = str(REGRESSION_WORKSPACE_ID)


@dataclass
class Report:
    path: Path
    passed: int = 0
    warned: int = 0
    failed: int = 0

    def __post_init__(self):
        self.lines: list[str] = []

    def line(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def section(self, title: str) -> None:
        self.line()
        self.line(f"=== {title} ===")

    def result(self, name: str, state: str, detail: str = "") -> None:
        if state == "PASS": self.passed += 1
        elif state == "WARN": self.warned += 1
        else: self.failed += 1
        self.line(f"[{state}] {name}" + (f" — {detail}" if detail else ""))

    def save(self) -> None:
        self.section("SUMMARY")
        self.line(f"PASS={self.passed} WARN={self.warned} FAIL={self.failed}")
        self.path.write_text("\n".join(self.lines)+"\n", encoding="utf-8")


def _auth_user_id(obj: Any) -> str:
    user = getattr(obj, "user", None) or obj
    value = getattr(user, "id", None)
    if value is None and isinstance(user, dict): value = user.get("id")
    if value is None: raise RuntimeError("Supabase admin response has no user id.")
    return str(value)


def ensure_ephemeral_auth_user(email: str, password: str) -> str:
    admin = create_client(
        settings.supabase_url,
        settings.supabase_secret_key,
    )
    users_result = admin.auth.admin.list_users(page=1, per_page=1000)
    users = getattr(users_result, "users", users_result)
    existing = None
    for user in users:
        if str(getattr(user, "email", "")).lower() == email.lower():
            existing = user
            break
    marker = "tia-final-internal-gate"
    if existing is not None:
        metadata = getattr(existing, "user_metadata", None) or {}
        if not isinstance(metadata, dict) or metadata.get("tia_test_marker") != marker:
            raise RuntimeError(
                "The deterministic final-gate email already belongs to a non-test "
                "Supabase user. Use --email with another admin account or remove the "
                "conflicting alias manually."
            )
        uid = str(getattr(existing, "id"))
        updated = admin.auth.admin.update_user_by_id(
            uid,
            {
                "password": password,
                "email_confirm": True,
                "user_metadata": {
                    "name": "Linka Final Gate Member",
                    "tia_test_marker": marker,
                },
            },
        )
        return _auth_user_id(updated)
    created = admin.auth.admin.create_user(
        {
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {
                "name": "Linka Final Gate Member",
                "tia_test_marker": marker,
            },
        }
    )
    return _auth_user_id(created)


def delete_ephemeral_auth_user(user_id: str) -> None:
    admin = create_client(
        settings.supabase_url,
        settings.supabase_secret_key,
    )
    admin.auth.admin.delete_user(user_id)


def run_captured_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    Capture child-process output as UTF-8 on every platform.

    Node/Playwright writes UTF-8 even when the Windows Python locale is cp1252.
    Explicit decoding prevents subprocess reader-thread UnicodeDecodeError and
    guarantees stdout/stderr are strings.
    """
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:3000")
    parser.add_argument("--frontend-e2e", action="store_true")
    parser.add_argument("--no-seed", action="store_true")
    parser.add_argument("--keep-fixtures", action="store_true")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = Report(Path.cwd() / f"tia_final_internal_gate_{stamp}.txt")
    report.section("Linka v0.15.4 FINAL INTERNAL GATE")

    if settings.is_production:
        report.result("Production safety gate", "FAIL", "Refusing production.")
        report.save(); return 2
    if not settings.supabase_secret_key:
        report.result("Supabase secret key available server-side", "FAIL")
        report.save(); return 2

    password = input("Supabase admin password: ")
    member_password = secrets.token_urlsafe(28) + "A9!"
    member_email = member_email_for(args.email)
    workspace_id = UUID(args.workspace_id)
    ids = gate_ids(workspace_id)
    auth_user_id: str | None = None

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    base = args.base_url.rstrip("/") + "/api/v1"
    client = httpx.Client(timeout=180.0)

    try:
        if not args.no_seed:
            proc = run_captured_process(
                [sys.executable, str(BACKEND_DIR/"scripts"/"seed_full_staging_demo.py")],
                cwd=BACKEND_DIR,
            )
            if proc.returncode != 0:
                report.line(proc.stdout); report.line(proc.stderr)
                report.result("Reset full staging scenarios", "FAIL", f"exit={proc.returncode}")
                report.save(); return 2
            report.result("Reset full staging scenarios", "PASS")

        admin_auth = create_client(
            settings.supabase_url, settings.supabase_publishable_key
        ).auth.sign_in_with_password({"email": args.email, "password": password})
        admin_token = admin_auth.session.access_token
        report.result("Admin Supabase login", "PASS")

        auth_user_id = ensure_ephemeral_auth_user(member_email, member_password)
        report.result("Ephemeral real Supabase member identity", "PASS")

        seed = run_captured_process(
            [
                sys.executable, str(BACKEND_DIR/"scripts"/"seed_final_internal_gate.py"),
                "--workspace-id", str(workspace_id),
                "--member-auth-user-id", auth_user_id,
                "--member-email", member_email,
            ],
            cwd=BACKEND_DIR,
        )
        if seed.returncode != 0:
            report.line(seed.stdout); report.line(seed.stderr)
            report.result("Seed final gate fixtures", "FAIL", f"exit={seed.returncode}")
            report.save(); return 2
        report.result("Seed final gate fixtures", "PASS")

        quality = run_captured_process(
            [
                sys.executable,
                str(BACKEND_DIR/"scripts"/"validate_final_gate_fixture_quality.py"),
                "--workspace-id",
                str(workspace_id),
            ],
            cwd=BACKEND_DIR,
        )
        if quality.returncode != 0:
            report.line(quality.stdout or "")
            report.line(quality.stderr or "")
            report.result(
                "Final Gate fixture data quality",
                "FAIL",
                f"exit={quality.returncode}",
            )
            report.save()
            return 2
        report.result("Final Gate fixture data quality", "PASS")

        member_auth = create_client(
            settings.supabase_url, settings.supabase_publishable_key
        ).auth.sign_in_with_password({"email": member_email, "password": member_password})
        member_token = member_auth.session.access_token
        report.result("Real member Supabase login", "PASS")

        admin_headers = {"Authorization": f"Bearer {admin_token}", "X-Workspace-ID": str(workspace_id)}
        member_a_headers = {"Authorization": f"Bearer {member_token}", "X-Workspace-ID": str(workspace_id)}
        member_b_headers = {"Authorization": f"Bearer {member_token}", "X-Workspace-ID": str(ids["secondary_workspace"])}
        adapter_headers = {"X-Channel-Token": MOCK_CHANNEL_TOKEN}
        automation_headers = {"X-Automation-Token": MOCK_AUTOMATION_TOKEN}

        def request(name: str, method: str, path: str, *, headers=None, body=None, expected=(200,), predicate=None, quiet=False):
            started=time.perf_counter()
            try:
                r=client.request(method, base+path, headers=headers or admin_headers, json=body)
                elapsed=time.perf_counter()-started
                try: data=r.json()
                except ValueError: data=r.text
                ok=r.status_code in expected
                if ok and predicate is not None:
                    try: ok=bool(predicate(data))
                    except Exception: ok=False
                if not quiet:
                    report.result(name, "PASS" if ok else "FAIL", f"HTTP {r.status_code}, {elapsed:.2f}s")
                return r, data, ok
            except Exception as exc:
                if not quiet: report.result(name,"FAIL",f"{type(exc).__name__}: {exc}")
                return None, None, False

        # ------------------------------------------------------------
        report.section("1. TRUE CONCURRENT BOOKING RACE")
        branch=sid(workspace_id,"branch:regression-main")
        doctor=sid(workspace_id,"doctor:regression-main")
        service=sid(workspace_id,"service:regression-laser")
        cairo=ZoneInfo("Africa/Cairo")
        race_date=(datetime.now(cairo).date()+timedelta(days=6)).isoformat()
        _, avail, ok=request(
            "Find race-test availability","GET",
            f"/booking/availability?branch_id={branch}&service_id={service}&doctor_id={doctor}&date={race_date}",
            predicate=lambda d:isinstance(d,dict) and len(d.get("slots",[]))>=1,
        )
        if ok:
            slot=avail["slots"][0]
            def create_race(patient_id: UUID, key: str):
                return httpx.post(
                    base+"/booking/appointments",
                    headers={**admin_headers,"Idempotency-Key":key},
                    json={
                        "patient_id":str(patient_id),"branch_id":str(branch),
                        "doctor_id":str(doctor),"service_id":str(service),
                        "start_at":slot["start_at"],"source":"staff",
                        "customer_note":"Final gate concurrent race",
                    }, timeout=60.0,
                )
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                futures=[
                    ex.submit(create_race,ids["race_patient_a"],"final-gate-race-a"),
                    ex.submit(create_race,ids["race_patient_b"],"final-gate-race-b"),
                ]
                responses=[f.result() for f in futures]
            statuses=sorted(r.status_code for r in responses)
            report.result("Exactly one concurrent request succeeds", "PASS" if statuses in ([201,409],[200,409]) else "FAIL", f"statuses={statuses}")
            with SessionLocal() as db:
                target=datetime.fromisoformat(slot["start_at"].replace("Z","+00:00"))
                rows=list(db.scalars(select(Appointment).where(
                    Appointment.workspace_id==workspace_id,
                    Appointment.doctor_id==doctor,
                    Appointment.start_at==target,
                    Appointment.status.in_(("pending","confirmed","checked_in","in_progress")),
                )))
                report.result("Database contains one active appointment for raced slot", "PASS" if len(rows)==1 else "FAIL", f"count={len(rows)}")
        else:
            report.result("Exactly one concurrent request succeeds","FAIL","No slot")
            report.result("Database contains one active appointment for raced slot","FAIL","No slot")

        # ------------------------------------------------------------
        report.section("2. MULTI-TENANT ISOLATION")
        isolation_cases=[
            ("A cannot read B patient", admin_headers, f"/crm/patients/{ids['secondary_patient']}", (404,)),
            ("A cannot read B appointment", admin_headers, f"/booking/appointments/{ids['secondary_appointment']}", (404,)),
            ("A cannot read B conversation", admin_headers, f"/inbox/conversations/{ids['secondary_conversation']}", (404,)),
            ("B cannot read A patient", member_b_headers, f"/crm/patients/{ids['member_patient']}", (404,)),
            ("B cannot read A appointment", member_b_headers, f"/booking/appointments/{sid(workspace_id, 'appointment:confirmed')}", (404,)),
            ("B cannot read A conversation", member_b_headers, f"/inbox/conversations/{sid(workspace_id, 'conversation:handoff_complaint')}", (404,)),
        ]
        for name, hdr, path, expected in isolation_cases:
            request(name,"GET",path,headers=hdr,expected=expected)
        request(
            "A cannot mutate B patient","PATCH",f"/crm/patients/{ids['secondary_patient']}",
            headers=admin_headers,body={"first_name":"IsolationBroken"},expected=(404,),
        )
        request(
            "B context cannot invoke agent on A patient","POST","/agent/chat",
            headers=member_b_headers,
            body={"patient_id":str(ids["member_patient"]),"channel":"web","message":"مرحبا"},
            expected=(422,404),
        )

        # ------------------------------------------------------------
        report.section("3. REAL MEMBER RBAC")
        request("Member can read CRM","GET","/crm/patients?limit=20",headers=member_a_headers)
        request("Member can read booking","GET","/booking/appointments?limit=20",headers=member_a_headers)
        member_date=(datetime.now(cairo).date()+timedelta(days=7)).isoformat()
        _, member_avail, member_avail_ok=request(
            "Member can discover availability","GET",
            f"/booking/availability?branch_id={branch}&service_id={service}&doctor_id={doctor}&date={member_date}",
            headers=member_a_headers,
            predicate=lambda d:isinstance(d,dict) and len(d.get("slots",[]))>=1,
        )
        if member_avail_ok:
            request(
                "Member can create operational booking","POST","/booking/appointments",
                headers={**member_a_headers,"Idempotency-Key":"final-gate-member-booking"},
                body={
                    "patient_id":str(ids["member_patient"]),"branch_id":str(branch),
                    "doctor_id":str(doctor),"service_id":str(service),
                    "start_at":member_avail["slots"][0]["start_at"],"source":"staff",
                    "customer_note":"Final gate real member booking",
                },
                expected=(201,),
            )
        else:
            report.result("Member can create operational booking","FAIL","No availability")
        request("Member can read Team Inbox","GET","/inbox/handoffs?limit=20",headers=member_a_headers)
        request(
            "Member can add patient note","POST",f"/crm/patients/{ids['member_patient']}/notes",
            headers=member_a_headers,body={"content":"Final gate member operational write","note_type":"general","is_pinned":False},expected=(201,),
        )
        request("Member denied workspace member list","GET","/auth/workspace/members",headers=member_a_headers,expected=(403,))
        request(
            "Member denied clinic mutation","POST","/clinic/branches",headers=member_a_headers,
            body={"name":"Forbidden Branch","code":"forbidden-final-gate","city":"Cairo","country_code":"EG","timezone":"Africa/Cairo","is_active":True},
            expected=(403,),
        )
        request(
            "Member denied booking settings mutation","PUT","/clinic/booking-settings",headers=member_a_headers,
            body={"slot_interval_minutes":15,"minimum_notice_minutes":60,"booking_horizon_days":90,"cancellation_notice_minutes":720,"allow_same_day_booking":True,"require_confirmation":True,"default_currency":"EGP"},
            expected=(403,),
        )

        # ------------------------------------------------------------
        report.section("4. STATEFUL WORKFLOW CONCURRENCY + EXPIRY")
        # Reuse the seeded channel conversation only as a valid FK owner; use a dedicated web flow row.
        with SessionLocal() as db:
            # Create a dedicated flow against member patient's new conversation through direct model setup.
            from app.models.conversation import Conversation
            conv=Conversation(
                workspace_id=workspace_id, patient_id=ids["member_patient"], channel="web",
                status="open", started_at=datetime.now(timezone.utc), last_message_at=datetime.now(timezone.utc),
                subject="Final gate optimistic flow",
            )
            db.add(conv); db.commit(); db.refresh(conv)
            flow=start_flow(
                db, workspace_id=workspace_id, conversation_id=conv.id,
                patient_id=ids["member_patient"], flow_type="booking",
                capabilities=["availability_discovery","appointment_creation"],
                entity_state={}, missing_information=["date"], last_decision={},
                run_id=conv.id,
            )
            db.commit(); flow_id=flow.id
        db1=SessionLocal(); db2=SessionLocal()
        try:
            f1=db1.get(ConversationFlowState,flow_id); f2=db2.get(ConversationFlowState,flow_id)
            transition_flow(db1,f1,actor_type="system",event_type="updated",run_id=None,status="collecting_requirements",missing_information=["time"])
            db1.commit()
            conflict=False
            try:
                transition_flow(db2,f2,actor_type="system",event_type="updated",run_id=None,status="collecting_requirements",missing_information=["branch"])
                db2.commit()
            except FlowStateConflictError:
                conflict=True
            report.result("Stale workflow version is rejected", "PASS" if conflict else "FAIL")
        finally:
            db1.close(); db2.close()
        with SessionLocal() as db:
            flow=db.get(ConversationFlowState,flow_id)
            flow.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
            db.commit()
            active=get_active_flow(db,workspace_id=workspace_id,conversation_id=flow.conversation_id,patient_id=flow.patient_id,run_id=None)
            db.commit()
            db.refresh(flow)
            expired_event=db.scalar(select(ConversationFlowEvent).where(
                ConversationFlowEvent.flow_state_id==flow.id,
                ConversationFlowEvent.event_type=="expired",
            ))
            report.result("Expired workflow is closed and audited", "PASS" if active is None and flow.status=="expired" and expired_event is not None else "FAIL", f"status={flow.status}")

        # ------------------------------------------------------------
        report.section("5. AUTOMATION LIFECYCLE")
        rule_id=ids["automation_rule"]
        request("Final gate automation rule visible","GET","/automations/rules",predicate=lambda d:any(str(x.get("id"))==str(rule_id) for x in d if isinstance(x,dict)))

        def next_slots(days:int):
            date=(datetime.now(cairo).date()+timedelta(days=days)).isoformat()
            _, data, good=request(
                f"Availability +{days}d","GET",
                f"/booking/availability?branch_id={branch}&service_id={service}&doctor_id={doctor}&date={date}",
                predicate=lambda d:isinstance(d,dict) and len(d.get("slots",[]))>=2,
            )
            return data.get("slots",[]) if good else []

        slots8=next_slots(8)
        if slots8:
            _, created, good=request(
                "Create automation reschedule appointment","POST","/booking/appointments",
                body={"patient_id":str(ids["automation_reschedule_patient"]),"branch_id":str(branch),"doctor_id":str(doctor),"service_id":str(service),"start_at":slots8[0]["start_at"],"source":"staff"},
                expected=(201,),
            )
            if good:
                old_id=UUID(created["id"])
                request("Plan automation jobs before reschedule","POST","/automations/adapter/tick",headers=automation_headers,body={"limit":20,"planning_horizon_days":14})
                with SessionLocal() as db:
                    old_job=db.scalar(select(AutomationJob).where(AutomationJob.workspace_id==workspace_id,AutomationJob.rule_id==rule_id,AutomationJob.appointment_id==old_id))
                    old_job_id=old_job.id if old_job else None
                slots9=next_slots(9)
                if slots9:
                    _, replacement, moved=request(
                        "Reschedule appointment for automation lifecycle","POST",f"/booking/appointments/{old_id}/reschedule",
                        body={"start_at":slots9[0]["start_at"],"reason":"Final gate reschedule"},expected=(200,),
                    )
                    if moved:
                        new_id=UUID(replacement["id"])
                        request("Replan after reschedule","POST","/automations/adapter/tick",headers=automation_headers,body={"limit":20,"planning_horizon_days":14})
                        with SessionLocal() as db:
                            old_job=db.get(AutomationJob,old_job_id) if old_job_id else None
                            new_jobs=list(db.scalars(select(AutomationJob).where(AutomationJob.workspace_id==workspace_id,AutomationJob.rule_id==rule_id,AutomationJob.appointment_id==new_id)))
                            report.result("Reschedule cancels old reminder and creates one replacement", "PASS" if old_job is not None and old_job.status=="cancelled" and len(new_jobs)==1 and new_jobs[0].status in {"queued","processing"} else "FAIL", f"old={getattr(old_job,'status',None)} new_count={len(new_jobs)}")
                    else: report.result("Reschedule cancels old reminder and creates one replacement","FAIL","Reschedule failed")
                else: report.result("Reschedule cancels old reminder and creates one replacement","FAIL","No replacement slot")
            else: report.result("Reschedule cancels old reminder and creates one replacement","FAIL","Appointment creation failed")
        else: report.result("Reschedule cancels old reminder and creates one replacement","FAIL","No source slots")

        slots10=next_slots(10)
        cancel_job_id=None
        if slots10:
            _, cancel_appt, good=request(
                "Create automation cancellation appointment","POST","/booking/appointments",
                body={"patient_id":str(ids["automation_cancel_patient"]),"branch_id":str(branch),"doctor_id":str(doctor),"service_id":str(service),"start_at":slots10[0]["start_at"],"source":"staff"}, expected=(201,),
            )
            if good:
                cancel_appt_id=UUID(cancel_appt["id"])
                request("Plan job before cancellation","POST","/automations/adapter/tick",headers=automation_headers,body={"limit":20,"planning_horizon_days":14})
                with SessionLocal() as db:
                    j=db.scalar(select(AutomationJob).where(AutomationJob.workspace_id==workspace_id,AutomationJob.rule_id==rule_id,AutomationJob.appointment_id==cancel_appt_id)); cancel_job_id=j.id if j else None
                request("Cancel automation target appointment","POST",f"/booking/appointments/{cancel_appt_id}/cancel",body={"reason":"Final gate cancellation","override_policy":True})
                request("Replan after cancellation","POST","/automations/adapter/tick",headers=automation_headers,body={"limit":20,"planning_horizon_days":14})
                with SessionLocal() as db:
                    j=db.get(AutomationJob,cancel_job_id) if cancel_job_id else None
                    report.result("Cancellation removes future reminder", "PASS" if j is not None and j.status=="cancelled" else "FAIL", f"status={getattr(j,'status',None)}")
            else: report.result("Cancellation removes future reminder","FAIL","Appointment create failed")
        else: report.result("Cancellation removes future reminder","FAIL","No slots")

        # Disabled-rule cancellation + stale reclaim.
        request("Disable final gate automation rule","PATCH",f"/automations/rules/{rule_id}",body={"enabled":False})
        request("Planner handles disabled rule","POST","/automations/adapter/tick",headers=automation_headers,body={"limit":20,"planning_horizon_days":14})
        with SessionLocal() as db:
            active_jobs=list(db.scalars(select(AutomationJob).where(AutomationJob.workspace_id==workspace_id,AutomationJob.rule_id==rule_id,AutomationJob.status.in_(("queued","failed")))))
            report.result("Disabled rule leaves no queued/failed jobs", "PASS" if not active_jobs else "FAIL", f"active={len(active_jobs)}")
        request("Re-enable final gate automation rule","PATCH",f"/automations/rules/{rule_id}",body={"enabled":True})
        # Insert a deterministic stale processing job using any gate appointment that exists.
        with SessionLocal() as db:
            appt=db.scalar(select(Appointment).where(Appointment.workspace_id==workspace_id,Appointment.patient_id.in_((ids["automation_reschedule_patient"],ids["automation_cancel_patient"]))).order_by(Appointment.created_at.desc()))
            if appt is not None:
                stale=AutomationJob(
                    workspace_id=workspace_id, rule_id=rule_id, appointment_id=appt.id,
                    patient_id=appt.patient_id, status="processing",
                    scheduled_for=datetime.now(timezone.utc)-timedelta(minutes=1),
                    dedupe_key=f"final-gate-stale:{appt.id}", attempts=1,
                    locked_at=datetime.now(timezone.utc)-timedelta(minutes=20),
                    payload_json={"marker":"final-gate-stale"}, result_json={},
                )
                db.add(stale); db.commit(); db.refresh(stale); stale_id=stale.id
            else: stale_id=None
        if stale_id:
            _, tick, good=request("Stale automation processing job is reclaimable","POST","/automations/adapter/tick",headers=automation_headers,body={"limit":100,"planning_horizon_days":14},predicate=lambda d:any(str(x.get("job_id"))==str(stale_id) for x in d.get("claimed",[]) if isinstance(x,dict)))
        else:
            report.result("Stale automation processing job is reclaimable","FAIL","No appointment")

        # ------------------------------------------------------------
        report.section("6. CHANNEL + HANDOFF RACES")
        inbound_body={
            "external_event_id":"final-gate-active-handoff-event",
            "external_message_id":"final-gate-active-handoff-message",
            "external_user_id":"final-gate-channel-user",
            "external_conversation_id":"final-gate-channel-conversation",
            "display_name":"FinalGate Channel","phone":"+201099920006",
            "message_type":"text","text":"لسه مستني رد من الفريق","metadata":{"marker":"final-gate"},
        }
        _, accepted, good=request("Inbound persists during active handoff","POST","/channels/adapter/inbound",headers=adapter_headers,body=inbound_body,expected=(202,),predicate=lambda d:d.get("duplicate") is False)
        if good:
            event_id=accepted["event_id"]
            _, processed, processed_ok=request("Active handoff pauses AI on channel processing","POST",f"/channels/adapter/inbound/{event_id}/process",headers=adapter_headers,body={},predicate=lambda d:d.get("agent_paused") is True and d.get("outbound_message_id") is None and d.get("dispatch_id") is None)
            with SessionLocal() as db:
                inbound_message=db.get(Message,UUID(accepted["message_id"]))
                ai_outbound=[]
                if inbound_message is not None:
                    ai_outbound=list(db.scalars(select(Message).where(
                        Message.workspace_id==workspace_id,
                        Message.conversation_id==UUID(accepted["conversation_id"]),
                        Message.sender_type=="ai",
                        Message.direction=="outbound",
                        Message.created_at>=inbound_message.created_at,
                    )))
                report.result(
                    "Paused inbound creates no AI outbound message or dispatch",
                    "PASS" if not ai_outbound else "FAIL",
                    f"ai_outbound={len(ai_outbound)}",
                )
        else:
            report.result("Active handoff pauses AI on channel processing","FAIL","Inbound failed")
            report.result("Paused inbound creates no AI outbound message or dispatch","FAIL","Inbound failed")

        provider_id="final-gate-provider-message"
        request("Provider callback advances to read","POST","/channels/adapter/outbox/provider-status",headers=adapter_headers,body={"external_event_id":"final-gate-read","provider_message_id":provider_id,"status":"read","metadata":{}},expected=(202,),predicate=lambda d:d.get("dispatch_status")=="read")
        request("Late failed callback cannot downgrade read","POST","/channels/adapter/outbox/provider-status",headers=adapter_headers,body={"external_event_id":"final-gate-late-failed","provider_message_id":provider_id,"status":"failed","error":"late provider failure","metadata":{}},expected=(202,),predicate=lambda d:d.get("dispatch_status")=="read")
        request("Late sent callback cannot downgrade read","POST","/channels/adapter/outbox/provider-status",headers=adapter_headers,body={"external_event_id":"final-gate-late-sent","provider_message_id":provider_id,"status":"sent","metadata":{}},expected=(202,),predicate=lambda d:d.get("dispatch_status")=="read")

        # ------------------------------------------------------------
        report.section("7. FRONTEND E2E")
        if args.frontend_e2e:
            frontend_dir=PROJECT_DIR/"frontend"
            if not frontend_dir.exists():
                report.result("Frontend Playwright E2E","FAIL","frontend directory missing")
            else:
                env=os.environ.copy()
                env.update({
                    "TIA_E2E_BASE_URL":args.frontend_url,
                    "TIA_E2E_ADMIN_EMAIL":args.email,
                    "TIA_E2E_ADMIN_PASSWORD":password,
                    "TIA_E2E_MEMBER_EMAIL":member_email,
                    "TIA_E2E_MEMBER_PASSWORD":member_password,
                    "TIA_E2E_PRIMARY_WORKSPACE_ID":str(workspace_id),
                    "TIA_E2E_SECONDARY_WORKSPACE_ID":str(ids["secondary_workspace"]),
                })
                npm_executable = (
                    shutil.which("npm.cmd")
                    if os.name == "nt"
                    else shutil.which("npm")
                )
                if npm_executable is None:
                    # Some Windows installations expose npm without the .cmd
                    # suffix through PATH-aware shells. Check it as a fallback,
                    # but never let subprocess raise an opaque WinError 2.
                    npm_executable = shutil.which("npm")

                if npm_executable is None:
                    report.result(
                        "Frontend Playwright E2E",
                        "FAIL",
                        "npm executable not found in PATH. Install Node.js or "
                        "add the Node.js installation directory to PATH.",
                    )
                else:
                    env["NO_COLOR"] = "1"
                    proc = run_captured_process(
                        [npm_executable, "run", "test:e2e"],
                        cwd=frontend_dir,
                        env=env,
                    )
                    if proc.returncode == 0:
                        report.result("Frontend Playwright E2E", "PASS")
                    else:
                        report.line((proc.stdout or "")[-8000:])
                        report.line((proc.stderr or "")[-8000:])
                        report.result(
                            "Frontend Playwright E2E",
                            "FAIL",
                            f"exit={proc.returncode}",
                        )
        else:
            report.result("Frontend Playwright E2E","WARN","Run again with --frontend-e2e after installing Chromium")

    except Exception as exc:
        report.result("Unhandled final gate error","FAIL",f"{type(exc).__name__}: {exc}")
    finally:
        report.save()
        print(f"Results saved to: {report.path}")
        if report.failed == 0 and not args.keep_fixtures and auth_user_id is not None:
            cleanup = run_captured_process(
                [
                    sys.executable,
                    str(BACKEND_DIR/"scripts"/"cleanup_final_internal_gate.py"),
                    "--workspace-id",
                    str(workspace_id),
                    "--member-email",
                    member_email,
                ],
                cwd=BACKEND_DIR,
            )
            if cleanup.returncode==0:
                try:
                    delete_ephemeral_auth_user(auth_user_id)
                    print("Final-gate fixtures and ephemeral Supabase user cleaned up.")
                except Exception as exc:
                    print(f"WARN: Could not delete ephemeral Supabase user: {type(exc).__name__}")
            else:
                print("WARN: Final-gate DB cleanup failed. Fixtures were kept for inspection.")
        elif report.failed > 0:
            print("Fixtures kept because the gate failed; rerunning the seed will reset them safely.")

    return 0 if report.failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
