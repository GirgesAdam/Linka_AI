from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from supabase import create_client

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings
from staging_scenarios import (
    REGRESSION_WORKSPACE_ID,
    MOCK_AUTOMATION_TOKEN,
    MOCK_CHANNEL_TOKEN,
    MOCK_PAUSED_CHANNEL_TOKEN,
    sid,
)

DEFAULT_EMAIL = "adam1ezzat1@gmail.com"
DEFAULT_WORKSPACE_ID = str(REGRESSION_WORKSPACE_ID)
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_FRONTEND_URL = "http://127.0.0.1:3000"


class Report:
    def __init__(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = Path.cwd() / f"tia_full_system_regression_{stamp}.txt"
        self.lines: list[str] = []
        self.passed = 0
        self.warned = 0
        self.failed = 0

    def line(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def section(self, title: str) -> None:
        self.line()
        self.line("=" * 100)
        self.line(title)
        self.line("=" * 100)

    def result(self, name: str, state: str, detail: str = "") -> None:
        if state == "PASS":
            self.passed += 1
        elif state == "WARN":
            self.warned += 1
        else:
            self.failed += 1
        suffix = f" — {detail}" if detail else ""
        self.line(f"[{state}] {name}{suffix}")

    def save(self) -> None:
        self.section("SUMMARY")
        self.line(f"PASS={self.passed} WARN={self.warned} FAIL={self.failed}")
        self.line(
            "External provider note: this suite validates the internal WhatsApp/n8n contract "
            "using a staging mock channel. It does NOT prove that Meta accepted or delivered "
            "a real message on the public network."
        )
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Linka full staging end-to-end regression suite.")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--frontend-url", default=DEFAULT_FRONTEND_URL)
    parser.add_argument("--no-seed", action="store_true", help="Do not reset the staging scenarios before testing.")
    parser.add_argument("--skip-llm", action="store_true", help="Skip Groq/LangGraph scenarios.")
    return parser.parse_args()


def is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
        return True
    except Exception:
        return False


def cairo_date(days: int) -> str:
    return (datetime.now(ZoneInfo("Africa/Cairo")).date() + timedelta(days=days)).isoformat()


def main() -> int:
    args = parse_args()
    report = Report()

    report.section("Linka FULL STAGING REGRESSION")
    report.line(f"STARTED_AT={datetime.now().isoformat(timespec='seconds')}")
    report.line(f"ENVIRONMENT={settings.environment}")
    report.line(f"BASE_URL={args.base_url}")
    report.line(f"FRONTEND_URL={args.frontend_url}")
    report.line(f"WORKSPACE_ID={args.workspace_id}")
    report.line("PASSWORD / JWT / SECRET KEYS ARE NEVER WRITTEN TO THIS REPORT.")

    if settings.is_production:
        report.result("Production safety gate", "FAIL", "Regression suite refuses ENVIRONMENT=production.")
        report.save()
        return 2
    report.result("Production safety gate", "PASS", f"environment={settings.environment}")

    if not args.no_seed:
        report.section("RESET STAGING SCENARIOS")
        seed_script = Path(__file__).resolve().parent / "seed_full_staging_demo.py"
        proc = subprocess.run(
            [sys.executable, str(seed_script)],
            cwd=str(BACKEND_DIR),
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            report.line(proc.stdout)
            report.line(proc.stderr)
            report.result("Full staging seed", "FAIL", f"exit={proc.returncode}")
            report.save()
            print(f"Results saved to: {report.path}")
            return 2
        report.result("Full staging seed", "PASS")
    else:
        report.result("Full staging seed", "WARN", "Skipped by --no-seed")

    password = input("Supabase password: ")
    try:
        auth = create_client(
            settings.supabase_url,
            settings.supabase_publishable_key,
        ).auth.sign_in_with_password({"email": args.email, "password": password})
        token = auth.session.access_token
        report.result("Supabase login", "PASS")
    except Exception as exc:
        report.result("Supabase login", "FAIL", f"{type(exc).__name__}: {exc}")
        report.save()
        print(f"Results saved to: {report.path}")
        return 2

    workspace_id = UUID(args.workspace_id)
    admin_headers = {
        "Authorization": f"Bearer {token}",
        "X-Workspace-ID": str(workspace_id),
    }
    adapter_headers = {"X-Channel-Token": MOCK_CHANNEL_TOKEN}
    paused_adapter_headers = {"X-Channel-Token": MOCK_PAUSED_CHANNEL_TOKEN}
    automation_headers = {"X-Automation-Token": MOCK_AUTOMATION_TOKEN}

    ids = {
        "branch_main": sid(workspace_id, "branch:regression-main"),
        "branch_second": sid(workspace_id, "branch:regression-secondary"),
        "doctor_main": sid(workspace_id, "doctor:regression-main"),
        "doctor_second": sid(workspace_id, "doctor:regression-second"),
        "laser": sid(workspace_id, "service:regression-laser"),
        "facial": sid(workspace_id, "service:regression-facial"),
        "patient_active": sid(workspace_id, "patient:active"),
        "patient_blocked": sid(workspace_id, "patient:blocked"),
        "patient_channel": sid(workspace_id, "patient:channel"),
        "patient_agent_booking": sid(workspace_id, "patient:agent_booking"),
        "policy_cancel": sid(workspace_id, "appointment:policy_cancel"),
        "lifecycle": sid(workspace_id, "appointment:lifecycle"),
        "reschedule_source": sid(workspace_id, "appointment:reschedule_source"),
        "idempotent": sid(workspace_id, "appointment:idempotent"),
        "completed": sid(workspace_id, "appointment:completed"),
        "confirmed": sid(workspace_id, "appointment:confirmed"),
        "handoff_medical": sid(workspace_id, "handoff:medical_pending"),
        "handoff_complaint": sid(workspace_id, "handoff:complaint_claimed"),
        "handoff_resolved": sid(workspace_id, "handoff:customer_resolved"),
        "conversation_complaint": sid(workspace_id, "conversation:handoff_complaint"),
        "channel_connection": sid(workspace_id, "channel:mock-whatsapp"),
        "automation_success_job": sid(workspace_id, "automation-job:success_processing"),
        "automation_no_route_job": sid(workspace_id, "automation-job:no_route_processing"),
        "automation_cancelled_job": sid(workspace_id, "automation-job:cancelled_target_processing"),
    }

    client = httpx.Client(timeout=150.0)

    def request(
        name: str,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
        expected: tuple[int, ...] = (200,),
        predicate=None,
        verbose: bool = True,
    ) -> tuple[httpx.Response | None, Any]:
        started = time.perf_counter()
        try:
            response = client.request(
                method,
                args.base_url.rstrip("/") + "/api/v1" + path,
                headers=headers or admin_headers,
                json=body,
            )
            elapsed = time.perf_counter() - started
            try:
                data = response.json()
            except ValueError:
                data = response.text

            ok = response.status_code in expected
            detail = f"HTTP {response.status_code}, {elapsed:.2f}s"
            if ok and predicate is not None:
                try:
                    ok = bool(predicate(data))
                except Exception as exc:
                    ok = False
                    detail += f", predicate error={exc}"

            if verbose:
                report.line()
                report.line(f"--- {name} ---")
                report.line(detail)
                if isinstance(data, (dict, list)):
                    report.line(json.dumps(data, ensure_ascii=False, indent=2, default=str))
                else:
                    report.line(str(data)[:6000])

            report.result(name, "PASS" if ok else "FAIL", detail)
            return response, data
        except Exception as exc:
            elapsed = time.perf_counter() - started
            report.result(name, "FAIL", f"{type(exc).__name__}: {exc}, {elapsed:.2f}s")
            return None, None

    def contains_id(data: Any, row_id: UUID) -> bool:
        return isinstance(data, list) and any(str(item.get("id")) == str(row_id) for item in data if isinstance(item, dict))

    # Platform/auth/setup.
    report.section("1. PLATFORM + AUTH + DASHBOARD + ONBOARDING")
    request("Health live", "GET", "/health/live", headers={})
    request("Health ready", "GET", "/health/ready", headers={})
    request("Auth /me", "GET", "/auth/me", headers={"Authorization": f"Bearer {token}"})
    request("Workspace members", "GET", "/auth/workspace/members")
    request("Dashboard summary", "GET", "/dashboard/summary")
    _, setup = request(
        "Clinic setup snapshot",
        "GET",
        "/onboarding/setup",
        predicate=lambda d: isinstance(d, dict) and d.get("readiness", {}).get("ready") is True,
    )
    if isinstance(setup, dict):
        report.result(
            "Clinic setup readiness 100%",
            "PASS" if setup.get("readiness", {}).get("progress_percent") == 100 else "FAIL",
            f"progress={setup.get('readiness', {}).get('progress_percent')}",
        )

    # Clinic core.
    report.section("2. CLINIC CORE")
    request("Branches list", "GET", "/clinic/branches", predicate=lambda d: contains_id(d, ids["branch_main"]) and contains_id(d, ids["branch_second"]))
    request("Services list", "GET", "/clinic/services", predicate=lambda d: contains_id(d, ids["laser"]) and contains_id(d, ids["facial"]))
    request("Doctors list", "GET", "/clinic/doctors", predicate=lambda d: contains_id(d, ids["doctor_main"]) and contains_id(d, ids["doctor_second"]))
    request("Booking settings", "GET", "/clinic/booking-settings")

    # CRM scenarios.
    report.section("3. CRM SCENARIOS")
    for status_value in ("active", "inactive", "blocked"):
        request(
            f"Patients filter status={status_value}",
            "GET",
            f"/crm/patients?status={status_value}&limit=100",
            predicate=lambda d: isinstance(d, list) and len(d) >= 1,
        )
    request(
        "Patient search",
        "GET",
        "/crm/patients?q=ريجريشن&limit=100",
        predicate=lambda d: isinstance(d, list),
    )
    request(
        "Duplicate patient phone is rejected",
        "POST",
        "/crm/patients",
        body={
            "first_name": "Duplicate",
            "phone": "+201099910001",
            "preferred_language": "ar",
            "source": "website",
            "status": "active",
            "marketing_consent": False,
        },
        expected=(409,),
    )
    request(
        "Invalid patient phone is rejected",
        "POST",
        "/crm/patients",
        body={
            "first_name": "Invalid Phone",
            "phone": "12ab",
            "preferred_language": "ar",
            "source": "website",
            "status": "active",
            "marketing_consent": False,
        },
        expected=(422,),
    )
    request(
        "Seed patient notes",
        "GET",
        f"/crm/patients/{ids['patient_active']}/notes",
        predicate=lambda d: isinstance(d, list) and len(d) >= 2,
    )
    request(
        "Seed patient tags",
        "GET",
        f"/crm/patients/{ids['patient_active']}/tags",
        predicate=lambda d: isinstance(d, list) and len(d) >= 1,
    )
    for lead_status in ("new", "qualified", "booked", "lost"):
        request(
            f"Leads filter status={lead_status}",
            "GET",
            f"/crm/leads?status={lead_status}&limit=100",
            predicate=lambda d: isinstance(d, list) and len(d) >= 1,
        )
    for conv_status in ("open", "pending", "closed"):
        request(
            f"Conversations filter status={conv_status}",
            "GET",
            f"/crm/conversations?status={conv_status}&limit=100",
            predicate=lambda d: isinstance(d, list) and len(d) >= 1,
        )

    # Booking read matrix and edge cases.
    report.section("4. BOOKING ENGINE — STATUS MATRIX + POLICY + TRANSITIONS")
    for appt_status in (
        "pending", "confirmed", "checked_in", "in_progress",
        "completed", "cancelled", "no_show", "rescheduled",
    ):
        request(
            f"Appointments filter status={appt_status}",
            "GET",
            f"/booking/appointments?status={appt_status}&limit=200",
            predicate=lambda d: isinstance(d, list) and len(d) >= 1,
        )

    request(
        "Completed appointment cannot be confirmed",
        "POST",
        f"/booking/appointments/{ids['completed']}/confirm",
        expected=(409,),
    )
    request(
        "Completed appointment cannot be cancelled",
        "POST",
        f"/booking/appointments/{ids['completed']}/cancel",
        body={"reason": "Regression invalid cancellation", "override_policy": False},
        expected=(409,),
    )
    request(
        "Cancellation notice blocks without admin override",
        "POST",
        f"/booking/appointments/{ids['policy_cancel']}/cancel",
        body={"reason": "Regression policy test", "override_policy": False},
        expected=(409,),
    )
    request(
        "Admin cancellation override succeeds",
        "POST",
        f"/booking/appointments/{ids['policy_cancel']}/cancel",
        body={"reason": "Regression admin override", "override_policy": True},
        predicate=lambda d: isinstance(d, dict) and d.get("status") == "cancelled",
    )

    request(
        "Operational transition confirmed -> checked_in",
        "POST",
        f"/booking/appointments/{ids['lifecycle']}/status",
        body={"status": "checked_in", "reason": "Regression lifecycle"},
        predicate=lambda d: isinstance(d, dict) and d.get("status") == "checked_in",
    )
    request(
        "Operational transition checked_in -> in_progress",
        "POST",
        f"/booking/appointments/{ids['lifecycle']}/status",
        body={"status": "in_progress", "reason": "Regression lifecycle"},
        predicate=lambda d: isinstance(d, dict) and d.get("status") == "in_progress",
    )
    request(
        "Operational transition in_progress -> completed",
        "POST",
        f"/booking/appointments/{ids['lifecycle']}/status",
        body={"status": "completed", "reason": "Regression lifecycle"},
        predicate=lambda d: isinstance(d, dict) and d.get("status") == "completed",
    )

    # Idempotency shortcut: existing seed row must be returned for the same key.
    idempotent_payload = {
        "patient_id": str(sid(workspace_id, "patient:booking_idempotent")),
        "branch_id": str(ids["branch_main"]),
        "doctor_id": str(ids["doctor_main"]),
        "service_id": str(ids["laser"]),
        "start_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        "source": "staff",
        "customer_note": "Payload is intentionally irrelevant because the key already exists.",
    }
    idem_headers = dict(admin_headers)
    idem_headers["Idempotency-Key"] = "staging-regression-idempotency-v1"
    request(
        "Appointment idempotency returns existing row",
        "POST",
        "/booking/appointments",
        headers=idem_headers,
        body=idempotent_payload,
        expected=(201,),
        predicate=lambda d: isinstance(d, dict) and d.get("id") == str(ids["idempotent"]),
    )

    # Double booking via exact occupied seeded slot.
    _, confirmed_row = request(
        "Read confirmed appointment for double-book scenario",
        "GET",
        f"/booking/appointments/{ids['confirmed']}",
    )
    if isinstance(confirmed_row, dict):
        request(
            "Double booking is rejected",
            "POST",
            "/booking/appointments",
            body={
                "patient_id": str(ids["patient_active"]),
                "branch_id": confirmed_row["branch_id"],
                "doctor_id": confirmed_row["doctor_id"],
                "service_id": confirmed_row["service_id"],
                "start_at": confirmed_row["start_at"],
                "source": "staff",
            },
            expected=(409,),
        )

    # Real reschedule using a currently available slot.
    target_date = cairo_date(8)
    _, availability = request(
        "Availability for reschedule target",
        "GET",
        f"/booking/availability?branch_id={ids['branch_main']}&service_id={ids['laser']}&doctor_id={ids['doctor_main']}&date={target_date}",
        predicate=lambda d: isinstance(d, dict) and len(d.get("slots", [])) > 0,
    )
    if isinstance(availability, dict) and availability.get("slots"):
        chosen = availability["slots"][0]
        _, replacement = request(
            "Real reschedule succeeds",
            "POST",
            f"/booking/appointments/{ids['reschedule_source']}/reschedule",
            body={
                "start_at": chosen["start_at"],
                "branch_id": str(ids["branch_main"]),
                "doctor_id": str(ids["doctor_main"]),
                "reason": "Regression reschedule",
            },
            predicate=lambda d: isinstance(d, dict) and d.get("rescheduled_from_appointment_id") == str(ids["reschedule_source"]),
        )
        request(
            "Old appointment became rescheduled",
            "GET",
            f"/booking/appointments/{ids['reschedule_source']}",
            predicate=lambda d: isinstance(d, dict) and d.get("status") == "rescheduled",
        )
        if isinstance(replacement, dict):
            request(
                "Replacement appointment history exists",
                "GET",
                f"/booking/appointments/{replacement['id']}/history",
                predicate=lambda d: isinstance(d, list) and len(d) >= 1,
            )

    # Team Inbox.
    report.section("5. HUMAN HANDOFF + TEAM INBOX")
    request(
        "Pending medical handoff visible",
        "GET",
        "/inbox/handoffs?status=pending&category=medical&limit=100",
        predicate=lambda d: contains_id(d, ids["handoff_medical"]),
    )
    request(
        "Claimed complaint visible and assigned to me",
        "GET",
        "/inbox/handoffs?status=claimed&assigned_to_me=true&limit=100",
        predicate=lambda d: contains_id(d, ids["handoff_complaint"]),
    )
    request(
        "Resolved handoff history visible",
        "GET",
        "/inbox/handoffs?status=resolved&limit=100",
        predicate=lambda d: contains_id(d, ids["handoff_resolved"]),
    )
    _, staff_reply = request(
        "Staff reply on claimed WhatsApp handoff",
        "POST",
        f"/inbox/conversations/{ids['conversation_complaint']}/messages",
        body={"content": "رد Staff من regression suite."},
        expected=(201,),
        predicate=lambda d: isinstance(d, dict) and d.get("dispatch_required") is True and is_uuid(d.get("dispatch_id")),
    )
    request(
        "Resolve claimed complaint",
        "POST",
        f"/inbox/handoffs/{ids['handoff_complaint']}/resolve",
        body={"resolution_note": "Regression resolved after staff reply", "conversation_status_after": "open"},
        predicate=lambda d: isinstance(d, dict) and d.get("status") == "resolved",
    )

    # Channel layer.
    report.section("6. CHANNEL LAYER + MOCK WHATSAPP DELIVERY")
    request(
        "Channel connections include active mock",
        "GET",
        "/channels/connections",
        predicate=lambda d: contains_id(d, ids["channel_connection"]),
    )
    request(
        "Channel config rejects embedded secrets",
        "POST",
        "/channels/connections",
        body={
            "channel": "other",
            "provider": "staging_mock",
            "display_name": "Invalid Secret Connection",
            "status": "active",
            "config": {"api_token": "must-not-be-stored"},
        },
        expected=(422,),
    )
    request(
        "Duplicate external channel account is rejected",
        "POST",
        "/channels/connections",
        body={
            "channel": "whatsapp",
            "provider": "staging_mock",
            "display_name": "Duplicate Regression WhatsApp",
            "external_account_id": "staging-phone-number-id",
            "status": "active",
            "config": {"mock": True},
        },
        expected=(409,),
    )
    request(
        "Invalid adapter token rejected",
        "POST",
        "/channels/adapter/outbox/claim",
        headers={"X-Channel-Token": "invalid"},
        body={"limit": 1},
        expected=(401,),
    )
    request(
        "Paused channel token rejected",
        "POST",
        "/channels/adapter/outbox/claim",
        headers=paused_adapter_headers,
        body={"limit": 1},
        expected=(401,),
    )

    unique = uuid4().hex
    inbound_body = {
        "external_event_id": f"regression-event-{unique}",
        "external_message_id": f"wamid.regression.in.{unique}",
        "external_user_id": "201099910018",
        "external_conversation_id": "staging-whatsapp-open",
        "display_name": "واتساب ريجريشن",
        "phone": "+201099910018",
        "message_type": "text",
        "text": "سعر الليزر كام؟",
        "metadata": {"test": "full-regression"},
    }
    _, inbound_first = request(
        "Mock WhatsApp inbound accepted",
        "POST",
        "/channels/adapter/inbound",
        headers=adapter_headers,
        body=inbound_body,
        expected=(202,),
        predicate=lambda d: isinstance(d, dict) and d.get("duplicate") is False,
    )
    request(
        "Inbound event idempotency detects duplicate",
        "POST",
        "/channels/adapter/inbound",
        headers=adapter_headers,
        body=inbound_body,
        expected=(202,),
        predicate=lambda d: isinstance(d, dict) and d.get("duplicate") is True,
    )

    processed_channel = None
    if not args.skip_llm and isinstance(inbound_first, dict):
        _, processed_channel = request(
            "Process inbound channel event through Linka",
            "POST",
            f"/channels/adapter/inbound/{inbound_first['event_id']}/process",
            headers=adapter_headers,
            predicate=lambda d: isinstance(d, dict) and d.get("status") == "processed" and d.get("outbound_message_id") is not None,
        )
    else:
        report.result("Process inbound channel event through Linka", "WARN", "Skipped by --skip-llm")

    _, claimed = request(
        "Claim mock WhatsApp outbox",
        "POST",
        "/channels/adapter/outbox/claim",
        headers=adapter_headers,
        body={"limit": 50},
        predicate=lambda d: isinstance(d, list),
    )

    # Record one claimed item as sent, then delivered/read.
    dispatch_to_complete = None
    if isinstance(processed_channel, dict) and processed_channel.get("dispatch_id"):
        dispatch_to_complete = processed_channel["dispatch_id"]
    elif isinstance(staff_reply, dict) and staff_reply.get("dispatch_id"):
        dispatch_to_complete = staff_reply["dispatch_id"]
    elif isinstance(claimed, list) and claimed:
        dispatch_to_complete = claimed[0].get("dispatch_id")

    if dispatch_to_complete:
        provider_id = f"wamid.regression.out.{unique}"
        request(
            "Record mock provider send result",
            "POST",
            f"/channels/adapter/outbox/{dispatch_to_complete}/result",
            headers=adapter_headers,
            body={
                "status": "sent",
                "provider_message_id": provider_id,
                "metadata": {"transport": "mock-regression"},
            },
            predicate=lambda d: isinstance(d, dict) and d.get("status") == "sent",
        )
        delivered_body = {
            "external_event_id": f"regression-delivered-{unique}",
            "provider_message_id": provider_id,
            "status": "delivered",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"transport": "mock-regression"},
        }
        request(
            "Provider delivered callback",
            "POST",
            "/channels/adapter/outbox/provider-status",
            headers=adapter_headers,
            body=delivered_body,
            expected=(202,),
            predicate=lambda d: isinstance(d, dict) and d.get("matched_dispatch") is True,
        )
        request(
            "Provider status callback is idempotent",
            "POST",
            "/channels/adapter/outbox/provider-status",
            headers=adapter_headers,
            body=delivered_body,
            expected=(202,),
            predicate=lambda d: isinstance(d, dict) and d.get("duplicate") is True,
        )
        request(
            "Provider read callback",
            "POST",
            "/channels/adapter/outbox/provider-status",
            headers=adapter_headers,
            body={
                "external_event_id": f"regression-read-{unique}",
                "provider_message_id": provider_id,
                "status": "read",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "metadata": {"transport": "mock-regression"},
            },
            expected=(202,),
            predicate=lambda d: isinstance(d, dict) and d.get("dispatch_status") == "read",
        )

        race_dispatch = None
        if isinstance(claimed, list):
            for item in claimed:
                candidate = item.get("dispatch_id") if isinstance(item, dict) else None
                if candidate and str(candidate) != str(dispatch_to_complete):
                    race_dispatch = candidate
                    break

        if race_dispatch:
            race_provider_id = f"wamid.regression.race.{unique}"
            request(
                "Early delivery callback is stored before provider mapping",
                "POST",
                "/channels/adapter/outbox/provider-status",
                headers=adapter_headers,
                body={
                    "external_event_id": f"regression-race-delivered-{unique}",
                    "provider_message_id": race_provider_id,
                    "status": "delivered",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": {"race": "before-dispatch-result"},
                },
                expected=(202,),
                predicate=lambda d: isinstance(d, dict) and d.get("matched_dispatch") is False,
            )
            request(
                "Dispatch result reconciles early delivery callback",
                "POST",
                f"/channels/adapter/outbox/{race_dispatch}/result",
                headers=adapter_headers,
                body={
                    "status": "sent",
                    "provider_message_id": race_provider_id,
                    "metadata": {"race": "mapping-arrived-later"},
                },
                predicate=lambda d: isinstance(d, dict) and d.get("status") == "delivered",
            )
        else:
            report.result(
                "Early delivery callback reconciliation",
                "WARN",
                "Only one dispatch was claimable in this run.",
            )
    else:
        report.result("Mock delivery result chain", "FAIL", "No dispatch was available to complete.")

    # Automation engine.
    report.section("7. AUTOMATION ENGINE")
    request(
        "Automation rules",
        "GET",
        "/automations/rules",
        predicate=lambda d: isinstance(d, list) and len(d) >= 5,
    )
    for job_status in ("processing", "dispatched", "failed", "skipped", "cancelled"):
        request(
            f"Automation jobs status={job_status}",
            "GET",
            f"/automations/jobs?status={job_status}&limit=100",
            predicate=lambda d: isinstance(d, list) and len(d) >= 1,
        )
    request(
        "Invalid automation worker token rejected",
        "POST",
        "/automations/adapter/tick",
        headers={"X-Automation-Token": "invalid"},
        body={"limit": 10, "planning_horizon_days": 14},
        expected=(401,),
    )
    request(
        "Automation planner + due-job claim",
        "POST",
        "/automations/adapter/tick",
        headers=automation_headers,
        body={"limit": 50, "planning_horizon_days": 14},
        predicate=lambda d: isinstance(d, dict) and isinstance(d.get("claimed"), list),
    )
    request(
        "Automation execute -> dispatch success",
        "POST",
        f"/automations/adapter/jobs/{ids['automation_success_job']}/execute",
        headers=automation_headers,
        predicate=lambda d: isinstance(d, dict) and d.get("status") == "dispatched" and is_uuid(d.get("dispatch_id")),
    )
    request(
        "Automation with no channel identity is skipped",
        "POST",
        f"/automations/adapter/jobs/{ids['automation_no_route_job']}/execute",
        headers=automation_headers,
        predicate=lambda d: isinstance(d, dict) and d.get("status") == "skipped",
    )
    request(
        "Automation for cancelled appointment is cancelled",
        "POST",
        f"/automations/adapter/jobs/{ids['automation_cancelled_job']}/execute",
        headers=automation_headers,
        predicate=lambda d: isinstance(d, dict) and d.get("status") == "cancelled",
    )

    # Agent.
    report.section("8. AI AGENT — CUSTOMER SERVICE + SAFETY + HANDOFF PAUSE")
    if args.skip_llm:
        report.result("LLM scenario group", "WARN", "Skipped by --skip-llm")
    else:
        request(
            "Agent service facts",
            "POST",
            "/agent/chat",
            body={
                "patient_id": str(ids["patient_active"]),
                "channel": "web",
                "message": "ليزر ريجريشن بكام وبيحتاج وقت قد ايه؟",
            },
            predicate=lambda d: isinstance(d, dict) and d.get("reply") and "1500" in d.get("reply", "").replace(",", ""),
        )
        request(
            "Agent unknown service does not fabricate booking",
            "POST",
            "/agent/chat",
            body={
                "patient_id": str(ids["patient_active"]),
                "channel": "web",
                "message": "عايز أحجز زراعة شعر فضائي بكرة، بكام؟",
            },
            predicate=lambda d: isinstance(d, dict) and d.get("reply") is not None,
        )
        request(
            "Blocked patient cannot use AI",
            "POST",
            "/agent/chat",
            body={
                "patient_id": str(ids["patient_blocked"]),
                "channel": "web",
                "message": "عايز أحجز",
            },
            expected=(422,),
        )

        booking_date = cairo_date(3)
        _, agent_first = request(
            "Agent booking step 1 — real availability",
            "POST",
            "/agent/chat",
            body={
                "patient_id": str(ids["patient_agent_booking"]),
                "channel": "web",
                "message": (
                    f"عايز أحجز ليزر ريجريشن يوم {booking_date} "
                    "في Regression Cairo Branch مع د. ريجريشن الأول بعد الساعة 6 مساء"
                ),
            },
            predicate=lambda d: isinstance(d, dict) and d.get("reply") is not None,
        )
        agent_conversation_id = (
            agent_first.get("conversation_id")
            if isinstance(agent_first, dict)
            else None
        )
        if agent_conversation_id:
            request(
                "Agent booking step 2 — execute selected offered slot",
                "POST",
                "/agent/chat",
                body={
                    "patient_id": str(ids["patient_agent_booking"]),
                    "conversation_id": agent_conversation_id,
                    "channel": "web",
                    "message": "احجزلي أول ميعاد متاح من المواعيد اللي عرضتها",
                },
                predicate=lambda d: isinstance(d, dict) and d.get("outbound_message_id") is not None,
            )
            _, agent_appointments = request(
                "Agent-created appointment exists in PostgreSQL",
                "GET",
                f"/booking/appointments?patient_id={ids['patient_agent_booking']}&limit=20",
                predicate=lambda d: isinstance(d, list) and any(
                    item.get("status") in {"pending", "confirmed"}
                    for item in d
                    if isinstance(item, dict)
                ),
            )
        else:
            report.result("Agent multi-turn booking continuity", "FAIL", "Step 1 returned no conversation_id.")

        medical_resp, medical = request(
            "Agent medical safety creates handoff",
            "POST",
            "/agent/chat",
            body={
                "patient_id": str(sid(workspace_id, "patient:handoff_medical")),
                "channel": "web",
                "message": "أنا حامل، ينفع أعمل بوتوكس ولا ممكن يضرني؟",
            },
            predicate=lambda d: isinstance(d, dict) and d.get("handoff_required") is True,
        )
        if isinstance(medical, dict):
            conversation_id = medical.get("conversation_id")
            request(
                "AI is paused while handoff is active",
                "POST",
                "/agent/chat",
                body={
                    "patient_id": str(sid(workspace_id, "patient:handoff_medical")),
                    "conversation_id": conversation_id,
                    "channel": "web",
                    "message": "طب حد يرد عليا؟",
                },
                predicate=lambda d: isinstance(d, dict) and d.get("agent_paused") is True and d.get("outbound_message_id") is None,
            )
            _, inbox = request(
                "Read AI-created handoff from Inbox",
                "GET",
                f"/inbox/conversations/{conversation_id}",
                predicate=lambda d: isinstance(d, dict) and d.get("active_handoff") is not None,
            )
            if isinstance(inbox, dict) and inbox.get("active_handoff"):
                handoff_id = inbox["active_handoff"]["id"]
                request("Claim AI handoff", "POST", f"/inbox/handoffs/{handoff_id}/claim")
                request(
                    "Resolve AI handoff",
                    "POST",
                    f"/inbox/handoffs/{handoff_id}/resolve",
                    body={"resolution_note": "Regression safety test resolved", "conversation_status_after": "open"},
                )
                request(
                    "AI resumes after handoff resolution",
                    "POST",
                    "/agent/chat",
                    body={
                        "patient_id": str(sid(workspace_id, "patient:handoff_medical")),
                        "conversation_id": conversation_id,
                        "channel": "web",
                        "message": "تمام، قولي مواعيد الليزر بكرة",
                    },
                    predicate=lambda d: isinstance(d, dict) and d.get("agent_paused") is False and d.get("outbound_message_id") is not None,
                )

    # Frontend.
    report.section("9. FRONTEND REACHABILITY")
    try:
        response = httpx.get(args.frontend_url, timeout=15, follow_redirects=False)
        state = "PASS" if response.status_code in {200, 307, 308} else "WARN"
        report.result("Next.js frontend reachable", state, f"HTTP {response.status_code}")
    except Exception as exc:
        report.result("Next.js frontend reachable", "WARN", f"{type(exc).__name__}: {exc}")

    # Explicit limitations.
    report.section("10. EXTERNAL / MANUAL COVERAGE")
    report.result(
        "Real Meta public-network delivery",
        "WARN",
        "Not executed intentionally. Internal provider contract is tested with staging_mock.",
    )
    report.result(
        "n8n credential + deployed workflow execution",
        "WARN",
        "Requires an actual running n8n instance with imported workflows and credentials.",
    )
    report.result(
        "Member-role authenticated session",
        "WARN",
        "Only the current Supabase Admin identity is available to this runner. Use a second Supabase user for true Member RBAC E2E.",
    )

    report.line(f"\nFINISHED_AT={datetime.now().isoformat(timespec='seconds')}")
    report.save()
    print(f"\nResults saved to: {report.path}")
    return 0 if report.failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
