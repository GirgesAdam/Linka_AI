from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents import clinic_grounding
from app.core.config import settings
from app.models.branch import Branch
from app.models.patient import Patient
from app.models.workspace import Workspace
from app.services.agent_v2.state_persistence import load_active_task
from tools.agent_eval.harness import assert_demo_only, send_turn, state_snapshot
from tools.agent_eval.run_batch_01 import quiet_patient

SERVICES_3 = (
    "ليزر إزالة الشعر - إبط، ليزر إزالة الشعر - بكيني، "
    "ليزر إزالة الشعر - ذراعين كاملين"
)
SERVICES_2 = "ليزر إزالة الشعر - إبط، ليزر إزالة الشعر - بكيني"
DATE = "يوم الثلاثاء الجاي"
BRANCH = "فرع التجمع الخامس"

engine = create_engine(settings.database_url, pool_pre_ping=True)


def _business_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    keys = ("appointments", "packages", "pulse_packs", "payments", "pulse_usages", "pulse_settlements")
    return {key: after.get(key) != before.get(key) for key in keys}


def _clear_catalog_cache() -> None:
    with clinic_grounding._catalog_cache_lock:
        clinic_grounding._catalog_cache.clear()


def _task(db: Session, workspace: Workspace, patient: Patient, conversation_id):
    handle = load_active_task(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        conversation_id=conversation_id,
    )
    if handle is None:
        return None
    return handle.active_task.model_dump(mode="json")


def _summary(turn) -> dict[str, Any]:
    return {
        "user": turn.user_message,
        "reply": turn.agent_response,
        "model": turn.model,
        "reads": turn.verified_reads,
        "write_attempted": turn.write_attempted,
        "write_result": turn.write_result,
        "actions": turn.actions,
        "handoff": turn.handoff_state,
        "errors": turn.errors,
        "trace": turn.structured_trace,
    }


def _has_read_error(turn) -> bool:
    payload = json.dumps(
        {"trace": turn.structured_trace, "errors": turn.errors},
        ensure_ascii=False,
        default=str,
    )
    return "ReadExecutionError" in payload or "Single-location clinic branch could not be resolved" in payload


def _group_services(task: dict[str, Any] | None) -> list[str]:
    if not task:
        return []
    grouped = task.get("grouped") or {}
    components = grouped.get("components") or []
    values = [str(item.get("service_id")) for item in components if item.get("service_id")]
    if not values and task.get("constraints", {}).get("service_id"):
        values = [str(task["constraints"]["service_id"])]
    return values


def _group_branches(task: dict[str, Any] | None) -> list[str]:
    if not task:
        return []
    grouped = task.get("grouped") or {}
    components = grouped.get("components") or []
    values = [str(item.get("branch_id")) for item in components if item.get("branch_id")]
    main = (task.get("constraints") or {}).get("branch_id")
    if main:
        values.append(str(main))
    return sorted(set(values))


def _reply_text(result: dict[str, Any]) -> str:
    return "\n".join(str(turn.get("reply") or "") for turn in result["turns"])


def _safe(result: dict[str, Any]) -> bool:
    return (
        not result["read_error"]
        and not any(result["db_delta"].values())
        and result["persisted_after_rollback_matches_before"]
    )


@contextmanager
def scenario(*, single_location: bool):
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if workspace is None:
            raise RuntimeError("missing synthetic workspace")
        assert_demo_only(workspace)
        branches = list(db.scalars(select(Branch).where(Branch.workspace_id == workspace.id).order_by(Branch.code)))
        if single_location:
            keep = next(row for row in branches if row.code == "new-cairo")
            for row in branches:
                row.is_active = row.id == keep.id
            workspace.primary_branch_id = keep.id
        else:
            workspace.primary_branch_id = None
            for row in branches:
                row.is_active = True
        db.flush()
        _clear_catalog_cache()
        patient = quiet_patient(db, workspace)
        before = state_snapshot(db, workspace, patient)
        active_branches = [str(row.id) for row in branches if row.is_active]
        yield db, workspace, patient, before, active_branches
    finally:
        _clear_catalog_cache()
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


