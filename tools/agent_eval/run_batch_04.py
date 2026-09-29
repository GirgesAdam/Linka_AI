from __future__ import annotations

import argparse
import base64
import json
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import Appointment
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.service import Service
from app.models.workspace import Workspace
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import (
    ScenarioResult,
    active_branch_id,
    assert_demo_only,
    jsonable,
    local_slot,
    send_turn,
    service_by_slug,
)
from tools.agent_eval.run_batch_01 import (
    created_appointments,
    doctor_name,
    quiet_patient,
)
from tools.agent_eval.run_batch_02 import (
    _apply_cost,
    _compatible_doctor_id,
    _seed_historical_appointment,
    _seed_history_conversation,
    laser_context,
)
from tools.agent_eval.run_batch_03 import (
    _appointment_by_id,
    _claim_reply_and_handback,
    _future_slot,
    _handback,
    _manual_staff_takeover,
    _package_booking_context,
    _pulse_safe,
    _run_messages,
    _seed_future_appointment,
    _seed_package_balance,
    _structured_ops,
    _usage_for_package,
    db_delta,
    extended_state_snapshot,
    make_result,
    summarize,
)
from tools.agent_eval.run_batch_03 import (
    run_case as _run_case,
)

ScenarioFn = Callable[[Session, Workspace], ScenarioResult]
BATCH_NUMBER = 4
SCENARIO_VERSION = "batch4-v1"
BATCH4_FIXTURE_VERSION = "batch4-demo-fixtures-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument("--output-dir", default="backend/eval_results")
    parser.add_argument("--git-sha", default=os.getenv("GITHUB_SHA") or "unknown")
    parser.add_argument("--batch4-base-sha", required=True)
    parser.add_argument("--input-price-per-million", type=float, required=True)
    parser.add_argument("--cached-input-price-per-million", type=float, required=True)
    parser.add_argument("--output-price-per-million", type=float, required=True)
    parser.add_argument("--cache-write-multiplier", type=float, default=1.0)
    parser.add_argument("--pricing-source", required=True)
    return parser.parse_args()


def require_explicit_demo_eval() -> None:
    if os.getenv("TIA_AGENT_EVAL_CONFIRM_DEMO") != "1":
        raise RuntimeError("Set TIA_AGENT_EVAL_CONFIRM_DEMO=1 to run the live Demo evaluation.")


def _age_conversation(db: Session, workspace: Workspace, conversation_id: UUID, *, days: int) -> None:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise RuntimeError("EVAL_INFRA_ERROR: conversation missing")
    rows = list(db.scalars(select(Message).where(
        Message.workspace_id == workspace.id,
        Message.conversation_id == conversation_id,
    )))
    delta = timedelta(days=days)
    for row in rows:
        row.created_at = row.created_at - delta
    if conversation.started_at:
        conversation.started_at = conversation.started_at - delta
    if conversation.last_message_at:
        conversation.last_message_at = conversation.last_message_at - delta
    if conversation.ownership_changed_at:
        conversation.ownership_changed_at = conversation.ownership_changed_at - delta
    db.flush()


def _availability_for(
    db: Session,
    workspace: Workspace,
    *,
    service: Service,
    doctor_id: UUID | None = None,
    device_key: str | None = None,
    after_date: date | None = None,
):
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    today = datetime.now(UTC).date()
    for offset in range(1, 60):
        day = today + timedelta(days=offset)
        if after_date is not None and day <= after_date:
            continue
        result = adapter.get_availability(AvailabilityRequest(
            branch_id=branch_id,
            service_id=str(service.id),
            booking_date=day,
            doctor_id=str(doctor_id) if doctor_id else None,
            laser_device_key=device_key,
        ))
        if result.slots:
            return result, result.slots[0]
    raise RuntimeError("EVAL_INFRA_ERROR: no suitable availability")


def _doctor_row(catalog: dict[str, Any], doctor_id: UUID | str) -> dict[str, Any]:
    target = str(doctor_id)
    return next(row for row in catalog.get("doctors", []) if str(row.get("id")) == target)


def _common_doctors(catalog: dict[str, Any], *service_ids: UUID) -> list[dict[str, Any]]:
    required = {str(value) for value in service_ids}
    return [
        row for row in catalog.get("doctors", [])
        if isinstance(row, dict)
        and row.get("id")
        and required.issubset({str(value) for value in (row.get("service_ids") or [])})
    ]


def _business_delta_empty(delta: dict[str, Any]) -> bool:
    for key in ("appointments", "packages", "package_usages", "pulse_packs", "payments", "pulse_usages", "pulse_settlements"):
        value = delta.get(key) or {}
        if value.get("created") or value.get("removed") or value.get("changed"):
            return False
    return not bool(delta.get("pulse_balance_delta"))


def _replacement_rows(snapshot: dict[str, Any], source_id: UUID | str) -> list[dict[str, Any]]:
    target = str(source_id)
    return [row for row in snapshot["appointments"] if row.get("rescheduled_from_appointment_id") == target]


def case_01_book_then_modify_after_gap(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    available, slot = _availability_for(db, workspace, service=service, after_date=datetime.now(UTC).date() + timedelta(days=2))
    catalog = build_clinic_catalog(db, workspace)
    doctor = _doctor_row(catalog, slot.doctor_id)
    source_date, source_time = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b4_01_book_then_modify_after_gap",
        [f"احجزيلي {service.name} يوم {source_date} الساعة {source_time} مع {doctor_name(doctor)}"],
    )
    mid = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, mid)
    if len(created) != 1:
        after = mid
        return make_result(
            scenario_id="b4_01_book_then_modify_after_gap", category="multi_day_lifecycle",
            purpose="A completed booking must remain actionable from canonical state after a real conversation gap.",
            turns=turns, before=before, after=after, verification={"created": created},
            deterministic_ok=False, expected="One booking is created before the simulated multi-day gap.",
            issue_title="Initial booking fixture did not create exactly one appointment",
        )
    source_id = created[0]["id"]
    _age_conversation(db, workspace, conversation_id, days=5)
    source = db.get(Appointment, UUID(source_id))
    target_av, target_slot = _future_slot(
        db, workspace, service_id=str(source.service_id), doctor_id=str(source.doctor_id),
        after_date=source.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
        exclude_appointment_id=source_id,
    )
    target_date, target_time = local_slot(target_av, target_slot)
    _, turn2 = send_turn(db, workspace, patient, "b4_01_book_then_modify_after_gap", 2, "عايزة أغير ميعاد الحجز اللي عملناه", conversation_id)
    _, turn3 = send_turn(db, workspace, patient, "b4_01_book_then_modify_after_gap", 3, f"خليه يوم {target_date} الساعة {target_time}", conversation_id)
    turns.extend([turn2, turn3])
    after = extended_state_snapshot(db, workspace, patient)
    replacements = _replacement_rows(after, source_id)
    original = _appointment_by_id(after, source_id)
    ok = original["status"] == "rescheduled" and len(replacements) == 1
    return make_result(
        scenario_id="b4_01_book_then_modify_after_gap", category="multi_day_lifecycle",
        purpose="Resolve a days-old booking from canonical upcoming appointments without relying on stale active-task state.",
        turns=turns, before=before, after=after,
        verification={"source_id": source_id, "source_date": source_date, "replacement_date": target_date, "replacements": replacements},
        deterministic_ok=ok,
        expected="After a five-day conversation gap, canonical appointment state resolves the original booking and only that appointment is rescheduled.",
        issue_title="Days-old booking could not be safely resolved from canonical state",
        issue_detail="The lifecycle must survive conversation gaps without stale active-task dependence.",
    )


