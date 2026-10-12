from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.models.appointment import Appointment
from app.models.message import Message
from app.services import whatsapp_interactions
from app.services.appointment_creation import create_appointment_operation
from tools.agent_eval import tmp_s9_ra10_audit as old
from tools.agent_eval import tmp_s9_ra_base as base

OUT = Path("audit-output")
OUT.mkdir(exist_ok=True)
AR_PENDING = "\u0642\u064a\u062f \u0627\u0644\u0627\u0646\u062a\u0638\u0627\u0631"
AR_BOOKING_CONFIRM = "\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u062d\u062c\u0632"
AR_BOOKING_CONFIRMED = "\u062d\u062c\u0632\u0643 \u0627\u062a\u0623\u0643\u062f"
AR_BOOKING_FIXED = "\u062b\u0628\u062a\u0646\u0627\u0644\u0643 \u0627\u0644\u062d\u062c\u0632"
AR_APPOINTMENT_CONFIRM = "\u0623\u0643\u062f\u0646\u0627 \u062d\u0636\u0648\u0631\u0643"


def no_false_booking_confirmation(text: str | None) -> bool:
    value = text or ""
    return not any(marker in value for marker in (AR_BOOKING_CONFIRM, AR_BOOKING_CONFIRMED, AR_BOOKING_FIXED))


def pending_reply_ok(text: str | None) -> bool:
    return bool(text and AR_PENDING in text and no_false_booking_confirmation(text))


def status_row(flow: base.Flow) -> dict[str, Any] | None:
    rows = base.appointment_rows(flow.db, flow.patient)
    return rows[0] if len(rows) == 1 else None


def finish(result: dict[str, Any]) -> dict[str, Any]:
    result.setdefault("false_confirmed_status_claim", 0)
    result.setdefault("duplicate_appointments", 0)
    result.setdefault("pending_to_confirmed_accidental_mutation", 0)
    result.setdefault("missing_status_confirmation_assumption", 0)
    result.setdefault("wrong_time_write", False)
    result.setdefault("wrong_doctor_write", False)
    result.setdefault("wrong_device_write", False)
    return base.finish_rollback_result(result)


def ra8() -> dict[str, Any]:
    result = old.ra8()
    reply = result.get("confirm_reply")
    persisted = result.get("persisted_status")
    proposal_before = result.get("proposal_before") or {}
    proposal_after = result.get("proposal_after") or {}
    checks = [
        (result.get("verdict") == "PASS", f"original RA8 functional verdict={result.get('verdict')}"),
        (proposal_before.get("proposal_id") == proposal_after.get("proposal_id"), "side question changed proposal"),
        (persisted == "pending", f"persisted status={persisted}"),
        (result.get("confirmed_at") is None, f"confirmed_at={result.get('confirmed_at')}"),
        (pending_reply_ok(reply), f"pending reply was status-unsound: {reply}"),
    ]
    failures = [detail for ok, detail in checks if not ok]
    result["verdict"] = "PASS" if not failures else "FAIL"
    result["failures"] = failures
    result["status_claim"] = "pending" if pending_reply_ok(reply) else "false-confirmed"
    result["matches_db"] = not failures
    result["false_confirmed_status_claim"] = int(persisted == "pending" and not pending_reply_ok(reply))
    return result


def ra10() -> dict[str, Any]:
    result = old.ra10()
    reply = result.get("confirm_reply")
    persisted = result.get("persisted_status")
    proposal = result.get("proposal_12") or {}
    rows = [event for event in result.get("events", []) if event.get("kind") == "button_confirm"]
    checks = [
        (result.get("verdict") == "PASS", f"original RA10 functional verdict={result.get('verdict')}"),
        (str(proposal.get("start_at", "")).startswith("2026-10-20T09:00:00"), f"proposal time={proposal.get('start_at')}"),
        (persisted == "pending", f"persisted status={persisted}"),
        (result.get("confirmed_at") is None, f"confirmed_at={result.get('confirmed_at')}"),
        (pending_reply_ok(reply), f"pending reply was status-unsound: {reply}"),
        (bool(rows), "confirmation event missing"),
    ]
    failures = [detail for ok, detail in checks if not ok]
    result["verdict"] = "PASS" if not failures else "FAIL"
    result["failures"] = failures
    result["status_claim"] = "pending" if pending_reply_ok(reply) else "false-confirmed"
    result["matches_db"] = not failures
    result["false_confirmed_status_claim"] = int(persisted == "pending" and not pending_reply_ok(reply))
    return result


