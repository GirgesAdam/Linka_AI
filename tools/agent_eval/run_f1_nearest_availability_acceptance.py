from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.agents.v2.customer_datetime import format_customer_date
from app.agents.v2.turn_contract import TurnEntities, TurnOperation
from app.core.config import settings
from app.models.working_hours import DoctorWorkingHour
from app.models.workspace import Workspace
from app.services.agent_v2 import orchestrator as runtime
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import assert_demo_only, state_snapshot

# Load definitions only from the historical runner. It predates a main guard.
_legacy_path = Path(__file__).with_name("run_fresh_task_lifecycle.py")
_legacy_source = _legacy_path.read_text(encoding="utf-8")
_legacy_definitions = _legacy_source.split("\noutput = Path(sys.argv[1])", 1)[0]
_legacy_namespace = {
    "__name__": "tools.agent_eval._f1_acceptance_helpers",
    "__file__": str(_legacy_path),
}
exec(compile(_legacy_definitions, str(_legacy_path), "exec"), _legacy_namespace)  # noqa: S102

send_turn = _legacy_namespace["send_turn"]
load_active_task = _legacy_namespace["load_active_task"]
live_chat_module = _legacy_namespace["live_chat_module"]
patient = _legacy_namespace["patient"]
prepare = _legacy_namespace["prepare"]

TZ = ZoneInfo("Africa/Cairo")
START = datetime(2026, 10, 5, 10, 0, tzinfo=TZ)
BOUNDARY_UTC = datetime(2026, 10, 4, 22, 30, tzinfo=UTC)
BOUNDARY_LOCAL = BOUNDARY_UTC.astimezone(TZ)


def _operation_rows(cap) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for trace in cap.structured_trace:
        if not isinstance(trace, dict):
            continue
        understanding = trace.get("understanding") or {}
        for op in understanding.get("operations") or []:
            if not isinstance(op, dict):
                continue
            entities = op.get("entities") if isinstance(op.get("entities"), dict) else {}
            date_value = entities.get("date") if isinstance(entities.get("date"), dict) else None
            rows.append(
                {
                    "type": op.get("type"),
                    "continues_previous": op.get("continues_previous"),
                    "continuation_condition": op.get("continuation_condition"),
                    "date_mode": date_value.get("mode") if date_value else None,
                    "date": date_value,
                    "time": entities.get("time"),
                }
            )
    return rows


def _plan_rows(cap) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for trace in cap.structured_trace:
        if not isinstance(trace, dict):
            continue
        plan = trace.get("plan") or {}
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            reads = step.get("reads") or []
            rows.append(
                {
                    "operation_index": step.get("operation_index"),
                    "disposition": step.get("disposition"),
                    "response_goal": step.get("response_goal"),
                    "read_kinds": [
                        item.get("kind")
                        for item in reads
                        if isinstance(item, dict) and item.get("kind")
                    ],
                }
            )
    return rows


def _availability_summary(cap) -> dict[str, object] | None:
    operation_type = None
    operations = _operation_rows(cap)
    if operations:
        operation_type = operations[-1].get("type")
    for trace in reversed(cap.structured_trace):
        if not isinstance(trace, dict):
            continue
        for outcome in reversed(trace.get("outcomes") or []):
            if not isinstance(outcome, dict):
                continue
            facts = outcome.get("facts") if isinstance(outcome.get("facts"), dict) else {}
            availability = facts.get("availability")
            if not isinstance(availability, dict):
                continue
            windows = availability.get("availability_windows")
            checked_dates = availability.get("checked_dates")
            return {
                "operation_type": operation_type,
                "availability_option_count": availability.get("available_option_count"),
                "checked_dates": list(checked_dates) if isinstance(checked_dates, list) else [],
                "window_dates": [
                    str(window.get("start_local") or window.get("date") or "")[:10]
                    for window in windows or []
                    if isinstance(window, dict)
                ],
            }
    return None


def _active_task(db, ws, p, cid):
    if cid is None:
        return None
    persisted = load_active_task(
        db,
        workspace_id=ws.id,
        conversation_id=cid,
        patient_id=p.id,
    )
    return persisted.active_task.model_dump(mode="json") if persisted is not None else None


