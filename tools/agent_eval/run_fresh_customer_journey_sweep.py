from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models.working_hours import DoctorWorkingHour
from app.models.workspace import Workspace
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import assert_demo_only, state_snapshot

# Load only definitions from the historical eval runner. That file predates a main
# guard and otherwise executes its own scenarios on import. This keeps the fresh
# sweep evaluation-only without modifying the historical runner.
_legacy_path = Path(__file__).with_name("run_fresh_task_lifecycle.py")
_legacy_source = _legacy_path.read_text(encoding="utf-8")
_legacy_definitions = _legacy_source.split("\noutput = Path(sys.argv[1])", 1)[0]
_legacy_namespace = {"__name__": "tools.agent_eval._fresh_sweep_helpers", "__file__": str(_legacy_path)}
exec(compile(_legacy_definitions, str(_legacy_path), "exec"), _legacy_namespace)  # noqa: S102 -- trusted repo eval source
Conversation = _legacy_namespace["Conversation"]
_inject_automation = _legacy_namespace["_inject_automation"]
_seed_appointment = _legacy_namespace["_seed_appointment"]
patient = _legacy_namespace["patient"]
prepare = _legacy_namespace["prepare"]

TZ = ZoneInfo("Africa/Cairo")
START = datetime(2026, 10, 5, 10, 0, tzinfo=TZ)


def snapshot_delta(before: dict, after: dict) -> dict:
    before_rows = {row["id"]: row for row in before.get("appointments", [])}
    after_rows = {row["id"]: row for row in after.get("appointments", [])}
    return {
        "created_appointments": [row for key, row in after_rows.items() if key not in before_rows],
        "changed_appointments": [
            {"before": before_rows[key], "after": row}
            for key, row in after_rows.items()
            if key in before_rows and before_rows[key] != row
        ],
    }


def finish(result_id: str, c: Conversation, before: dict) -> dict:
    after = state_snapshot(c.db, c.ws, c.p)
    return {
        "id": result_id,
        "turns": c.turns,
        "errors": c.errors,
        "before": before,
        "after": after,
        "db_delta": snapshot_delta(before, after),
        "writes": sum(bool(turn.get("write_attempted")) for turn in c.turns),
    }


def e1(db, ws):
    p = patient(db, ws, "E1Fresh")
    before = state_snapshot(db, ws, p)
    c = Conversation("E1Fresh", db, ws, p, START)
    for msg in (
        "محتاج احجز ليزر للابط",
        "خليه ديكا",
        "ينفع الاربع؟",
        "لا معلش الخميس احسن",
        "طب 5 ونص مساء",
        "ايوه احجزه",
    ):
        if c.turns and "booking_completed" in c.turns[-1]["goals"]:
            break
        c.send(msg)
    if c.turns and "booking_completed" in c.turns[-1]["goals"]:
        c.send("تسلم يا باشا")
    return finish("E1", c, before)


def e2(db, ws):
    p = patient(db, ws, "E2Fresh")
    before = state_snapshot(db, ws, p)
    c = Conversation("E2Fresh", db, ws, p, START)
    c.send("ممكن Full Body كانديلا الخميس بعد 7 بالليل؟")
    return finish("E2", c, before)


def e3(db, ws):
    p = patient(db, ws, "E3Fresh")
    before = state_snapshot(db, ws, p)
    c = Conversation("E3Fresh", db, ws, p, START)
    for msg in (
        "عايز جلسة ليزر إبط",
        "ديكا",
        "الاربع",
        "استنى خلي الجهاز كانديلا",
        "وكمان بدل الاربع خليها الخميس",
        "الساعة 7 مساء",
        "تمام احجز",
    ):
        if c.turns and "booking_completed" in c.turns[-1]["goals"]:
            break
        c.send(msg)
    return finish("E3", c, before)


def e4(db, ws):
    p = patient(db, ws, "E4Fresh")
    before = state_snapshot(db, ws, p)
    c = Conversation("E4Fresh", db, ws, p, START)
    for msg in (
        "عايزة احجز Full Body",
        "كانديلا",
        "على فكرة عنوانكم فين؟",
        "وبتفتحوا من الساعة كام؟",
        "تمام نكمل، الخميس",
    ):
        c.send(msg)
    return finish("E4", c, before)


def e5(db, ws):
    p = patient(db, ws, "E5Fresh")
    before = state_snapshot(db, ws, p)
    c = Conversation("E5Fresh", db, ws, p, START)
    c.send("ممكن احجز إبط؟")
    c.send("ديكا مناسب")
    task_before_gap = c.turns[-1]["active_task"]
    c.set_now(START + timedelta(days=3))
    c.send("طيب الخميس بليل فيه حاجه؟")
    result = finish("E5", c, before)
    result["task_before_gap"] = task_before_gap
    result["gap_days"] = 3
    return result


