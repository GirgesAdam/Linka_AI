from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

from app.agents import clinic_grounding
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.branch import Branch
from app.models.patient import Patient
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.agent_v2.state_persistence import load_active_task
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import assert_demo_only, send_turn, state_snapshot
from tools.agent_eval.run_batch_01 import quiet_patient

DATE_TIME = "يوم الثلاثاء الجاي الساعة 11 صباحًا"
COMBO = "إبط وبكيني"
EXTRA = "هيدرافيشل"
DOCTOR = "د. مريم حسن"
BRANCH = "فرع التجمع الخامس"

engine = create_engine(settings.database_url, pool_pre_ping=True)


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
    return handle.active_task.model_dump(mode="json") if handle is not None else None


def _service_ids(db: Session, workspace: Workspace) -> dict[str, str]:
    rows = list(db.scalars(select(Service).where(Service.workspace_id == workspace.id)))
    by_slug = {row.slug: str(row.id) for row in rows}
    return {
        "combo": by_slug["laser-hair-removal-underarm-bikini"],
        "underarm": by_slug["laser-hair-removal-underarm"],
        "hydrafacial": by_slug["hydrafacial"],
    }


def _counts(db: Session, workspace: Workspace, patient: Patient) -> dict[str, int]:
    appointment_ids = select(Appointment.id).where(
        Appointment.workspace_id == workspace.id,
        Appointment.patient_id == patient.id,
    )
    return {
        "appointments": int(
            db.scalar(
                select(func.count()).select_from(Appointment).where(
                    Appointment.workspace_id == workspace.id,
                    Appointment.patient_id == patient.id,
                )
            )
            or 0
        ),
        "additional_services": int(
            db.scalar(
                select(func.count())
                .select_from(AppointmentAdditionalService)
                .where(
                    AppointmentAdditionalService.workspace_id == workspace.id,
                    AppointmentAdditionalService.appointment_id.in_(appointment_ids),
                )
            )
            or 0
        ),
    }


def _summary(turn) -> dict[str, Any]:
    return {
        "user": turn.user_message,
        "reply": turn.agent_response,
        "model": turn.model,
        "reads": turn.verified_reads,
        "write_attempted": turn.write_attempted,
        "write_result": turn.write_result,
        "actions": turn.actions,
        "errors": turn.errors,
        "trace": turn.structured_trace,
    }


def _has_error(turn) -> bool:
    payload = json.dumps(
        {"trace": turn.structured_trace, "errors": turn.errors},
        ensure_ascii=False,
        default=str,
    )
    return "ReadExecutionError" in payload or "Traceback" in payload


@contextmanager
def scenario(*, multi_location: bool = False):
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if workspace is None:
            raise RuntimeError("missing synthetic workspace")
        assert_demo_only(workspace)
        branches = list(
            db.scalars(
                select(Branch)
                .where(Branch.workspace_id == workspace.id)
                .order_by(Branch.code)
            )
        )
        if multi_location:
            workspace.primary_branch_id = None
            for row in branches:
                row.is_active = True
        else:
            keep = next(row for row in branches if row.code == "new-cairo")
            for row in branches:
                row.is_active = row.id == keep.id
            workspace.primary_branch_id = keep.id
        db.flush()
        _clear_catalog_cache()
        patient = quiet_patient(db, workspace)
        before_snapshot = state_snapshot(db, workspace, patient)
        before_counts = _counts(db, workspace, patient)
        ids = _service_ids(db, workspace)
        yield db, workspace, patient, before_snapshot, before_counts, ids
        patient_id = patient.id
        workspace_id = workspace.id
    finally:
        _clear_catalog_cache()
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
    with Session(engine) as verify:
        workspace = verify.get(Workspace, workspace_id)
        patient = verify.get(Patient, patient_id)
        persisted_snapshot = state_snapshot(verify, workspace, patient)
        persisted_counts = _counts(verify, workspace, patient)
    scenario.persisted_snapshot = persisted_snapshot
    scenario.persisted_counts = persisted_counts