def case_02_cancel_after_long_gap(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    available, slot = _availability_for(db, workspace, service=service, after_date=datetime.now(UTC).date() + timedelta(days=3))
    appointment = _seed_future_appointment(
        db, workspace, patient, service=service, doctor_id=UUID(str(slot.doctor_id)), slot=slot,
    )
    day, time_text = local_slot(available, slot)
    conversation = _seed_history_conversation(
        db, workspace, patient,
        [(f"احجزيلي {service.name} يوم {day} الساعة {time_text}", f"تم حجز {service.name} يوم {day} الساعة {time_text}.")],
    )
    before = extended_state_snapshot(db, workspace, patient)
    _, turn = send_turn(db, workspace, patient, "b4_02_cancel_after_long_gap", 1, "الغيه خلاص", conversation.id)
    after = extended_state_snapshot(db, workspace, patient)
    row = _appointment_by_id(after, appointment.id)
    ok = row["status"] == "cancelled" and turn.write_attempted and turn.write_result == "completed"
    return make_result(
        scenario_id="b4_02_cancel_after_long_gap", category="multi_day_lifecycle",
        purpose="A long-gap cancellation should use the single canonical upcoming appointment rather than stale conversational state.",
        turns=[turn], before=before, after=after,
        verification={"target_id": str(appointment.id), "appointment_after": row, "history_gap_days": 120},
        deterministic_ok=ok,
        expected="With exactly one actionable upcoming appointment, 'الغيه خلاص' cancels that canonical appointment after a long gap.",
        issue_severity="P1", issue_title="Long-gap cancellation lost or changed the canonical target",
    )


def case_03_completed_appointment_not_actionable(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    catalog = build_clinic_catalog(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    doctor_id = _compatible_doctor_id(catalog, service.id)
    branch_id = UUID(active_branch_id(catalog))
    completed = _seed_historical_appointment(
        db, workspace, patient, service=service, doctor_id=doctor_id, branch_id=branch_id,
        days_ago=6, status="completed",
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "b4_03_completed_appointment_not_actionable", ["غيري الميعاد"])
    after = extended_state_snapshot(db, workspace, patient)
    row = _appointment_by_id(after, completed.id)
    ok = row["status"] == "completed" and not turns[-1].write_attempted and _business_delta_empty(db_delta(before, after))
    return make_result(
        scenario_id="b4_03_completed_appointment_not_actionable", category="multi_day_lifecycle",
        purpose="Completed historical appointments must never remain actionable lifecycle targets.",
        turns=turns, before=before, after=after,
        verification={"completed_id": str(completed.id), "appointment_after": row},
        deterministic_ok=ok,
        expected="The completed appointment remains completed and no reschedule write occurs.",
        issue_severity="P1", issue_title="Completed appointment remained actionable",
    )


def case_04_financial_handoff_then_return_later(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b4_04_financial_handoff_then_return_later",
        ["فاضل عليا كام فلوس؟"],
    )
    staff_evidence = _claim_reply_and_handback(
        db, workspace, conversation_id, staff_text="راجعنا الحساب، ولو محتاجة تفاصيل أكتر كلمينا.",
    )
    _age_conversation(db, workspace, conversation_id, days=4)
    service = service_by_slug(db, workspace, "hydrafacial")
    available, slot = _availability_for(db, workspace, service=service, after_date=datetime.now(UTC).date() + timedelta(days=2))
    catalog = build_clinic_catalog(db, workspace)
    doctor = _doctor_row(catalog, slot.doctor_id)
    day, time_text = local_slot(available, slot)
    _, booking_turn = send_turn(
        db, workspace, patient, "b4_04_financial_handoff_then_return_later", 2,
        f"عايزة أحجز {service.name} يوم {day} الساعة {time_text} مع {doctor_name(doctor)}",
        conversation_id,
    )
    turns.append(booking_turn)
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    delta = db_delta(before, after)
    ok = (
        len(created) == 1
        and booking_turn.write_result == "completed"
        and not delta["payments"]["created"]
        and not delta["pulse_usages"]["created"]
        and not delta["pulse_settlements"]["created"]
        and staff_evidence.get("after_handback", {}).get("owner_type") == "ai"
    )
    return make_result(
        scenario_id="b4_04_financial_handoff_then_return_later", category="handoff_across_time",
        purpose="A financial handoff must not contaminate a fresh booking after handback and a multi-day gap.",
        turns=turns, before=before, after=after,
        verification={"staff_evidence": staff_evidence, "created": created},
        deterministic_ok=ok,
        expected="After handback and a four-day gap, the fresh booking succeeds with no stale financial mutation.",
        issue_severity="P0" if delta["payments"]["created"] or delta["pulse_settlements"]["created"] else "P1",
        issue_title="Stale financial handoff leaked into a later booking",
    )


def case_05_reception_modifies_then_handback(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _available, slot = _availability_for(db, workspace, service=service, after_date=datetime.now(UTC).date() + timedelta(days=2))
    appointment = _seed_future_appointment(db, workspace, patient, service=service, doctor_id=UUID(str(slot.doctor_id)), slot=slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(db, workspace, patient, "b4_05_reception_modifies_then_handback", ["هاي"])
    handoff, staff, ownership = _manual_staff_takeover(
        db, workspace, patient, conversation_id, staff_text="تمام، هعدّل الميعاد من الريسبشن.",
    )
    target_av, target_slot = _future_slot(
        db, workspace, service_id=str(service.id), doctor_id=str(appointment.doctor_id),
        after_date=appointment.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
        exclude_appointment_id=str(appointment.id),
    )
    appointment.start_at = target_slot.start_at
    appointment.end_at = target_slot.end_at
    appointment.busy_start_at = target_slot.start_at
    appointment.busy_end_at = target_slot.end_at
    appointment.duration_minutes = target_slot.duration_minutes
    db.flush()
    target_date, target_time = local_slot(target_av, target_slot)
    handback = _handback(db, workspace, conversation_id, handoff, staff)
    _, customer_turn = send_turn(
        db, workspace, patient, "b4_05_reception_modifies_then_handback", 2, "ميعادي بقى امتى؟", conversation_id,
    )
    turns.append(customer_turn)
    after = extended_state_snapshot(db, workspace, patient)
    row = _appointment_by_id(after, appointment.id)
    ok = (
        handback.get("owner_type") == "ai"
        and "appointments" in customer_turn.verified_reads
        and not customer_turn.write_attempted
        and row["start_at"] == target_slot.start_at.isoformat()
    )
    return make_result(
        scenario_id="b4_05_reception_modifies_then_handback", category="handoff_across_time",
        purpose="After Reception edits an appointment, the Agent must read the new canonical DB truth after handback.",
        turns=turns, before=before, after=after,
        verification={"ownership": ownership, "handback": handback, "target_date": target_date, "target_time": target_time, "appointment_after": row},
        deterministic_ok=ok,
        expected="The Agent answers from the Reception-updated appointment and performs no stale-state write.",
        issue_severity="P1", issue_title="Pre-handoff appointment state overrode canonical DB truth",
    )


def case_06_multiple_messages_while_human_owns(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    before = extended_state_snapshot(db, workspace, patient)
    starter_turns, conversation_id = _run_messages(db, workspace, patient, "b4_06_multiple_messages_while_human_owns", ["هاي"])
    handoff, staff, ownership = _manual_staff_takeover(db, workspace, patient, conversation_id, staff_text="أنا معاك من الريسبشن.")
    human_turns = []
    for index, message in enumerate(("ممكن أغير ميعادي؟", "وخليه الجمعة", "ولو ينفع بعد الضهر"), start=2):
        _, turn = send_turn(db, workspace, patient, "b4_06_multiple_messages_while_human_owns", index, message, conversation_id)
        human_turns.append(turn)
    handback = _handback(db, workspace, conversation_id, handoff, staff)
    _, fresh_turn = send_turn(
        db, workspace, patient, "b4_06_multiple_messages_while_human_owns", 5,
        "جلسة الهيدرافيشل بكام؟", conversation_id,
    )
    turns = starter_turns + human_turns + [fresh_turn]
    after = extended_state_snapshot(db, workspace, patient)
    silent = all(turn.agent_response is None and not turn.write_attempted for turn in human_turns)
    ok = silent and handback.get("owner_type") == "ai" and fresh_turn.agent_response is not None and not fresh_turn.write_attempted
    return make_result(
        scenario_id="b4_06_multiple_messages_while_human_owns", category="handoff_across_time",
        purpose="Several customer messages during human ownership must remain AI-silent and must not replay after handback.",
        turns=turns, before=before, after=after,
        verification={"ownership": ownership, "human_turns_silent": silent, "handback": handback},
        deterministic_ok=ok,
        expected="All human-owned inbound turns are silent/no-write; after handback only the fresh price question is answered.",
        issue_severity="P0" if any(turn.write_attempted for turn in human_turns) else "P1",
        issue_title="AI replied or wrote while Reception owned the conversation",
        handoff_ok=ok,
    )


def case_07_package_reschedule_cancel_chain(db: Session, workspace: Workspace) -> ScenarioResult:
    patient, service, doctor, _, _, date_text, time_text = _package_booking_context(db, workspace)
    package = _seed_package_balance(db, workspace, patient, service, remaining=2, name="Batch4 lifecycle chain")
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b4_07_package_reschedule_cancel_chain",
        [f"احجزيلي {service.name} من الباكدج يوم {date_text} الساعة {time_text} مع {doctor_name(doctor)}"],
    )
    after_book = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after_book)
    if len(created) != 1:
        return make_result(
            scenario_id="b4_07_package_reschedule_cancel_chain", category="package_lifecycle",
            purpose="Package booking → reschedule → cancel must preserve a single entitlement lifecycle.",
            turns=turns, before=before, after=after_book, verification={"created": created},
            deterministic_ok=False, expected="Initial package booking creates one appointment and one reservation.",
            issue_title="Package lifecycle chain could not establish one booking",
        )
    original_id = created[0]["id"]
    original = db.get(Appointment, UUID(original_id))
    target_av, target_slot = _future_slot(
        db, workspace, service_id=str(original.service_id), doctor_id=str(original.doctor_id),
        after_date=original.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
        exclude_appointment_id=original_id,
    )
    target_date, target_time = local_slot(target_av, target_slot)
    _, reschedule_turn = send_turn(
        db, workspace, patient, "b4_07_package_reschedule_cancel_chain", 2,
        f"غيريه ليوم {target_date} الساعة {target_time}", conversation_id,
    )
    turns.append(reschedule_turn)
    after_reschedule = extended_state_snapshot(db, workspace, patient)
    replacements = _replacement_rows(after_reschedule, original_id)
    _, cancel_turn = send_turn(
        db, workspace, patient, "b4_07_package_reschedule_cancel_chain", 3,
        "تمام، الغيه خلاص", conversation_id,
    )
    turns.append(cancel_turn)
    after = extended_state_snapshot(db, workspace, patient)
    package_after = next(row for row in after["packages"] if row["id"] == str(package.id))
    usages = _usage_for_package(after, package.id)
    replacements_after = _replacement_rows(after, original_id)
    delta = db_delta(before, after)
    canonical_handoff = (
        cancel_turn.write_result == "handoff"
        and len(replacements_after) == 1
        and replacements_after[0]["status"] == "confirmed"
        and package_after["remaining"] == 1
        and len(usages) == 1
        and usages[0]["status"] == "reserved"
    )
    direct_safe_cancel = (
        len(replacements_after) == 1
        and replacements_after[0]["status"] == "cancelled"
        and package_after["remaining"] == 2
        and len(usages) == 1
        and usages[0]["status"] == "released"
    )
    ok = (
        len(replacements) == 1
        and (canonical_handoff or direct_safe_cancel)
        and _pulse_safe(delta)
    )
    return make_result(
        scenario_id="b4_07_package_reschedule_cancel_chain", category="package_lifecycle",
        purpose="Exercise one package reservation through booking, reschedule, and a cancellation request without double consume/refund.",
        turns=turns, before=before, after=after,
        verification={
            "package_id": str(package.id),
            "original_id": original_id,
            "replacement_after": replacements_after,
            "package_after": package_after,
            "usages": usages,
            "canonical_financial_handoff": canonical_handoff,
            "direct_safe_cancel": direct_safe_cancel,
        },
        deterministic_ok=ok,
        expected="Package identity stays single through reschedule; cancellation either follows the current staff-handoff policy without mutation or releases exactly once if directly allowed.",
        issue_severity="P0" if not _pulse_safe(delta) else "P1",
        issue_title="Package reservation lifecycle double-mutated or lost identity",
    )


def case_08_package_then_nonpackage_service(db: Session, workspace: Workspace) -> ScenarioResult:
    patient, package_service, package_doctor, package_av, package_slot, date_text, time_text = _package_booking_context(db, workspace)
    package = _seed_package_balance(db, workspace, patient, package_service, remaining=2, name="Batch4 eligible only")
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b4_08_package_then_nonpackage_service",
        [f"احجزيلي {package_service.name} من الباكدج يوم {date_text} الساعة {time_text} مع {doctor_name(package_doctor)}"],
    )
    hydra = service_by_slug(db, workspace, "hydrafacial")
    package_day = package_slot.start_at.astimezone(ZoneInfo(package_av.timezone)).date()
    hydra_av, hydra_slot = _availability_for(db, workspace, service=hydra, after_date=package_day)
    catalog = build_clinic_catalog(db, workspace)
    hydra_doctor = _doctor_row(catalog, hydra_slot.doctor_id)
    hydra_date, hydra_time = local_slot(hydra_av, hydra_slot)
    _, second_turn = send_turn(
        db, workspace, patient, "b4_08_package_then_nonpackage_service", 2,
        f"وكمان احجزيلي {hydra.name} يوم {hydra_date} الساعة {hydra_time} مع {doctor_name(hydra_doctor)}",
        conversation_id,
    )
    turns.append(second_turn)
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    package_rows = [row for row in created if row["service_id"] == str(package_service.id)]
    standard_rows = [row for row in created if row["service_id"] == str(hydra.id)]
    usages = _usage_for_package(after, package.id)
    delta = db_delta(before, after)
    ok = (
        len(package_rows) == 1
        and package_rows[0]["patient_package_id"] == str(package.id)
        and package_rows[0]["billing_context"] == "package_prepaid"
        and len(standard_rows) == 1
        and standard_rows[0]["patient_package_id"] is None
        and standard_rows[0]["billing_context"] == "standard"
        and len(usages) == 1
        and _pulse_safe(delta)
    )
    return make_result(
        scenario_id="b4_08_package_then_nonpackage_service", category="package_lifecycle",
        purpose="An existing package may cover Service A but must never leak onto a later Service B booking.",
        turns=turns, before=before, after=after,
        verification={"package_id": str(package.id), "created": created, "usages": usages},
        deterministic_ok=ok,
        expected="Only the eligible PRP booking consumes the package; Hydrafacial remains standard with no Agent financial settlement.",
        issue_severity="P0" if not _pulse_safe(delta) else "P1",
        issue_title="Package coverage leaked to an ineligible service",
    )


def case_09_exhausted_package_after_gap(db: Session, workspace: Workspace) -> ScenarioResult:
    patient, service, doctor, _, _, date_text, time_text = _package_booking_context(db, workspace)
    package = _seed_package_balance(db, workspace, patient, service, remaining=0, name="Batch4 exhausted canonical")
    conversation = _seed_history_conversation(
        db, workspace, patient,
        [("فاضللي كام جلسة في الباكدج؟", "فاضلك جلسة واحدة في الباكدج.")],
    )
    before = extended_state_snapshot(db, workspace, patient)
    _, turn = send_turn(
        db, workspace, patient, "b4_09_exhausted_package_after_gap", 1,
        f"احجزيلي {service.name} من الباكدج يوم {date_text} الساعة {time_text} مع {doctor_name(doctor)}",
        conversation.id,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    package_after = next(row for row in after["packages"] if row["id"] == str(package.id))
    ok = not created and package_after["remaining"] == 0 and turn.write_result in {"blocked", None}
    return make_result(
        scenario_id="b4_09_exhausted_package_after_gap", category="package_lifecycle",
        purpose="Canonical zero balance must beat a stale historical conversation that claimed one package session remained.",
        turns=[turn], before=before, after=after,
        verification={"package_id": str(package.id), "package_after": package_after, "created": created, "stale_claim": 1},
        deterministic_ok=ok,
        expected="DB truth wins: no package-backed booking is created when canonical remaining sessions are zero.",
        issue_severity="P1", issue_title="Stale package balance overrode canonical DB truth",
    )


def case_10_two_appointments_one_package(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    laser_service = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    _, _, laser_doctor, _laser_av, _ = laser_context(
        db, workspace, service_slug="laser-hair-removal-underarm", device_key="candela_gentle",
    )
    laser_target_av, laser_slot = _future_slot(
        db, workspace, service_id=str(laser_service.id), doctor_id=str(laser_doctor["id"]),
        device_key="candela_gentle", after_date=datetime.now(UTC).date() + timedelta(days=2),
    )
    laser = _seed_future_appointment(
        db, workspace, patient, service=laser_service, doctor_id=UUID(str(laser_doctor["id"])),
        slot=laser_slot, device_key="candela_gentle",
    )
    hydra = service_by_slug(db, workspace, "hydrafacial")
    _hydra_av, hydra_slot = _availability_for(
        db, workspace, service=hydra,
        after_date=laser_slot.start_at.astimezone(ZoneInfo(laser_target_av.timezone)).date(),
    )
    hydra_appt = _seed_future_appointment(
        db, workspace, patient, service=hydra, doctor_id=UUID(str(hydra_slot.doctor_id)), slot=hydra_slot,
    )
    package_service = service_by_slug(db, workspace, "prp-skin")
    package = _seed_package_balance(db, workspace, patient, package_service, remaining=2, name="Batch4 unrelated package")
    hydra_day = hydra_appt.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date()
    target_av, target_slot = _future_slot(
        db, workspace, service_id=str(laser_service.id), doctor_id=str(laser.doctor_id),
        device_key="candela_gentle",
        after_date=hydra_day,
        exclude_appointment_id=str(laser.id),
    )
    target_date, target_time = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db, workspace, patient, "b4_10_two_appointments_one_package",
        [f"غيري ميعاد الليزر وخلي جلسة البشرة زي ما هي، خلي الليزر يوم {target_date} الساعة {target_time}"],
    )
    after = extended_state_snapshot(db, workspace, patient)
    laser_after = _appointment_by_id(after, laser.id)
    hydra_after = _appointment_by_id(after, hydra_appt.id)
    replacements = _replacement_rows(after, laser.id)
    package_after = next(row for row in after["packages"] if row["id"] == str(package.id))
    ok = (
        laser_after["status"] == "rescheduled"
        and len(replacements) == 1
        and hydra_after["status"] == "confirmed"
        and package_after["remaining"] == 2
    )
    return make_result(
        scenario_id="b4_10_two_appointments_one_package", category="concurrent_lifecycles",
        purpose="Two upcoming appointments plus an unrelated package must not widen lifecycle scope.",
        turns=turns, before=before, after=after,
        verification={"laser_id": str(laser.id), "skin_id": str(hydra_appt.id), "package_id": str(package.id), "replacements": replacements},
        deterministic_ok=ok,
        expected="Only the laser appointment changes; the skin appointment and unrelated package remain untouched.",
        issue_severity="P1", issue_title="Concurrent lifecycle request changed the wrong appointment or package",
    )


def case_11_cancel_one_then_modify_other(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    laser_service = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    _, _, laser_doctor, _, _ = laser_context(
        db, workspace, service_slug="laser-hair-removal-underarm", device_key="candela_gentle",
    )
    laser_av, laser_slot = _future_slot(
        db, workspace, service_id=str(laser_service.id), doctor_id=str(laser_doctor["id"]),
        device_key="candela_gentle", after_date=datetime.now(UTC).date() + timedelta(days=3),
    )
    laser = _seed_future_appointment(
        db, workspace, patient, service=laser_service, doctor_id=UUID(str(laser_doctor["id"])),
        slot=laser_slot, device_key="candela_gentle",
    )
    hydra = service_by_slug(db, workspace, "hydrafacial")
    _hydra_av, hydra_slot = _availability_for(
        db, workspace, service=hydra,
        after_date=laser_slot.start_at.astimezone(ZoneInfo(laser_av.timezone)).date(),
    )
    hydra_appt = _seed_future_appointment(
        db, workspace, patient, service=hydra, doctor_id=UUID(str(hydra_slot.doctor_id)), slot=hydra_slot,
    )
    laser_date, _ = local_slot(laser_av, laser_slot)
    target_av, target_slot = _future_slot(
        db, workspace, service_id=str(hydra.id), doctor_id=str(hydra_appt.doctor_id),
        after_date=hydra_appt.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
        exclude_appointment_id=str(hydra_appt.id),
    )
    target_date, target_time = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b4_11_cancel_one_then_modify_other",
        [f"الغِي ميعاد الليزر يوم {laser_date}"],
    )
    _, second_turn = send_turn(
        db, workspace, patient, "b4_11_cancel_one_then_modify_other", 2,
        f"والتاني خليه يوم {target_date} الساعة {target_time}", conversation_id,
    )
    turns.append(second_turn)
    after = extended_state_snapshot(db, workspace, patient)
    laser_after = _appointment_by_id(after, laser.id)
    hydra_after = _appointment_by_id(after, hydra_appt.id)
    replacements = _replacement_rows(after, hydra_appt.id)
    ok = laser_after["status"] == "cancelled" and hydra_after["status"] == "rescheduled" and len(replacements) == 1
    return make_result(
        scenario_id="b4_11_cancel_one_then_modify_other", category="concurrent_lifecycles",
        purpose="After cancelling appointment A, a follow-up about the other appointment must bind to B rather than reuse the cancelled target.",
        turns=turns, before=before, after=after,
        verification={"cancelled_id": str(laser.id), "other_id": str(hydra_appt.id), "replacements": replacements},
        deterministic_ok=ok,
        expected="Laser is cancelled exactly once; the other appointment is then rescheduled without reviving the cancelled target.",
        issue_severity="P1", issue_title="Cancelled appointment leaked into the next lifecycle action",
    )


def case_12_same_service_different_dates_devices(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    _, _, candela_doctor, _, _ = laser_context(
        db, workspace, service_slug="laser-hair-removal-underarm", device_key="candela_gentle",
    )
    candela_av, candela_slot = _future_slot(
        db, workspace, service_id=str(service.id), doctor_id=str(candela_doctor["id"]),
        device_key="candela_gentle", after_date=datetime.now(UTC).date() + timedelta(days=2),
    )
    candela = _seed_future_appointment(
        db, workspace, patient, service=service, doctor_id=UUID(str(candela_doctor["id"])),
        slot=candela_slot, device_key="candela_gentle",
    )
    _, _, prime_doctor, _, _ = laser_context(
        db, workspace, service_slug="laser-hair-removal-underarm", device_key="prime_lase",
    )
    candela_day = candela_slot.start_at.astimezone(ZoneInfo(candela_av.timezone)).date()
    prime_av, prime_slot = _future_slot(
        db, workspace, service_id=str(service.id), doctor_id=str(prime_doctor["id"]),
        device_key="prime_lase", after_date=candela_day,
    )
    prime = _seed_future_appointment(
        db, workspace, patient, service=service, doctor_id=UUID(str(prime_doctor["id"])),
        slot=prime_slot, device_key="prime_lase",
    )
    prime_date, _ = local_slot(prime_av, prime_slot)
    target_av, target_slot = _future_slot(
        db, workspace, service_id=str(service.id), doctor_id=str(prime.doctor_id),
        device_key="prime_lase",
        after_date=prime.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
        exclude_appointment_id=str(prime.id),
    )
    target_date, target_time = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db, workspace, patient, "b4_12_same_service_different_dates_devices",
        [f"غيري ميعاد الليزر بتاع Prime Lase يوم {prime_date} وخليه يوم {target_date} الساعة {target_time}"],
    )
    after = extended_state_snapshot(db, workspace, patient)
    candela_after = _appointment_by_id(after, candela.id)
    prime_after = _appointment_by_id(after, prime.id)
    replacements = _replacement_rows(after, prime.id)
    ok = (
        candela_after["status"] == "confirmed"
        and prime_after["status"] == "rescheduled"
        and len(replacements) == 1
        and replacements[0]["laser_device_key"] == "prime_lase"
    )
    return make_result(
        scenario_id="b4_12_same_service_different_dates_devices", category="concurrent_lifecycles",
        purpose="Date plus device must deterministically identify one of two same-service laser appointments.",
        turns=turns, before=before, after=after,
        verification={"candela_id": str(candela.id), "prime_id": str(prime.id), "replacements": replacements},
        deterministic_ok=ok,
        expected="Only the Prime Lase appointment on the referenced date is rescheduled; Candela remains untouched.",
        issue_severity="P1", issue_title="Date/device reference selected the wrong same-service appointment",
    )


def _common_day_availability(db: Session, workspace: Workspace, services: list[Service], *, doctor_id: UUID | None = None):
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    today = datetime.now(UTC).date()
    for offset in range(2, 55):
        day = today + timedelta(days=offset)
        results = {}
        for service in services:
            result = adapter.get_availability(AvailabilityRequest(
                branch_id=branch_id,
                service_id=str(service.id),
                booking_date=day,
                doctor_id=str(doctor_id) if doctor_id else None,
            ))
            if not result.slots:
                break
            results[str(service.id)] = result
        if len(results) == len(services):
            return day, results
    raise RuntimeError("EVAL_INFRA_ERROR: no common availability day")


def case_13_price_availability_existing_package(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "prp-skin")
    package = _seed_package_balance(db, workspace, patient, service, remaining=2, name="Batch4 compound info")
    available, slot = _availability_for(db, workspace, service=service, after_date=datetime.now(UTC).date() + timedelta(days=2))
    day, _ = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db, workspace, patient, "b4_13_price_availability_existing_package",
        [f"جلسة الـPRP للبشرة بكام، وعندي باكدج تنفع لها؟ ولو تنفع في ميعاد يوم {day}؟"],
    )
    after = extended_state_snapshot(db, workspace, patient)
    turn = turns[-1]
    reads = set(turn.verified_reads)
    delta = db_delta(before, after)
    ops = {op.get("type") for op in _structured_ops(turn)}
    ok = (
        not turn.write_attempted
        and _business_delta_empty(delta)
        and {"service_catalog", "customer_packages", "availability"}.issubset(reads)
        and "pricing" in ops
        and ("package_info" in ops or "package_information" in ops)
    )
    return make_result(
        scenario_id="b4_13_price_availability_existing_package", category="multi_intent",
        purpose="One informational turn combines canonical price, existing-package eligibility, and availability without authorizing payment or booking.",
        turns=turns, before=before, after=after,
        verification={"package_id": str(package.id), "verified_reads": sorted(reads), "operations": sorted(ops), "query_date": day},
        deterministic_ok=ok,
        expected="Price, package eligibility, and availability are grounded in verified reads with zero booking/payment/Pulse mutation.",
        issue_severity="P0" if not _business_delta_empty(delta) else "P2",
        issue_title="Compound informational query widened into an ungrounded or mutating action",
    )


def case_14_change_service_after_availability(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    hydra = service_by_slug(db, workspace, "hydrafacial")
    prp = service_by_slug(db, workspace, "prp-skin")
    day, availability = _common_day_availability(db, workspace, [hydra, prp])
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b4_14_change_service_after_availability",
        [f"عايزة أحجز {hydra.name} يوم {day.isoformat()}"],
    )
    _, changed_turn = send_turn(
        db, workspace, patient, "b4_14_change_service_after_availability", 2,
        f"لا خليها {prp.name}", conversation_id,
    )
    turns.append(changed_turn)
    after = extended_state_snapshot(db, workspace, patient)
    ops = _structured_ops(changed_turn)
    active = None
    if changed_turn.structured_trace:
        active = changed_turn.structured_trace[-1].get("active_task")
    active_service = ((active or {}).get("constraints") or {}).get("service_id")
    ok = (
        "availability" in changed_turn.verified_reads
        and not changed_turn.write_attempted
        and active_service == str(prp.id)
        and _business_delta_empty(db_delta(before, after))
    )
    return make_result(
        scenario_id="b4_14_change_service_after_availability", category="multi_intent",
        purpose="Changing service after availability was already found must invalidate the old service assumptions and revalidate the new service.",
        turns=turns, before=before, after=after,
        verification={
            "hydrafacial_service_id": str(hydra.id),
            "prp_service_id": str(prp.id),
            "common_day": day.isoformat(),
            "changed_turn_operations": ops,
            "active_task_after_change": active,
            "prp_slots_on_day": len(availability[str(prp.id)].slots),
        },
        deterministic_ok=ok,
        expected="Hydrafacial availability is not reused as PRP availability; the PRP service is re-read/revalidated and no booking occurs yet.",
        issue_severity="P1" if changed_turn.write_attempted else "P2",
        issue_title="Service change reused stale availability assumptions",
    )


def case_15_incremental_doctor_date_service_corrections(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    prp = service_by_slug(db, workspace, "prp-skin")
    hydra = service_by_slug(db, workspace, "hydrafacial")
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)

    def scheduled_here(row: dict[str, Any]) -> bool:
        scheduled = {
            str(value)
            for value in (row.get("scheduled_branch_ids") or row.get("branch_ids") or [])
            if value
        }
        return not scheduled or branch_id in scheduled

    common = [
        row for row in _common_doctors(catalog, prp.id, hydra.id)
        if scheduled_here(row)
    ]
    if not common:
        raise RuntimeError("EVAL_INFRA_ERROR: no branch-scheduled doctor common to PRP and Hydrafacial")
    final_doctor = common[0]
    prp_doctors = [
        row for row in catalog.get("doctors", [])
        if row.get("id")
        and str(prp.id) in {str(value) for value in (row.get("service_ids") or [])}
        and scheduled_here(row)
        and str(row.get("id")) != str(final_doctor["id"])
    ]
    initial_doctor = prp_doctors[0] if prp_doctors else final_doctor
    final_day, common_av = _common_day_availability(
        db, workspace, [prp, hydra], doctor_id=UUID(str(final_doctor["id"])),
    )
    initial_av, initial_slot = _availability_for(
        db, workspace, service=prp, doctor_id=UUID(str(initial_doctor["id"])),
        after_date=datetime.now(UTC).date() + timedelta(days=1),
    )
    initial_day, _ = local_slot(initial_av, initial_slot)
    hydra_final_av = common_av[str(hydra.id)]
    final_slot = hydra_final_av.slots[0]
    final_time = local_slot(hydra_final_av, final_slot)[1]
    before = extended_state_snapshot(db, workspace, patient)
    messages = [
        f"عايزة أحجز {prp.name}",
        f"يوم {initial_day}",
        f"لا خليه يوم {final_day.isoformat()}",
        f"مع {doctor_name(initial_doctor)}",
        f"لا مع {doctor_name(final_doctor)}",
        f"بدل {prp.name} خليها {hydra.name}",
        f"الساعة {final_time}",
    ]
    turns, _ = _run_messages(db, workspace, patient, "b4_15_incremental_doctor_date_service_corrections", messages)
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    if len(created) == 1:
        row = created[0]
        local_start = datetime.fromisoformat(row["start_at"]).astimezone(ZoneInfo(workspace.timezone or "UTC"))
        final_ok = (
            row["service_id"] == str(hydra.id)
            and row["doctor_id"] == str(final_doctor["id"])
            and local_start.date() == final_day
            and local_start.strftime("%H:%M") == final_time
        )
    else:
        final_ok = False
    ok = final_ok and len(created) == 1
    return make_result(
        scenario_id="b4_15_incremental_doctor_date_service_corrections", category="multi_intent",
        purpose="Natural corrections across date, doctor, and service must converge to only the latest explicit values.",
        turns=turns, before=before, after=after,
        verification={
            "initial_date": initial_day,
            "final_date": final_day.isoformat(),
            "initial_doctor_id": str(initial_doctor["id"]),
            "final_doctor_id": str(final_doctor["id"]),
            "initial_service_id": str(prp.id),
            "final_service_id": str(hydra.id),
            "final_time": final_time,
            "created": created,
        },
        deterministic_ok=ok,
        expected="Exactly one final Hydrafacial booking reflects the latest date, doctor, service, and time; superseded values do not leak.",
        issue_severity="P1", issue_title="Superseded booking constraints leaked into the final write",
    )


def case_16_repeat_reschedule_confirmation(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    source_av, source_slot = _availability_for(db, workspace, service=service, after_date=datetime.now(UTC).date() + timedelta(days=2))
    source = _seed_future_appointment(
        db, workspace, patient, service=service, doctor_id=UUID(str(source_slot.doctor_id)), slot=source_slot,
    )
    source_date, _ = local_slot(source_av, source_slot)
    target_av, target_slot = _future_slot(
        db, workspace, service_id=str(service.id), doctor_id=str(source.doctor_id),
        after_date=source.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
        exclude_appointment_id=str(source.id),
    )
    target_date, target_time = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b4_16_repeat_reschedule_confirmation",
        [f"غيري ميعاد {service.name} يوم {source_date} وخليه يوم {target_date} الساعة {target_time}"],
    )
    _, ack_turn = send_turn(
        db, workspace, patient, "b4_16_repeat_reschedule_confirmation", 2,
        "تمام كده خليها على الميعاد الجديد", conversation_id,
    )
    turns.append(ack_turn)
    after = extended_state_snapshot(db, workspace, patient)
    replacements = _replacement_rows(after, source.id)
    ok = (
        _appointment_by_id(after, source.id)["status"] == "rescheduled"
        and len(replacements) == 1
        and not ack_turn.write_attempted
    )
    return make_result(
        scenario_id="b4_16_repeat_reschedule_confirmation", category="idempotency",
        purpose="A conversational acknowledgment after a successful reschedule must not execute a second reschedule.",
        turns=turns, before=before, after=after,
        verification={"source_id": str(source.id), "replacements": replacements, "ack_write_attempted": ack_turn.write_attempted},
        deterministic_ok=ok,
        expected="The first turn reschedules once; the follow-up is acknowledgment-only with no additional appointment write.",
        issue_severity="P1", issue_title="Reschedule acknowledgment caused a duplicate lifecycle write",
    )


def case_17_repeat_package_booking_confirmation(db: Session, workspace: Workspace) -> ScenarioResult:
    patient, service, doctor, _, _, date_text, time_text = _package_booking_context(db, workspace)
    package = _seed_package_balance(db, workspace, patient, service, remaining=2, name="Batch4 idempotent package")
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b4_17_repeat_package_booking_confirmation",
        [f"احجزيلي {service.name} من الباكدج يوم {date_text} الساعة {time_text} مع {doctor_name(doctor)}"],
    )
    _, ack_turn = send_turn(
        db, workspace, patient, "b4_17_repeat_package_booking_confirmation", 2,
        "اه تمام احجزيه كده", conversation_id,
    )
    turns.append(ack_turn)
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    usages = _usage_for_package(after, package.id)
    package_after = next(row for row in after["packages"] if row["id"] == str(package.id))
    ok = (
        len(created) == 1
        and len(usages) == 1
        and usages[0]["status"] == "reserved"
        and package_after["remaining"] == 1
        and not ack_turn.write_attempted
    )
    return make_result(
        scenario_id="b4_17_repeat_package_booking_confirmation", category="idempotency",
        purpose="Repeating conversational confirmation after a successful package booking must not duplicate the appointment or PackageUsage.",
        turns=turns, before=before, after=after,
        verification={"package_id": str(package.id), "created": created, "usages": usages, "package_after": package_after, "ack_write_attempted": ack_turn.write_attempted},
        deterministic_ok=ok,
        expected="Exactly one package-backed appointment and one reserved usage remain after the repeated confirmation.",
        issue_severity="P1", issue_title="Repeated package-booking confirmation duplicated a write",
    )


CASES: list[ScenarioFn] = [
    case_01_book_then_modify_after_gap,
    case_02_cancel_after_long_gap,
    case_03_completed_appointment_not_actionable,
    case_04_financial_handoff_then_return_later,
    case_05_reception_modifies_then_handback,
    case_06_multiple_messages_while_human_owns,
    case_07_package_reschedule_cancel_chain,
    case_08_package_then_nonpackage_service,
    case_09_exhausted_package_after_gap,
    case_10_two_appointments_one_package,
    case_11_cancel_one_then_modify_other,
    case_12_same_service_different_dates_devices,
    case_13_price_availability_existing_package,
    case_14_change_service_after_availability,
    case_15_incremental_doctor_date_service_corrections,
    case_16_repeat_reschedule_confirmation,
    case_17_repeat_package_booking_confirmation,
]


def _write_reports(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# Linka Agent Evaluation — Batch 04 Raw Baseline",
        "",
        f"- Batch runtime base SHA: {payload['run_metadata']['batch4_base_sha']}",
        f"- Harness SHA: {payload['run_metadata']['git_sha']}",
        f"- Scenario version: {payload['run_metadata']['scenario_version']}",
        f"- Model: {payload['run_metadata']['model']}",
        f"- Reasoning: {payload['run_metadata']['reasoning_effort']}",
        f"- Scenarios executed: {len(payload['scenario_results'])}",
        "",
        "Raw deterministic findings are guards; final conversational verdict requires manual review.",
        "",
    ]
    for row in payload["scenario_results"]:
        lines.extend([
            f"## {row['id']}",
            "",
            f"Category: {row['category']}",
            f"Purpose: {row['purpose']}",
            f"Review status: {row['review']['status']}",
            f"Expected: {row['review']['expected']}",
            "",
        ])
        for turn in row["turns"]:
            lines.append(f"Customer {turn['turn_number']}: {turn['user_message']}")
            lines.append(f"Linka: {turn['agent_response']}")
            lines.append(
                "Usage: "
                f"in={turn['token_usage']['input_tokens']} "
                f"read={turn['token_usage']['cached_tokens']} "
                f"write={turn['token_usage']['cache_write_tokens']} "
                f"uncached={turn['token_usage']['uncached_input_tokens']} "
                f"out={turn['token_usage']['output_tokens']} "
                f"latency={turn['latency_ms']}ms"
            )
            lines.append("")
        lines.append("DB verification:")
        lines.append(json.dumps(row["db_verification"], ensure_ascii=False, indent=2))
        if row["issues"]:
            lines.append("Deterministic findings:")
            for issue in row["issues"]:
                lines.append(f"- {issue['severity']}: {issue['title']} — {issue['detail']}")
        else:
            lines.append("Deterministic findings: none; manual review still required.")
        lines.append("")
    lines.extend(["## Batch summary", "", json.dumps(payload["batch_summary"], ensure_ascii=False, indent=2)])
    md_path.write_text("\n".join(lines), encoding="utf-8")


def _emit_compact(payload: dict[str, Any]) -> None:
    for row in payload["scenario_results"]:
        compact = {
            "id": row["id"],
            "category": row["category"],
            "execution_error": row.get("execution_error"),
            "issues": row.get("issues") or [],
            "token_usage": row.get("token_usage") or {},
            "cost": row.get("cost") or {},
            "turns": [
                {
                    "turn_number": turn["turn_number"],
                    "user_message": turn["user_message"],
                    "agent_response": turn.get("agent_response"),
                    "verified_reads": turn.get("verified_reads") or [],
                    "write_attempted": turn.get("write_attempted"),
                    "write_result": turn.get("write_result"),
                    "handoff_state": turn.get("handoff_state"),
                    "runtime_state": turn.get("runtime_state") or {},
                    "structured_operations": [
                        operation
                        for trace in (turn.get("structured_trace") or [])
                        for operation in ((trace.get("understanding") or {}).get("operations") or [])
                    ],
                    "plan_reads": [
                        read
                        for trace in (turn.get("structured_trace") or [])
                        for step in ((trace.get("plan") or {}).get("steps") or [])
                        for read in (step.get("reads") or [])
                    ],
                }
                for turn in row.get("turns") or []
            ],
            "db_verification": row.get("db_verification") or {},
        }
        print("EVAL_SCENARIO_COMPACT=" + json.dumps(compact, ensure_ascii=False, separators=(",", ":")), flush=True)
    print("EVAL_COMPACT_END", flush=True)


def main() -> int:
    ns = parse_args()
    require_explicit_demo_eval()
    if not all(value > 0 for value in (
        ns.input_price_per_million,
        ns.cached_input_price_per_million,
        ns.output_price_per_million,
    )):
        raise RuntimeError("Current provider pricing must be supplied explicitly.")

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as db:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == ns.workspace_slug))
        if workspace is None:
            raise RuntimeError("Demo workspace not found.")
        assert_demo_only(workspace)
        demo_seed = {
            "workspace_id": str(workspace.id),
            "workspace_slug": workspace.slug,
            "history_loader_limit": settings.agent_history_messages,
            "fixture_version": BATCH4_FIXTURE_VERSION,
        }

    results: list[ScenarioResult] = []
    stopped_for_p0 = False
    for case_fn in CASES:
        row = _run_case(engine, ns.workspace_slug, case_fn)
        _apply_cost(
            row,
            input_price=ns.input_price_per_million,
            cached_price=ns.cached_input_price_per_million,
            output_price=ns.output_price_per_million,
            cache_write_multiplier=ns.cache_write_multiplier,
        )
        results.append(row)
        if any(issue.get("severity") == "P0" for issue in row.issues):
            stopped_for_p0 = True
            break

    summary = summarize(results)
    total_actual_cost = sum(float(row.cost.get("actual_total_usd") or 0) for row in results)
    total_without_cache = sum(float(row.cost.get("without_explicit_cache_usd") or 0) for row in results)
    summary["cost"] = {
        "actual_usd": round(total_actual_cost, 8),
        "without_explicit_cache_usd": round(total_without_cache, 8),
        "saving_usd": round(max(0.0, total_without_cache - total_actual_cost), 8),
        "saving_percent": round(
            ((total_without_cache - total_actual_cost) / total_without_cache * 100.0)
            if total_without_cache > 0 else 0.0,
            2,
        ),
    }
    summary["stopped_for_deterministic_p0"] = stopped_for_p0

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(ns.output_dir)
    payload = {
        "run_metadata": {
            "batch": BATCH_NUMBER,
            "scenario_version": SCENARIO_VERSION,
            "git_sha": ns.git_sha,
            "batch4_base_sha": ns.batch4_base_sha,
            "workspace": ns.workspace_slug,
            "model": settings.openai_model,
            "fallback_model": settings.openai_fallback_model,
            "reasoning_effort": settings.openai_reasoning_effort,
            "fallback_reasoning_effort": settings.openai_fallback_reasoning_effort,
            "generated_at": datetime.now(UTC).isoformat(),
            "pricing": {
                "input_per_million": ns.input_price_per_million,
                "cached_input_per_million": ns.cached_input_price_per_million,
                "output_per_million": ns.output_price_per_million,
                "cache_write_multiplier": ns.cache_write_multiplier,
                "source": ns.pricing_source,
            },
            "demo_seed": demo_seed,
        },
        "scenario_results": [jsonable(row) for row in results],
        "batch_summary": summary,
    }
    json_path = output_dir / f"batch_04_raw_{timestamp}.json"
    md_path = output_dir / f"batch_04_raw_{timestamp}.md"
    _write_reports(payload, json_path, md_path)
    _emit_compact(payload)

    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    ).decode("ascii")
    print("EVAL_REPORT_B64_BEGIN", flush=True)
    for offset in range(0, len(encoded), 3000):
        print(f"EVAL_REPORT_B64={encoded[offset:offset + 3000]}", flush=True)
    print("EVAL_REPORT_B64_END", flush=True)
    print(f"JSON_RESULT={json_path}")
    print(f"MD_RESULT={md_path}")
    print(f"SCENARIOS_RUN={len(results)}")
    print(f"TOTAL_TURNS={summary['total_turns']}")
    print(f"TOTAL_LLM_CALLS={summary['total_llm_calls']}")
    print(f"INTERPRETER_CALLS={summary['interpreter_calls']}")
    print(f"RESPONDER_CALLS={summary['responder_calls']}")
    print(f"TOTAL_TOKENS={summary['tokens']['total_tokens']}")
    print(f"ACTUAL_COST_USD={summary['cost']['actual_usd']}")
    print(f"WITHOUT_CACHE_USD={summary['cost']['without_explicit_cache_usd']}")
    print(f"CACHE_SAVING_PERCENT={summary['cost']['saving_percent']}")
    return 2 if stopped_for_p0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