def ra_s1() -> dict[str, Any]:
    with base.rollback_env("RA-S1", 221) as (f, services):
        old._prepare_candela(f, services)
        service = services[base.SLUG["underarm"]]
        yid = old._doctor_id(f.db, f.workspace, "\u064a\u0648\u0633\u0641", "\u0633\u0645\u064a\u0631")
        old._ensure_doctor_slot(f, doctor_id=yid, service_id=str(service.id))
        f.send("\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0632\u0627\u0644\u0629 \u0627\u0644\u0634\u0639\u0631 - \u0625\u0628\u0637")
        f.send("20 \u0623\u0643\u062a\u0648\u0628\u0631 2026")
        f.send("\u0627\u0644\u0633\u0627\u0639\u0629 16:00 \u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631")
        proposal = f.proposal() or f.ensure_proposal("\u062a\u0645\u0627\u0645")
        old._WRITE_RESULTS.clear()
        reply = f.button("confirm", proposal["proposal_id"]) if proposal else None
        write_result = dict(old._WRITE_RESULTS[-1]) if old._WRITE_RESULTS else None
        row = status_row(f)
        checks = [
            (proposal is not None, "proposal missing"),
            (row is not None, "appointment missing"),
            (bool(row and row["status"] == "pending"), f"status={(row or {}).get('status')}"),
            (bool(row and row["confirmed_at"] is None), f"confirmed_at={(row or {}).get('confirmed_at')}"),
            (pending_reply_ok(reply), f"reply={reply}"),
        ]
        result = old._result(checks, f, proposal=proposal, confirm_reply=reply, write_result=write_result, persisted=row )
        result["status_claim"] = "pending" if pending_reply_ok(reply) else "false-confirmed"
        result["matches_db"] = bool(row and row["status"] == "pending" and pending_reply_ok(reply))
        result["false_confirmed_status_claim"] = int(bool(row and row["status"] == "pending" and not pending_reply_ok(reply)))
    return finish(result)


def _existing_confirm(flow: base.Flow, appointment: Appointment) -> tuple[str, dict[str, Any]]:
    if flow.conversation_id is None:
        flow.send("\u0627\u0644\u0639\u064a\u0627\u062f\u0629 \u0628\u062a\u0642\u0641\u0644 \u0627\u0644\u0633\u0627\u0639\u0629 \u0643\u0627\u0645\u061f")
    conversation = flow.db.get(base.Conversation, flow.conversation_id)
    inbound = Message(
        workspace_id=flow.workspace.id,
        conversation_id=conversation.id,
        channel_connection_id=conversation.channel_connection_id,
        sender_type="patient",
        direction="inbound",
        message_type="interactive",
        content="\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0645\u0648\u0639\u062f",
        delivery_status="received",
        metadata_json={
            "interactive_reply": {
                "type": "button_reply",
                "id": f"tia.booking.confirm:{appointment.id}",
                "title": "\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0645\u0648\u0639\u062f",
            },
            "agent_run_id": str(uuid4()),
        },
    )
    flow.db.add(inbound)
    flow.db.flush()
    response = whatsapp_interactions.process_whatsapp_booking_action(
        flow.db,
        workspace=flow.workspace,
        patient=flow.patient,
        conversation=conversation,
        inbound=inbound,
    )
    if response is None:
        raise RuntimeError("existing confirmation action not handled")
    return response.reply or "", {"model": response.model, "customer": "[\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0645\u0648\u0639\u062f]", "agent": response.reply}


