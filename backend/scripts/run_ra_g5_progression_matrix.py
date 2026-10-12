from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_grouped_multiservice_intent_live_review as live

from app.models.conversation import Conversation
from app.services.agent_v2.orchestrator import orchestrate_v2_turn
from app.services.agent_v2.state import BookingTaskState

GROUPED = live.ORIGINAL
SINGLE = (
    "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0639\u0644\u0649 Candela Gentle \u0645\u0639 \u062f. \u0645\u0631\u064a\u0645 \u0623\u062d\u0645\u062f\u060c "
    "\u0648\u0631\u064a\u0646\u064a \u0623\u0642\u0631\u0628 \u0645\u064a\u0639\u0627\u062f \u0645\u0646\u0627\u0633\u0628 \u0642\u0628\u0644 \u0645\u0627 \u062a\u062d\u062c\u0632"
)
PICK_FIRST = "\u0627\u0644\u0627\u062e\u062a\u064a\u0627\u0631 \u0631\u0642\u0645 1 \u0645\u0646\u0627\u0633\u0628\u060c \u0627\u062d\u062c\u0632\u064a\u0647 \u0648\u0648\u0631\u064a\u0646\u064a \u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0642\u0628\u0644 \u0627\u0644\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0626\u064a"
PICK_NEW = "\u0627\u0644\u0633\u0628\u062a \u0627\u0644\u0633\u0627\u0639\u0629 6 \u0645\u0633\u0627\u0621 \u0645\u0646\u0627\u0633\u0628\u060c \u0627\u062d\u062c\u0632\u064a\u0647 \u0648\u0648\u0631\u064a\u0646\u064a \u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0642\u0628\u0644 \u0627\u0644\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0626\u064a"


def dump(value):
    return live.dump(value)


def raw_operation(raw):
    for item in raw:
        structured = item.get("structured") if isinstance(item, dict) else None
        if isinstance(structured, dict):
            operations = structured.get("operations")
            if isinstance(operations, list) and operations:
                return operations[0]
    return None


def snapshot_summary(task):
    if not isinstance(task, BookingTaskState) or task.option_snapshot is None:
        return None
    snap = task.option_snapshot
    return {
        "snapshot_id": snap.snapshot_id,
        "purpose": snap.purpose,
        "task_version": snap.task_version,
        "option_count": len(snap.options),
        "first_option": (
            {
                "ref": snap.options[0].ref,
                "label": snap.options[0].label,
                "payload": dict(snap.options[0].payload),
            }
            if snap.options else None
        ),
    }


def task_summary(task):
    if not isinstance(task, BookingTaskState):
        return None
    proposal = task.proposal
    return {
        "version": task.version,
        "status": task.status,
        "requested_service_ids": list(task.requested_service_ids),
        "constraints": task.constraints.model_dump(mode="json"),
        "commercial_basis_presented_key": task.derived.commercial_basis_presented_key,
        "commercial_basis_presented_device_key": task.derived.commercial_basis_presented_device_key,
        "selected_slot_ref": task.derived.selected_slot_ref,
        "option_snapshot": snapshot_summary(task),
        "proposal": proposal.model_dump(mode="json") if proposal is not None else None,
    }


def outcome_summary(turn):
    outcome = turn.outcomes[-1] if turn.outcomes else None
    facts = dict(outcome.facts) if outcome is not None else {}
    return {
        "response_goal": outcome.response_goal if outcome is not None else None,
        "booking_device_price_step": facts.get("booking_device_price_step") is True,
        "booking_commercial_basis_step": facts.get("booking_commercial_basis_step") is True,
        "commercial_basis_key": facts.get("commercial_basis_key"),
        "facts": facts,
    }


def trace_summary(turn):
    return [
        {
            "operation_type": trace.operation_type,
            "disposition_before": trace.disposition_before,
            "disposition_after": trace.disposition_after,
            "read_kinds": list(trace.read_kinds),
            "verified_parameters": dict(trace.verified_parameters or {}),
            "availability_slot_count": len(trace.availability_slots),
            "pending_write": trace.pending_write,
        }
        for trace in turn.traces
    ]


def customer_visible_commercial(reply):
    text = str(reply or "")
    return any(marker in text for marker in ("\u062c\u0646\u064a\u0647", "EGP", "\u0633\u0639\u0631", "\u062a\u0643\u0644\u0641"))


def matching_message(task):
    if not isinstance(task, BookingTaskState) or task.option_snapshot is None or not task.option_snapshot.options:
        raise AssertionError("displayed matching-time case requires a current option snapshot")
    payload = task.option_snapshot.options[0].payload
    start_time = str(payload.get("start_time_24h") or "")[:5]
    if not start_time:
        raise AssertionError("displayed option missing start_time_24h")
    return (
        "\u0627\u0644\u0646\u0647\u0627\u0631\u062f\u0647 \u0627\u0644\u0633\u0627\u0639\u0629 " + start_time +
        " \u0645\u0646\u0627\u0633\u0628\u060c \u0627\u062d\u062c\u0632\u064a\u0647 \u0648\u0648\u0631\u064a\u0646\u064a \u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0642\u0628\u0644 \u0627\u0644\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0626\u064a"
    )


def run_turn(db, workspace, patient, conversation_id, history, message, turn_id):
    history.append(HumanMessage(content=message))
    live.RAW.clear()
    turn = orchestrate_v2_turn(
        db=db,
        workspace=workspace,
        patient=patient,
        conversation_id=conversation_id,
        run_id=uuid4(),
        history=history,
        local_now=live.NOW,
        timezone_name=workspace.timezone,
        clinic_name=workspace.name,
        turn_id=turn_id,
        write_executor=None,
    )
    raw = list(live.RAW)
    if turn.reply:
        history.append(AIMessage(content=turn.reply))
    return turn, raw