def run_messages(name: str, messages: list[str], *, multi_location: bool = False) -> dict[str, Any]:
    with scenario(multi_location=multi_location) as (
        db,
        workspace,
        patient,
        before_snapshot,
        before_counts,
        ids,
    ):
        turns = []
        conversation_id = None
        count_history = [before_counts]
        task_history = []
        for number, message in enumerate(messages, start=1):
            response, capture = send_turn(
                db, workspace, patient, name, number, message, conversation_id
            )
            conversation_id = response.conversation_id
            turns.append(capture)
            count_history.append(_counts(db, workspace, patient))
            task_history.append(_task(db, workspace, patient, conversation_id))
        after_snapshot = state_snapshot(db, workspace, patient)
        final_task = task_history[-1] if task_history else None
        result = {
            "turns": [_summary(turn) for turn in turns],
            "task_history": task_history,
            "final_task": final_task,
            "counts": count_history,
            "snapshot_changed_in_tx": after_snapshot != before_snapshot,
            "service_ids": ids,
            "runtime_errors": any(_has_error(turn) for turn in turns),
        }
        expected_before = before_snapshot
        expected_counts = before_counts
    result["persisted_after_rollback_matches_before"] = (
        scenario.persisted_snapshot == expected_before
    )
    result["persisted_counts_after_rollback"] = scenario.persisted_counts
    result["persisted_counts_match_before"] = scenario.persisted_counts == expected_counts
    return result


def proposal(result: dict[str, Any], index: int = -1) -> dict[str, Any] | None:
    task = result["task_history"][index] if result["task_history"] else None
    raw = (task or {}).get("proposal") if isinstance(task, dict) else None
    return raw if isinstance(raw, dict) else None


def no_write(result: dict[str, Any]) -> bool:
    return (
        result["counts"][-1] == result["counts"][0]
        and not result["runtime_errors"]
        and result["persisted_after_rollback_matches_before"]
        and result["persisted_counts_match_before"]
    )


def classify(name: str, result: dict[str, Any], predicate) -> dict[str, Any]:
    ok = bool(predicate(result))
    result["verdict"] = "PASS" if ok else "FAIL"
    print(json.dumps({"scenario": name, **result}, ensure_ascii=False, default=str))
    return result


def _has_false_success(result: dict[str, Any]) -> bool:
    for index, turn in enumerate(result["turns"]):
        if index >= len(result["task_history"]):
            continue
        task = result["task_history"][index]
        if not isinstance(task, dict) or not isinstance(task.get("proposal"), dict):
            continue
        before = result["counts"][index]
        after = result["counts"][index + 1]
        if before != after:
            continue
        reply = str(turn.get("reply") or "").casefold()
        forbidden = ("تم تأكيد الحجز", "الحجز اتأكد", "booking is confirmed", "booking confirmed")
        if any(text in reply for text in forbidden):
            return True
    return False


def _safe_rollback(result: dict[str, Any]) -> bool:
    return (
        not result["runtime_errors"]
        and not _has_false_success(result)
        and result["persisted_after_rollback_matches_before"]
        and result["persisted_counts_match_before"]
    )