def e6(db, ws):
    p = patient(db, ws, "E6Fresh")
    before = state_snapshot(db, ws, p)
    c = Conversation("E6Fresh", db, ws, p, START)
    c.send("عايز احجز إبط")
    c.send("ديكا")
    old_task = c.turns[-1]["active_task"]
    c.set_now(START + timedelta(days=12))
    c.send("المرة دي عايز احجز Full Body")
    result = finish("E6", c, before)
    result["old_task"] = old_task
    result["gap_days"] = 12
    return result


def e7(db, ws, under, youssef, branch_id):
    # Direct reminder-linked reschedule control.
    p = patient(db, ws, "E7Fresh")
    before = state_snapshot(db, ws, p)
    appointment = _seed_appointment(
        db,
        ws,
        p,
        under,
        youssef,
        branch_id,
        day=datetime(2026, 10, 8, tzinfo=TZ).date(),
        now=START,
    )
    c = Conversation("E7Fresh", db, ws, p, START)
    c.send("هو الحجز اللي عندي الخميس الساعة كام؟")
    automation = _inject_automation(
        db,
        ws,
        c,
        appointment,
        rule_key="appointment_reminder_6h",
        content="تذكير بموعدك القادم في العيادة.",
    )
    c.send("ممكن نخليه الساعة 7 بالليل بدل كده؟")
    result = finish("E7", c, before)
    result["target_appointment_id"] = str(appointment.id)
    result["automation_context"] = automation["context_preview"]["automation_context"]

    # Separate acknowledgement control so consuming the reminder focus here does not
    # change the direct reschedule scenario above.
    p_ack = patient(db, ws, "E7AckFresh")
    ack_before = state_snapshot(db, ws, p_ack)
    ack_appointment = _seed_appointment(
        db,
        ws,
        p_ack,
        under,
        youssef,
        branch_id,
        day=datetime(2026, 10, 8, tzinfo=TZ).date(),
        now=START,
    )
    c_ack = Conversation("E7AckFresh", db, ws, p_ack, START)
    c_ack.send("ميعادي الخميس لسه ثابت؟")
    ack_automation = _inject_automation(
        db,
        ws,
        c_ack,
        ack_appointment,
        rule_key="appointment_reminder_6h",
        content="تذكير بموعدك القادم في العيادة.",
    )
    c_ack.send("تمام")
    result["ack_control"] = finish("E7_ACK", c_ack, ack_before)
    result["ack_automation_context"] = ack_automation["context_preview"]["automation_context"]
    return result


def e8(db, ws, branch_id):
    # Evaluation fixture only: close Monday in the outer rollback transaction so the
    # first date is deterministically unavailable without persisting any business data.
    monday = START.date().weekday()
    db.execute(
        delete(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == ws.id,
            DoctorWorkingHour.branch_id == branch_id,
            DoctorWorkingHour.weekday == monday,
        )
    )
    db.flush()
    p = patient(db, ws, "E8Fresh")
    before = state_snapshot(db, ws, p)
    c = Conversation("E8Fresh", db, ws, p, START)
    c.send("محتاج احجز ليزر إبط")
    c.send("ديكا")
    c.send("النهارده")
    try:
        c.send("طب اقرب حاجه بعده امتى؟")
    except RuntimeError as exc:
        c.errors.append(f"{type(exc).__name__}: {exc}")
        c.turns.append({
            "customer": "طب اقرب حاجه بعده امتى؟",
            "linka": None,
            "goals": [],
            "operations": [],
            "verified_reads": [],
            "actions": [],
            "write_attempted": False,
            "write_result": None,
            "active_task": c.turns[-1].get("active_task") if c.turns else None,
            "error": c.errors[-1],
        })
    return finish("E8", c, before)


def run_all(db, ws):
    under, _full, _mary, youssef, branch_id = prepare(db, ws)
    return [
        e1(db, ws),
        e2(db, ws),
        e3(db, ws),
        e4(db, ws),
        e5(db, ws),
        e6(db, ws),
        e7(db, ws, under, youssef, branch_id),
        e8(db, ws, branch_id),
    ]


def main() -> None:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "fresh_customer_journey_sweep.json")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    conn = engine.connect()
    outer = conn.begin()
    db = Session(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        ws = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        assert_demo_only(ws)
        mode = sys.argv[2] if len(sys.argv) > 2 else "all"
        if mode == "e7":
            under, _full, _mary, youssef, branch_id = prepare(db, ws)
            results = [e7(db, ws, under, youssef, branch_id)]
        elif mode == "e2":
            prepare(db, ws)
            results = [e2(db, ws)]
        else:
            results = run_all(db, ws)
        payload = {
            "starting_main_sha": "d13c4b8724592de5f5c6bd8faa17ea0a1d99ad64",
            "evaluation_only": True,
            "rollback_transaction": True,
            "results": results,
        }
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print("RESULTS", [(r["id"], len(r["turns"]), r["writes"], r["errors"]) for r in results])
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        conn.close()
        engine.dispose()
        print("OUTER_ROLLBACK=CONFIRMED")


if __name__ == "__main__":
    main()