def run_case(name, grouped, selection_mode):
    with live.rollback_session() as db:
        workspace, patient, doctor, underarm, bikini, arms = live.build_fixture(db)
        conversation_id = uuid4()
        db.add(Conversation(
            id=conversation_id,
            workspace_id=workspace.id,
            patient_id=patient.id,
            channel="web",
            status="open",
            owner_type="ai",
            unread_count=0,
            started_at=live.NOW,
            last_message_at=live.NOW,
        ))
        db.flush()
        history = []
        before_count = live.appointment_count(db, workspace.id)
        first_message = GROUPED if grouped else SINGLE
        first, first_raw = run_turn(
            db, workspace, patient, conversation_id, history, first_message, f"{name.lower()}-1"
        )
        before_task = first.active_task
        if selection_mode == "option_ref":
            second_message = PICK_FIRST
        elif selection_mode == "matching_time":
            second_message = matching_message(before_task)
        elif selection_mode == "new_time":
            second_message = PICK_NEW
        else:
            raise AssertionError(selection_mode)
        second, second_raw = run_turn(
            db, workspace, patient, conversation_id, history, second_message, f"{name.lower()}-2"
        )
        after_count = live.appointment_count(db, workspace.id)
        if before_count != 0 or after_count != 0:
            raise AssertionError(f"{name}: audit created appointment")

        after_task = second.active_task
        second_op = raw_operation(second_raw)
        if selection_mode == "option_ref":
            verification_pass = (
                isinstance(before_task, BookingTaskState)
                and before_task.option_snapshot is not None
                and before_task.option_snapshot.task_version == before_task.version
            )
            verification_basis = "current_verified_option_snapshot"
        else:
            verification_pass = any(
                bool(trace.availability_slots) and bool(trace.verified_parameters)
                for trace in second.traces
            )
            verification_basis = "fresh_availability_read"

        proposal = after_task.proposal if isinstance(after_task, BookingTaskState) else None
        result = {
            "case": name,
            "grouped": grouped,
            "selection_mode": selection_mode,
            "customer": [first_message, second_message],
            "agent": [first.reply, second.reply],
            "turn1": {
                "raw_structured_operation": raw_operation(first_raw),
                "task": task_summary(before_task),
                "outcome": outcome_summary(first),
                "traces": trace_summary(first),
            },
            "turn2": {
                "raw_structured_operation": second_op,
                "active_task_relationship": (second_op or {}).get("active_task_relationship"),
                "active_task_explicit_fields": (second_op or {}).get("active_task_explicit_fields"),
                "task_before": task_summary(before_task),
                "task_after": task_summary(after_task),
                "availability_verification": "PASS" if verification_pass else "FAIL",
                "availability_verification_basis": verification_basis,
                "verified_parameters": [dict(t.verified_parameters or {}) for t in second.traces],
                "selected_slot_ref": (
                    after_task.derived.selected_slot_ref if isinstance(after_task, BookingTaskState) else None
                ),
                "outcome": outcome_summary(second),
                "traces": trace_summary(second),
                "proposal": proposal.model_dump(mode="json") if proposal is not None else None,
                "pending_write": dump(second.pending_write),
            },
            "commercial_basis_presented_key_before": (
                before_task.derived.commercial_basis_presented_key if isinstance(before_task, BookingTaskState) else None
            ),
            "commercial_basis_presented_key_after": (
                after_task.derived.commercial_basis_presented_key if isinstance(after_task, BookingTaskState) else None
            ),
            "commercial_truth_customer_visible_before_selection": customer_visible_commercial(first.reply),
            "commercial_truth_customer_visible_after_selection_turn": customer_visible_commercial(second.reply),
            "proposal_status": "PROPOSAL" if proposal is not None else "NO PROPOSAL",
            "appointment_count_before": before_count,
            "appointment_count_after": after_count,
        }
        print("RA_G5_MATRIX=" + json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
        return result


def main():
    matrix = [
        ("M1", True, "option_ref"),
        ("M2", True, "matching_time"),
        ("M3", True, "new_time"),
        ("M4", False, "option_ref"),
        ("M5", False, "matching_time"),
        ("M6", False, "new_time"),
    ]
    results = []
    for item in matrix:
        case_name = item[0]
        last_error = None
        result = None
        for attempt in range(1, 4):
            try:
                result = run_case(*item)
                result["semantic_attempt"] = attempt
                result["structured_output_failures_before_success"] = attempt - 1
                break
            except Exception as exc:
                last_error = exc
                print(
                    "RA_G5_MATRIX_ATTEMPT_FAILURE="
                    + json.dumps(
                        {
                            "case": case_name,
                            "attempt": attempt,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "raw": list(live.RAW),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    flush=True,
                )
        if result is None:
            result = {
                "case": case_name,
                "proposal_status": "STRUCTURED OUTPUT FAILURE",
                "error_type": type(last_error).__name__ if last_error else None,
                "error": str(last_error) if last_error else None,
                "structured_output_failures_before_success": 3,
            }
            print(
                "RA_G5_MATRIX="
                + json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str),
                flush=True,
            )
        results.append(result)
    summary = {item["case"]: item["proposal_status"] for item in results}`r`n    print("RA_G5_MATRIX_SUMMARY=" + json.dumps(summary, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