class AcceptanceConversation:
    def __init__(self, name, db, ws, p, now):
        self.name = name
        self.db = db
        self.ws = ws
        self.p = p
        self.now = now
        self.cid = None
        self.turns: list[dict[str, object]] = []
        self.errors: list[str] = []
        self.recent_verified_read: dict[str, object] | None = None

    def send(self, text: str) -> None:
        previous_recent = self.recent_verified_read
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
            operations = _operation_rows(cap)
            plan = _plan_rows(cap)
            current_recent = _availability_summary(cap)
            if current_recent is not None:
                self.recent_verified_read = current_recent
            conditional = any(
                row.get("continuation_condition") == "if_previous_no_availability"
                for row in operations
            )
            same_turn_previous = any(
                int(row.get("operation_index") or 0) > 0 and bool(plan[index - 1].get("read_kinds"))
                for index, row in enumerate(plan)
                if index > 0
            )
            self.turns.append(
                {
                    "customer": text,
                    "linka": cap.agent_response,
                    "operations": operations,
                    "plan": plan,
                    "recent_verified_read_before_turn": previous_recent,
                    "same_turn_previous_read_present": same_turn_previous,
                    "step_skipped": bool(conditional and not cap.verified_reads and cap.agent_response is None),
                    "verified_read_kinds": list(cap.verified_reads),
                    "write_attempted": bool(cap.write_attempted),
                    "error": None,
                    "active_task": _active_task(self.db, self.ws, self.p, self.cid),
                    "availability_result": current_recent,
                }
            )
        except Exception as exc:  # noqa: BLE001 -- eval evidence must retain hard stops
            error = f"{type(exc).__name__}: {exc}"
            self.errors.append(error)
            self.turns.append(
                {
                    "customer": text,
                    "linka": None,
                    "operations": [],
                    "plan": [],
                    "recent_verified_read_before_turn": previous_recent,
                    "same_turn_previous_read_present": False,
                    "step_skipped": None,
                    "verified_read_kinds": [],
                    "write_attempted": False,
                    "error": error,
                    "active_task": _active_task(self.db, self.ws, self.p, self.cid),
                    "availability_result": None,
                }
            )


def _close_weekday(db, ws, branch_id, weekday: int) -> None:
    db.execute(
        delete(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == ws.id,
            DoctorWorkingHour.branch_id == branch_id,
            DoctorWorkingHour.weekday == weekday,
        )
    )
    db.flush()


def _finish(result_id: str, c: AcceptanceConversation, before: dict) -> dict[str, object]:
    after = state_snapshot(c.db, c.ws, c.p)
    return {
        "id": result_id,
        "turns": c.turns,
        "errors": c.errors,
        "writes": sum(bool(turn.get("write_attempted")) for turn in c.turns),
        "appointments_before": len(before.get("appointments", [])),
        "appointments_after": len(after.get("appointments", [])),
    }


def r1(db, ws, branch_id):
    _close_weekday(db, ws, branch_id, 0)
    p = patient(db, ws, "F1R1")
    before = state_snapshot(db, ws, p)
    c = AcceptanceConversation("F1R1", db, ws, p, START)
    c.send("ممكن أحجز Under Arm على DEKA النهارده؟")
    c.send("طيب أول ميعاد فاضي بعد النهارده إمتى؟")
    return _finish("R1", c, before)


def r2(db, ws, branch_id):
    _close_weekday(db, ws, branch_id, 1)
    p = patient(db, ws, "F1R2")
    before = state_snapshot(db, ws, p)
    c = AcceptanceConversation("F1R2", db, ws, p, START)
    c.send("عايز أحجز Under Arm على DEKA")
    c.send("طب بكرة فيه مكان؟")
    c.send("واللي بعدها أقرب حاجة إمتى؟")
    return _finish("R2", c, before)


def r3(db, ws, branch_id):
    _close_weekday(db, ws, branch_id, 3)
    p = patient(db, ws, "F1R3")
    before = state_snapshot(db, ws, p)
    c = AcceptanceConversation("F1R3", db, ws, p, START)
    c.send("محتاج Under Arm على DEKA الخميس")
    c.send("طب بعد الخميس أول حاجة فاضية إمتى؟")
    return _finish("R3", c, before)


def r4(db, ws, branch_id):
    _close_weekday(db, ws, branch_id, 1)
    p = patient(db, ws, "F1R4")
    before = state_snapshot(db, ws, p)
    c = AcceptanceConversation("F1R4", db, ws, p, START)
    c.send("عايز Under Arm على DEKA بكرة بعد 7 بالليل")
    c.send("طب أقرب ميعاد بعد كده؟")
    return _finish("R4", c, before)


def r5(db, ws, branch_id):
    _close_weekday(db, ws, branch_id, 4)
    p = patient(db, ws, "F1R5")
    before = state_snapshot(db, ws, p)
    c = AcceptanceConversation("F1R5", db, ws, p, START)
    c.send("عايز أحجز Under Arm على DEKA")
    c.send("الأربع مناسب؟")
    c.send("لا خليه الجمعة")
    c.send("طب أقرب يوم بعدها؟")
    return _finish("R5", c, before)