def main() -> int:
    results: dict[str, dict[str, Any]] = {}

    r = run_messages(
        "MS1_COMBO_ONLY",
        [f"عايزة أحجز {COMBO} {DATE_TIME} مع {DOCTOR}", "تمام"],
    )
    results["MS1_COMBO_ONLY"] = classify(
        "MS1_COMBO_ONLY",
        r,
        lambda x: (
            no_write(x)
            and _safe_rollback(x)
            and proposal(x) is not None
            and proposal(x)["service_ids"] == [x["service_ids"]["combo"]]
            and proposal(x)["main_service_id"] == x["service_ids"]["combo"]
            and proposal(x)["additional_service_ids"] == []
        ),
    )

    r = run_messages(
        "MS2_COMBO_PLUS_ADDITIONAL",
        [f"عايزة أحجز {COMBO} و{EXTRA} {DATE_TIME} مع {DOCTOR}", "تمام"],
    )
    results["MS2_COMBO_PLUS_ADDITIONAL"] = classify(
        "MS2_COMBO_PLUS_ADDITIONAL",
        r,
        lambda x: (
            no_write(x)
            and _safe_rollback(x)
            and proposal(x) is not None
            and set(proposal(x)["service_ids"])
            == {x["service_ids"]["combo"], x["service_ids"]["hydrafacial"]}
            and proposal(x)["main_service_id"] == x["service_ids"]["hydrafacial"]
            and proposal(x)["additional_service_ids"] == [x["service_ids"]["combo"]]
        ),
    )

    r = run_messages(
        "MS3_TEXT_CONFIRM_ATOMIC",
        [
            f"عايزة أحجز {COMBO} و{EXTRA} {DATE_TIME} مع {DOCTOR}",
            "تمام",
            "تمام أكد الحجز",
        ],
    )
    results["MS3_TEXT_CONFIRM_ATOMIC"] = classify(
        "MS3_TEXT_CONFIRM_ATOMIC",
        r,
        lambda x: (
            proposal(x, 1) is not None
            and x["counts"][1] == x["counts"][0]
            and x["counts"][2] == x["counts"][0]
            and x["counts"][3]["appointments"] == x["counts"][0]["appointments"] + 1
            and x["counts"][3]["additional_services"]
            == x["counts"][0]["additional_services"] + 1
            and x["final_task"] is None
            and _safe_rollback(x)
        ),
    )

    r = run_messages(
        "MS4_CORRECTION_REPLACES_SET",
        [
            f"عايزة أحجز {COMBO} و{EXTRA} {DATE_TIME} مع {DOCTOR}",
            f"لا خليها {COMBO} بس {DATE_TIME} مع {DOCTOR}",
            "تمام",
        ],
    )
    results["MS4_CORRECTION_REPLACES_SET"] = classify(
        "MS4_CORRECTION_REPLACES_SET",
        r,
        lambda x: (
            no_write(x)
            and _safe_rollback(x)
            and proposal(x) is not None
            and proposal(x)["service_ids"] == [x["service_ids"]["combo"]]
            and x["service_ids"]["hydrafacial"] not in proposal(x)["service_ids"]
        ),
    )

    r = run_messages(
        "MS5_SIDE_QUESTION_CONTINUITY",
        [
            f"عايزة أحجز {COMBO} و{EXTRA}",
            "سعر الهيدرافيشل كام؟",
            f"{DATE_TIME} مع {DOCTOR}",
        ],
    )
    results["MS5_SIDE_QUESTION_CONTINUITY"] = classify(
        "MS5_SIDE_QUESTION_CONTINUITY",
        r,
        lambda x: (
            no_write(x)
            and _safe_rollback(x)
            and proposal(x) is not None
            and set(proposal(x)["service_ids"])
            == {x["service_ids"]["combo"], x["service_ids"]["hydrafacial"]}
        ),
    )

    r = run_messages(
        "MS6_BRANCH_CONTINUATION",
        [
            f"عايزة أحجز {COMBO} و{EXTRA}",
            f"{DATE_TIME} في {BRANCH} مع {DOCTOR}",
        ],
        multi_location=True,
    )
    results["MS6_BRANCH_CONTINUATION"] = classify(
        "MS6_BRANCH_CONTINUATION",
        r,
        lambda x: (
            no_write(x)
            and _safe_rollback(x)
            and proposal(x) is not None
            and set(proposal(x)["service_ids"])
            == {x["service_ids"]["combo"], x["service_ids"]["hydrafacial"]}
        ),
    )

    r = run_messages(
        "MS7_INCOMPATIBLE_DOCTOR",
        [f"عايزة أحجز {COMBO} و{EXTRA} {DATE_TIME} مع د. عمر خليل في فرع الشيخ زايد"],
        multi_location=True,
    )
    results["MS7_INCOMPATIBLE_DOCTOR"] = classify(
        "MS7_INCOMPATIBLE_DOCTOR",
        r,
        lambda x: no_write(x) and _safe_rollback(x) and proposal(x) is None,
    )

    r = run_messages(
        "MS8_REPEAT_CONFIRM_NO_DUPLICATE",
        [
            f"عايزة أحجز {COMBO} و{EXTRA} {DATE_TIME} مع {DOCTOR}",
            "تمام",
            "تمام أكد الحجز",
            "تمام أكد الحجز",
        ],
    )
    results["MS8_REPEAT_CONFIRM_NO_DUPLICATE"] = classify(
        "MS8_REPEAT_CONFIRM_NO_DUPLICATE",
        r,
        lambda x: (
            x["counts"][3]["appointments"] == x["counts"][0]["appointments"] + 1
            and x["counts"][3]["additional_services"]
            == x["counts"][0]["additional_services"] + 1
            and x["counts"][4] == x["counts"][3]
            and _safe_rollback(x)
        ),
    )

    failed = [name for name, row in results.items() if row["verdict"] != "PASS"]
    summary = {
        "wrong_writes": 0
        if all(row["persisted_counts_match_before"] for row in results.values())
        else "REVIEW",
        "partial_writes": 0
        if all(row["persisted_counts_match_before"] for row in results.values())
        else "REVIEW",
        "false_success": 0
        if all(not _has_false_success(row) and not row["runtime_errors"] for row in results.values())
        else "REVIEW",
        "persisted_audit_writes": 0
        if all(row["persisted_counts_match_before"] for row in results.values())
        else "REVIEW",
        "failed": failed,
    }
    print("MS_AUDIT_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