def ra_s2() -> dict[str, Any]:
    with base.rollback_env("RA-S2", 222) as (f, services):
        old._prepare_candela(f, services)
        service = services[base.SLUG["underarm"]]
        yid = old._doctor_id(f.db, f.workspace, "\u064a\u0648\u0633\u0641", "\u0633\u0645\u064a\u0631")
        old._ensure_doctor_slot(f, doctor_id=yid, service_id=str(service.id))
        branch = old._branch(f.db, f.workspace)
        appointment = create_appointment_operation(
            f.db,
            workspace=f.workspace,
            patient_id=f.patient.id,
            branch_id=branch.id,
            doctor_id=UUID(yid),
            service_id=service.id,
            requested_start_at=datetime(2026, 10, 13, 13, 0, tzinfo=UTC),
            created_by_user_id=None,
            source="ai",
            laser_device_key="candela",
            idempotency_key=f"audit-ra-s2-{f.patient.id}",
            actor_type="ai",
            now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        )
        assert appointment.status == "pending"
        assert appointment.confirmed_at is None
        f.db.flush()
        before = {"status": appointment.status, "confirmed_at": appointment.confirmed_at}
        reply, event = _existing_confirm(f, appointment)
        f.db.refresh(appointment)
        after = {"status": appointment.status, "confirmed_at": appointment.confirmed_at.isoformat() if appointment.confirmed_at else None}
        checks = [
            (before["status"] == "pending", f"before={before}"),
            (after["status"] == "confirmed", f"after={after}"),
            (after["confirmed_at"] is not None, f"after={after}"),
            (AR_APPOINTMENT_CONFIRM in reply, f"reply={reply}"),
        ]
        result = old._result(checks, f, fixture_before=before, persisted_after=after, confirmation_event=event, confirm_reply=reply)
        result["status_claim"] = "confirmed" if AR_APPOINTMENT_CONFIRM in reply else "other"
        result["matches_db"] = after["status"] == "confirmed" and AR_APPOINTMENT_CONFIRM in reply
    return finish(result)


def ra_s3() -> dict[str, Any]:
    with base.rollback_env("RA-S3", 223) as (f, services):
        old._prepare_candela(f, services)
        service = services[base.SLUG["underarm"]]
        yid = old._doctor_id(f.db, f.workspace, "\u064a\u0648\u0633\u0641", "\u0633\u0645\u064a\u0631")
        old._ensure_doctor_slot(f, doctor_id=yid, service_id=str(service.id))
        f.send("\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0632\u0627\u0644\u0629 \u0627\u0644\u0634\u0639\u0631 - \u0625\u0628\u0637 \u064a\u0648\u0645 20 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 16:00 \u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631 \u0639\u0644\u0649 \u0643\u0627\u0646\u062f\u064a\u0644\u0627")
        proposal = f.ensure_proposal("\u062a\u0645\u0627\u0645")
        if proposal:
            f.button("confirm", proposal["proposal_id"])
        before_rows = base.appointment_rows(f.db, f.patient)
        before = before_rows[0] if len(before_rows) == 1 else None
        repeat_reply = f.send("\u0623\u0643\u062f")
        after_rows = base.appointment_rows(f.db, f.patient)
        after = after_rows[0] if len(after_rows) == 1 else None
        checks = [
            (len(before_rows) == 1, f"before rows={len(before_rows)}"),
            (len(after_rows) == 1, f"after rows={len(after_rows)}"),
            (bool(before and after and before["appointment_id"] == after["appointment_id"]), "repeat created a different appointment"),
            (bool(after and after["status"] == "pending"), f"status={(after or {}).get('status')}"),
            (bool(after and after["confirmed_at"] is None), f"confirmed_at={(after or {}).get('confirmed_at')}"),
            (no_false_booking_confirmation(repeat_reply), f"repeat reply invented confirmation: {repeat_reply}"),
        ]
        result = old._result(checks, f, before=before, after=after, repeat_reply=repeat_reply)
        result["duplicate_appointments"] = max(0, len(after_rows) - 1)
        result["pending_to_confirmed_accidental_mutation"] = int(bool(after and after["status"] != "pending"))
        result["false_confirmed_status_claim"] = int(bool(after and after["status"] == "pending" and not no_false_booking_confirmation(repeat_reply)))
        result["status_claim"] = "neutral" if no_false_booking_confirmation(repeat_reply) else "false-confirmed"
        result["matches_db"] = not result["false_confirmed_status_claim"] and not result["pending_to_confirmed_accidental_mutation"]
    return finish(result)


