from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from supabase import create_client

from app.core.config import settings

DEFAULT_EMAIL = "adam1ezzat1@gmail.com"
DEFAULT_WORKSPACE_ID = "cd044141-edf4-43fc-8783-a9e7e147010b"
DEFAULT_PATIENT_ID = "6b7f8957-4790-47de-9498-05375c26a2b8"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class Report:
    def __init__(self):
        stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path=Path.cwd()/f"tia_recent_milestones_regression_{stamp}.txt"
        self.lines=[]
        self.pass_count=0; self.warn_count=0; self.fail_count=0

    def line(self, text=""):
        print(text); self.lines.append(text)

    def result(self, name, state, detail=""):
        if state=="PASS": self.pass_count+=1
        elif state=="WARN": self.warn_count+=1
        else: self.fail_count+=1
        self.line(f"[{state}] {name}" + (f" — {detail}" if detail else ""))

    def save(self):
        self.line()
        self.line(f"SUMMARY PASS={self.pass_count} WARN={self.warn_count} FAIL={self.fail_count}")
        self.path.write_text("\n".join(self.lines)+"\n",encoding="utf-8")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--email",default=DEFAULT_EMAIL)
    ap.add_argument("--workspace-id",default=DEFAULT_WORKSPACE_ID)
    ap.add_argument("--patient-id",default=DEFAULT_PATIENT_ID)
    ap.add_argument("--base-url",default=DEFAULT_BASE_URL)
    ap.add_argument("--frontend-url",default="http://127.0.0.1:3000")
    args=ap.parse_args()

    report=Report()
    report.line("Linka — RECENT MILESTONES REGRESSION")
    report.line(f"STARTED_AT={datetime.now().isoformat(timespec='seconds')}")
    report.line(f"WORKSPACE_ID={args.workspace_id}")
    report.line(f"PATIENT_ID={args.patient_id}")
    report.line("PASSWORD/TOKEN ARE NEVER SAVED.")
    password=input("Supabase password: ")

    try:
        auth=create_client(settings.supabase_url,settings.supabase_publishable_key).auth.sign_in_with_password({"email":args.email,"password":password})
        token=auth.session.access_token
        report.result("Supabase login","PASS")
    except Exception as exc:
        report.result("Supabase login","FAIL",f"{type(exc).__name__}: {exc}")
        report.save(); print("Results:",report.path); return 1

    headers={"Authorization":f"Bearer {token}","X-Workspace-ID":args.workspace_id}
    client=httpx.Client(timeout=120)

    def call(name,method,path,expected=(200,),body=None,workspace=True):
        h=headers if workspace else {"Authorization":f"Bearer {token}"}
        started=time.perf_counter()
        try:
            r=client.request(method,args.base_url.rstrip("/")+"/api/v1"+path,headers=h,json=body)
            elapsed=time.perf_counter()-started
            try: data=r.json()
            except Exception: data=r.text
            report.line(f"\n--- {name} ---\nSTATUS={r.status_code} TIME={elapsed:.2f}s\n{json.dumps(data,ensure_ascii=False,indent=2,default=str) if isinstance(data,(dict,list)) else data}")
            state="PASS" if r.status_code in expected else "FAIL"
            report.result(name,state,f"HTTP {r.status_code}, {elapsed:.2f}s")
            return r,data
        except Exception as exc:
            report.result(name,"FAIL",f"{type(exc).__name__}: {exc}")
            return None,None

    call("Auth /me","GET","/auth/me",workspace=False)
    call("Workspace members","GET","/auth/workspace/members")
    call("Dashboard summary","GET","/dashboard/summary")
    _,setup=call("Onboarding + clinic setup snapshot","GET","/onboarding/setup")
    if isinstance(setup,dict):
        readiness=setup.get("readiness",{})
        report.result("Clinic readiness","PASS" if readiness.get("ready") else "WARN",f"{readiness.get('progress_percent','?')}% ready; missing={readiness.get('missing',[])}")

    call("Clinic branches","GET","/clinic/branches")
    call("Clinic services","GET","/clinic/services")
    call("Clinic doctors","GET","/clinic/doctors")
    call("Booking settings","GET","/clinic/booking-settings",expected=(200,404))
    call("CRM patients","GET","/crm/patients?limit=20")
    call("Appointments","GET","/booking/appointments?limit=20")
    call("Team Inbox queue","GET","/inbox/handoffs?limit=20")
    _,channels=call("Channel connections","GET","/channels/connections")
    if isinstance(channels,list) and not channels:
        report.result("External channel configured","WARN","No channel connection yet; WhatsApp transport cannot be live-tested.")
    call("Automation rules","GET","/automations/rules")
    call("Automation jobs","GET","/automations/jobs?limit=20")

    # Agent read behavior.
    call("Agent service facts","POST","/agent/chat",body={
        "patient_id":args.patient_id,"channel":"web","message":"جلسة الليزر بكام وبتاخد وقت قد ايه؟"
    })
    call("Agent availability filter","POST","/agent/chat",body={
        "patient_id":args.patient_id,"channel":"web","message":"عايزة مواعيد ليزر بكرة من الساعة 8 مساء لحد 9 مساء"
    })

    # Real medical handoff, then resolve it to leave the demo patient clean.
    _,handoff_chat=call("Agent medical handoff","POST","/agent/chat",body={
        "patient_id":args.patient_id,"channel":"web","message":"انا حامل، ينفع اعمل ليزر ولا ممكن يضرني؟"
    })
    if isinstance(handoff_chat,dict):
        if handoff_chat.get("handoff_required") is True:
            report.result("Medical handoff flag","PASS")
            conv=handoff_chat.get("conversation_id")
            if conv:
                _,conv_data=call("Inbox conversation after handoff","GET",f"/inbox/conversations/{conv}")
                if isinstance(conv_data,dict) and conv_data.get("active_handoff"):
                    hid=conv_data["active_handoff"]["id"]
                    call("Claim handoff","POST",f"/inbox/handoffs/{hid}/claim")
                    call("Resolve handoff","POST",f"/inbox/handoffs/{hid}/resolve",body={
                        "resolution_note":"Regression test cleanup","conversation_status_after":"open"
                    })
        else:
            report.result("Medical handoff flag","FAIL","handoff_required was not true")

    # Frontend reachability only; authentication UI itself is browser-session based.
    try:
        r=httpx.get(args.frontend_url,timeout=15,follow_redirects=False)
        report.result("Frontend server reachable","PASS" if r.status_code in (200,307,308) else "WARN",f"HTTP {r.status_code}")
    except Exception as exc:
        report.result("Frontend server reachable","WARN",f"{type(exc).__name__}: {exc}")

    report.line(f"\nFINISHED_AT={datetime.now().isoformat(timespec='seconds')}")
    report.save()
    print(f"\nResults saved to: {report.path}")
    return 0 if report.fail_count==0 else 2


if __name__=="__main__":
    raise SystemExit(main())