def run_messages(name: str, messages: list[str], *, single_location: bool) -> dict[str, Any]:
    with scenario(single_location=single_location) as (db, workspace, patient, before, active_branches):
        turns = []
        conversation_id = None
        for number, message in enumerate(messages, start=1):
            response, capture = send_turn(
                db, workspace, patient, name, number, message, conversation_id
            )
            conversation_id = response.conversation_id
            turns.append(capture)
        after = state_snapshot(db, workspace, patient)
        task = _task(db, workspace, patient, conversation_id) if conversation_id else None
        result = {
            "turns": [_summary(turn) for turn in turns],
            "task": task,
            "service_ids": _group_services(task),
            "branch_ids": _group_branches(task),
            "active_branch_count": len(active_branches),
            "db_delta": _business_delta(before, after),
            "read_error": any(_has_read_error(turn) for turn in turns),
            "write_results": [turn.write_result for turn in turns if turn.write_result],
        }
        patient_id = patient.id
        workspace_id = workspace.id
    with Session(engine) as verify:
        workspace = verify.get(Workspace, workspace_id)
        patient = verify.get(Patient, patient_id)
        persisted = state_snapshot(verify, workspace, patient)
    result["persisted_after_rollback_matches_before"] = persisted == before
    return result


def classify(name: str, result: dict[str, Any], predicate) -> dict[str, Any]:
    ok = bool(predicate(result)) and result["persisted_after_rollback_matches_before"]
    result["verdict"] = "PASS" if ok else "FAIL"
    print(json.dumps({"scenario": name, **result}, ensure_ascii=False, default=str))
    return result


def main() -> int:
    results: dict[str, dict[str, Any]] = {}
    request3 = f"عايزة أحجز {SERVICES_3}"
    request2 = f"عايزة أحجز {SERVICES_2}"

    for name, request in (("MB1", request3), ("MB2", request2), ("MB3", request3)):
        result = run_messages(name, [request, DATE], single_location=True)
        results[name] = classify(name, result, _safe)

    result = run_messages("MB4", [request2, DATE], single_location=True)
    results["MB4"] = classify(
        "MB4", result, lambda r: _safe(r) and r["active_branch_count"] == 1
    )

    result = run_messages("MB5", [request2, DATE], single_location=False)
    results["MB5"] = classify(
        "MB5",
        result,
        lambda r: (
            _safe(r)
            and r["active_branch_count"] >= 2
            and "فرع" in _reply_text(r)
            and not r["write_results"]
        ),
    )

    result = run_messages("MB6", [f"عايزة أحجز {SERVICES_2} في {BRANCH}", DATE], single_location=False)
    results["MB6"] = classify(
        "MB6",
        result,
        lambda r: _safe(r) and "أحمد محمود" not in _reply_text(r),
    )

    result = run_messages("MB7", [request2, f"{DATE} في {BRANCH}"], single_location=False)
    results["MB7"] = classify(
        "MB7",
        result,
        lambda r: _safe(r) and "أحمد محمود" not in _reply_text(r),
    )

    result = run_messages("MB8", [request3, "سعر جلسة الإبط كام؟", DATE], single_location=True)
    results["MB8"] = classify(
        "MB8",
        result,
        lambda r: _safe(r) and "٥٥٠" in _reply_text(r),
    )

    result = run_messages("MB9", [request3, f"خليهم {SERVICES_2} بس", DATE], single_location=True)
    results["MB9"] = classify("MB9", result, _safe)

    result = run_messages(
        "MB10",
        [f"عايزة أحجز {SERVICES_3} مع د. عمر خليل في فرع الشيخ زايد", DATE],
        single_location=False,
    )
    results["MB10"] = classify(
        "MB10",
        result,
        lambda r: _safe(r) and "عمر خليل" in _reply_text(r) and not r["write_results"],
    )

    result = run_messages(
        "SINGLE",
        ["عايزة أحجز ليزر إزالة الشعر - إبط", DATE],
        single_location=True,
    )
    results["SINGLE"] = classify("SINGLE", result, _safe)

    failed = [name for name, result in results.items() if result["verdict"] != "PASS"]
    summary = {
        "models": sorted({t["model"] for r in results.values() for t in r["turns"] if t.get("model")}),
        "wrong_writes": 0 if all(not any(r["db_delta"].values()) for r in results.values()) else "REVIEW",
        "partial_writes": 0 if all(not any(r["db_delta"].values()) for r in results.values()) else "REVIEW",
        "false_success": 0 if all(not r["read_error"] for r in results.values()) else "REVIEW",
        "persisted_audit_writes": 0 if all(r["persisted_after_rollback_matches_before"] for r in results.values()) else "REVIEW",
        "failed": failed,
    }
    print("MB_AUDIT_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
