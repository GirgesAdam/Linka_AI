from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_grouped_multiservice_intent_live_review as live
import run_ra_g5_progression_matrix as matrix

from app.models.conversation import Conversation
from app.services.agent_v2.state import BookingTaskState

SINGLE_BOOK = "عايزة أحجز ليزر بكيني على Candela Gentle مع د. مريم أحمد"
SINGLE_AVAIL = "وريني أقرب ميعاد مناسب لليزر بكيني على Candela Gentle مع د. مريم أحمد قبل ما تحجز"
CONTINUE_AFTER_DISCLOSURE = "تمام، السعر مناسب. كملي ووريني ملخص الحجز قبل التأكيد النهائي"


def _conversation(db, workspace, patient):
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
    return conversation_id


def _proposal_status(task):
    return "PROPOSAL" if isinstance(task, BookingTaskState) and task.proposal is not None else "NO PROPOSAL"


def run_single(name: str, selection_mode: str):
    with live.rollback_session() as db:
        workspace, patient, *_ = live.build_fixture(db)
        conversation_id = _conversation(db, workspace, patient)
        history = []
        before_count = live.appointment_count(db, workspace.id)

        commercial, commercial_raw = matrix.run_turn(
            db, workspace, patient, conversation_id, history, SINGLE_BOOK, f"{name.lower()}-commercial"
        )
        commercial_task = commercial.active_task
        if not isinstance(commercial_task, BookingTaskState):
            raise AssertionError(f"{name}: direct single booking did not establish active task")
        if commercial_task.derived.commercial_basis_presented_key is None:
            raise AssertionError(f"{name}: normal single booking did not present commercial basis")
        if not matrix.customer_visible_commercial(commercial.reply):
            raise AssertionError(f"{name}: commercial key was recorded without customer-visible truth")

        availability, availability_raw = matrix.run_turn(
            db, workspace, patient, conversation_id, history, SINGLE_AVAIL, f"{name.lower()}-availability"
        )
        before_task = availability.active_task
        if not isinstance(before_task, BookingTaskState) or before_task.option_snapshot is None:
            raise AssertionError(f"{name}: single availability did not retain current booking option snapshot")

        if selection_mode == "option_ref":
            customer = matrix.PICK_FIRST
        elif selection_mode == "matching_time":
            customer = matrix.matching_message(before_task)
        elif selection_mode == "new_time":
            customer = matrix.PICK_NEW
        else:
            raise AssertionError(selection_mode)

        selected, selected_raw = matrix.run_turn(
            db, workspace, patient, conversation_id, history, customer, f"{name.lower()}-select"
        )
        after_task = selected.active_task
        after_count = live.appointment_count(db, workspace.id)
        if before_count != 0 or after_count != 0:
            raise AssertionError(f"{name}: audit created appointment")

        proposal = after_task.proposal if isinstance(after_task, BookingTaskState) else None
        result = {
            "case": name,
            "journey": "single_book_commercial_then_availability_then_selection",
            "selection_mode": selection_mode,
            "customer": [SINGLE_BOOK, SINGLE_AVAIL, customer],
            "agent": [commercial.reply, availability.reply, selected.reply],
            "commercial_turn": {
                "raw_structured_operation": matrix.raw_operation(commercial_raw),
                "task": matrix.task_summary(commercial_task),
                "outcome": matrix.outcome_summary(commercial),
            },
            "availability_turn": {
                "raw_structured_operation": matrix.raw_operation(availability_raw),
                "task": matrix.task_summary(before_task),
                "outcome": matrix.outcome_summary(availability),
            },
            "selection_turn": {
                "raw_structured_operation": matrix.raw_operation(selected_raw),
                "task": matrix.task_summary(after_task),
                "outcome": matrix.outcome_summary(selected),
                "traces": matrix.trace_summary(selected),
                "proposal": proposal.model_dump(mode="json") if proposal is not None else None,
                "pending_write": matrix.dump(selected.pending_write),
            },
            "commercial_truth_customer_visible_before_selection": True,
            "commercial_basis_presented_key_before_selection": before_task.derived.commercial_basis_presented_key,
            "proposal_status": _proposal_status(after_task),
            "appointment_count_before": before_count,
            "appointment_count_after": after_count,
        }
        print("RA_G5_SINGLE_COMPARATOR=" + json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
        return result


def run_grouped_p4_proof():
    with live.rollback_session() as db:
        workspace, patient, *_ = live.build_fixture(db)
        conversation_id = _conversation(db, workspace, patient)
        history = []
        before_count = live.appointment_count(db, workspace.id)

        first, first_raw = matrix.run_turn(
            db, workspace, patient, conversation_id, history, live.ORIGINAL, "p4-1"
        )
        second, second_raw = matrix.run_turn(
            db, workspace, patient, conversation_id, history, matrix.PICK_NEW, "p4-2"
        )
        second_task = second.active_task
        if not isinstance(second_task, BookingTaskState):
            raise AssertionError("P4 proof: grouped task lost after commercial disclosure")
        if second_task.proposal is not None:
            raise AssertionError("P4 proof: proposal unexpectedly existed before post-disclosure consent")
        if second_task.derived.commercial_basis_presented_key is None:
            raise AssertionError("P4 proof: commercial basis was not recorded after disclosure")
        if not matrix.customer_visible_commercial(second.reply):
            raise AssertionError("P4 proof: commercial basis was not customer-visible")

        third, third_raw = matrix.run_turn(
            db, workspace, patient, conversation_id, history, CONTINUE_AFTER_DISCLOSURE, "p4-3"
        )
        final_task = third.active_task
        after_count = live.appointment_count(db, workspace.id)
        if before_count != 0 or after_count != 0:
            raise AssertionError("P4 proof: audit created appointment")
        if not isinstance(final_task, BookingTaskState) or final_task.proposal is None:
            raise AssertionError("P4 proof: post-disclosure continuation did not create proposal")
        expected = {str(first.active_task.requested_service_ids[0]), str(first.active_task.requested_service_ids[1])}
        actual = set(final_task.proposal.service_ids)
        if actual != expected:
            raise AssertionError(f"P4 proof: proposal lost services expected={expected} actual={actual}")
        result = {
            "case": "P4-PROOF",
            "customer": [live.ORIGINAL, matrix.PICK_NEW, CONTINUE_AFTER_DISCLOSURE],
            "agent": [first.reply, second.reply, third.reply],
            "turn1": {"raw": matrix.raw_operation(first_raw), "task": matrix.task_summary(first.active_task)},
            "turn2": {"raw": matrix.raw_operation(second_raw), "task": matrix.task_summary(second_task), "outcome": matrix.outcome_summary(second)},
            "turn3": {"raw": matrix.raw_operation(third_raw), "task": matrix.task_summary(final_task), "outcome": matrix.outcome_summary(third), "traces": matrix.trace_summary(third)},
            "requested_service_ids": list(final_task.requested_service_ids),
            "proposal": final_task.proposal.model_dump(mode="json"),
            "pending_write": matrix.dump(third.pending_write),
            "appointment_count_before": before_count,
            "appointment_count_after": after_count,
        }
        print("RA_G5_P4_PROOF=" + json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
        return result


def main() -> int:
    outputs = []
    for name, mode in (("M4", "option_ref"), ("M5", "matching_time"), ("M6", "new_time")):
        last = None
        for attempt in range(1, 4):
            try:
                result = run_single(name, mode)
                result["attempt"] = attempt
                outputs.append(result)
                break
            except Exception as exc:
                last = exc
                print("RA_G5_FOLLOWUP_ATTEMPT_FAILURE=" + json.dumps({"case": name, "attempt": attempt, "error_type": type(exc).__name__, "error": str(exc), "raw": list(live.RAW)}, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
        else:
            raise AssertionError(f"{name}: no valid comparator after retries: {last}")

    proof = None
    last = None
    for attempt in range(1, 4):
        try:
            proof = run_grouped_p4_proof()
            break
        except Exception as exc:
            last = exc
            print("RA_G5_FOLLOWUP_ATTEMPT_FAILURE=" + json.dumps({"case": "P4-PROOF", "attempt": attempt, "error_type": type(exc).__name__, "error": str(exc), "raw": list(live.RAW)}, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
    if proof is None:
        raise AssertionError(f"P4 proof failed after retries: {last}")

    print("RA_G5_FOLLOWUP_SUMMARY=" + json.dumps({item["case"]: item["proposal_status"] for item in outputs} | {"P4-PROOF": "PROPOSAL"}, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