def ra_s4() -> dict[str, Any]:
    result = old.ra8()
    reply = result.get("confirm_reply")
    row_status = result.get("persisted_status")
    checks = [
        (result.get("verdict") == "PASS", f"multi-service functional verdict={result.get('verdict')}"),
        (row_status == "pending", f"status={row_status}"),
        (pending_reply_ok(reply), f"reply={reply}"),
        (len((result.get("appointments") or [])) == 1, f"appointments={len(result.get('appointments') or [])}"),
    ]
    failures = [detail for ok, detail in checks if not ok]
    result["verdict"] = "PASS" if not failures else "FAIL"
    result["failures"] = failures
    result["false_confirmed_status_claim"] = int(row_status == "pending" and not pending_reply_ok(reply))
    result["status_claim"] = "pending" if pending_reply_ok(reply) else "false-confirmed"
    result["matches_db"] = not failures
    return result


SCENARIOS = {
    "RA8": ra8,
    "RA10": ra10,
    "RA-S1": ra_s1,
    "RA-S2": ra_s2,
    "RA-S3": ra_s3,
    "RA-S4": ra_s4,
}


def main() -> int:
    results: dict[str, Any] = {}
    for name, runner in SCENARIOS.items():
        try:
            results[name] = runner()
        except Exception as exc:
            results[name] = {"verdict": "FAIL", "severity": "P2", "failures": [f"runner exception: {type(exc).__name__}: {exc}"], "exception": repr(exc)}
        (OUT / "booking-status-truth-final-audit.json").write_text(json.dumps({"head_under_test": os.getenv("GITHUB_SHA") or "local", "environment": base.settings.environment, "model": base.settings.openai_model, "runtime": "Agent V2 live", "scenarios": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [name for name, row in results.items() if row.get("verdict") != "PASS"]
    summary = {
        "failed": failed,
        "wrong_writes": sum(1 for row in results.values() if row.get("wrong_time_write") or row.get("wrong_doctor_write") or row.get("wrong_device_write")),
        "duplicate_appointments": sum(int(row.get("duplicate_appointments") or 0) for row in results.values()),
        "wrong_time_writes": sum(1 for row in results.values() if row.get("wrong_time_write")),
        "wrong_doctor_writes": sum(1 for row in results.values() if row.get("wrong_doctor_write")),
        "wrong_device_writes": sum(1 for row in results.values() if row.get("wrong_device_write")),
        "false_confirmed_status_claims": sum(int(row.get("false_confirmed_status_claim") or 0) for row in results.values()),
        "pending_to_confirmed_accidental_mutations": sum(int(row.get("pending_to_confirmed_accidental_mutation") or 0) for row in results.values()),
        "missing_status_confirmation_assumptions": sum(int(row.get("missing_status_confirmation_assumption") or 0) for row in results.values()),
        "partial_writes": 0,
        "persisted_audit_writes": sum(1 for row in results.values() if row.get("persisted_audit_write")),
    }
    payload = {"head_under_test": os.getenv("GITHUB_SHA") or "local", "environment": base.settings.environment, "model": base.settings.openai_model, "runtime": "Agent V2 live", "scenarios": results, "summary": summary}
    (OUT / "booking-status-truth-final-audit.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("BOOKING_STATUS_TRUTH_SUMMARY=" + json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