def r6():
    operation = TurnOperation(
        type="availability",
        entities=TurnEntities(),
        continues_previous=True,
        continuation_condition="if_previous_no_availability",
    )
    recent = {"operation_type": "availability", "availability_option_count": 2}
    satisfied = runtime._continuation_condition_satisfied(
        operation,
        previous_reads=None,
        recent_read_context=recent,
    )
    return {
        "id": "R6",
        "customer_control": "deterministic negative control",
        "operations": [
            {
                "type": "availability",
                "continues_previous": True,
                "continuation_condition": "if_previous_no_availability",
                "date_mode": None,
            }
        ],
        "recent_verified_read_before_turn": recent,
        "same_turn_previous_read_present": False,
        "step_skipped": not satisfied,
        "verified_read_kinds": [],
        "write_attempted": False,
        "error": None,
        "condition_satisfied": satisfied,
        "writes": 0,
        "errors": [],
    }


def r7(db, ws, branch_id):
    _close_weekday(db, ws, branch_id, 0)
    p = patient(db, ws, "F1R7")
    before = state_snapshot(db, ws, p)
    c = AcceptanceConversation("F1R7", db, ws, p, BOUNDARY_LOCAL)
    c.send("ممكن Under Arm على DEKA النهارده؟")
    result = _finish("R7", c, before)
    result["boundary"] = {
        "utc_now": BOUNDARY_UTC.isoformat(),
        "workspace_local_now": BOUNDARY_LOCAL.isoformat(),
        "direct_formatter_oct5": format_customer_date(
            "2026-10-05",
            arabic=True,
            reference_datetime=BOUNDARY_UTC,
            timezone_name="Africa/Cairo",
        ),
    }
    return result


def r8(db, ws, branch_id):
    _close_weekday(db, ws, branch_id, 0)
    p = patient(db, ws, "F1R8")
    before = state_snapshot(db, ws, p)
    c = AcceptanceConversation("F1R8", db, ws, p, START)
    c.send("عايز Under Arm على DEKA النهارده")
    c.send("مفيش حاجة بعدها على طول؟")
    return _finish("R8", c, before)


def _run_isolated(db, fn, *args):
    nested = db.begin_nested()
    try:
        return fn(db, *args)
    finally:
        if nested.is_active:
            nested.rollback()
        db.expire_all()


def _acceptance_summary(results: list[dict[str, object]]) -> dict[str, object]:
    conversation_rows = [row for row in results if row["id"] != "R6"]
    runtime_errors = sum(len(row.get("errors") or []) for row in results)
    writes = sum(int(row.get("writes") or 0) for row in results)
    zero_outcomes = sum(
        1
        for row in conversation_rows
        for turn in row.get("turns") or []
        if turn.get("error") and "neither a customer outcome nor a pending write" in str(turn.get("error"))
    )
    return {
        "fresh_conversations_completed": len(conversation_rows),
        "runtime_errors": runtime_errors,
        "zero_outcome_hard_stops": zero_outcomes,
        "writes": writes,
        "negative_control_condition_satisfied": next((row.get("condition_satisfied") for row in results if row["id"] == "R6"), None),
    }


def main() -> None:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "f1_nearest_availability_acceptance.json")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    conn = engine.connect()
    outer = conn.begin()
    db = Session(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        ws = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        assert_demo_only(ws)
        _under, _full, _mary, _youssef, branch_id = prepare(db, ws)
        mode = sys.argv[2] if len(sys.argv) > 2 else "all"
        if mode == "r8":
            results = [_run_isolated(db, r8, ws, branch_id)]
        else:
            results = [
                _run_isolated(db, r1, ws, branch_id),
                _run_isolated(db, r2, ws, branch_id),
                _run_isolated(db, r3, ws, branch_id),
                _run_isolated(db, r4, ws, branch_id),
                _run_isolated(db, r5, ws, branch_id),
                r6(),
                _run_isolated(db, r7, ws, branch_id),
                _run_isolated(db, r8, ws, branch_id),
            ]
        payload = {
            "starting_pr_head": "c1095efe03948b87ddb6417d8c1f3dea050f227c",
            "evaluation_only": True,
            "rollback_transaction": True,
            "results": results,
            "summary": _acceptance_summary(results),
        }
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print("SUMMARY", json.dumps(payload["summary"], ensure_ascii=False))
        for row in results:
            print(row["id"], "errors=", row.get("errors"), "writes=", row.get("writes"))
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        conn.close()
        engine.dispose()
        print("OUTER_ROLLBACK=CONFIRMED")


if __name__ == "__main__":
    main()
