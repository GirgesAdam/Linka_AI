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
from app.models.patient import Patient
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.agent_v2.state_persistence import load_active_task
from app.services.package_offers import list_package_offers
from sqlalchemy import select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import (
    ScenarioResult,
    acquire_eval_advisory_lock,
    active_branch_id,
    assert_demo_only,
    default_evaluation,
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
from tools.agent_eval.run_batch_02 import _apply_cost, _ensure_batch2_catalog_fixtures
from tools.agent_eval.run_batch_03 import (
    _appointment_by_id,
    _claim_reply_and_handback,
    _future_slot,
    _seed_future_appointment,
    _seed_package_balance,
    db_delta,
    extended_state_snapshot,
    make_result,
    summarize,
)
from tools.agent_eval.run_batch_04 import _replacement_rows

ScenarioFn = Callable[[Session, Workspace], ScenarioResult]
BATCH_NUMBER = 7
SCENARIO_VERSION = "batch7-v1"
BATCH7_FIXTURE_VERSION = "batch7-demo-fixtures-v2"

STALE_COUNTER_KEYS = (
    "stale_service_carryovers",
    "stale_doctor_carryovers",
    "stale_device_carryovers",
    "stale_date_time_carryovers",
    "stale_package_carryovers",
    "wrong_active_task_target",
    "unexpected_task_restart",
    "unexpected_task_loss",
    "duplicate_writes",
    "stale_lifecycle_writes",
    "wrong_appointment_writes",
    "side_read_business_writes",
    "financial_boundary_violations",
    "human_ownership_writes",
    "invented_entity_writes",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument("--output-dir", default="backend/eval_results")
    parser.add_argument("--git-sha", default=os.getenv("GITHUB_SHA") or "unknown")
    parser.add_argument("--batch7-base-sha", required=True)
    parser.add_argument("--input-price-per-million", type=float, required=True)
    parser.add_argument("--cached-input-price-per-million", type=float, required=True)
    parser.add_argument("--output-price-per-million", type=float, required=True)
    parser.add_argument("--cache-write-multiplier", type=float, default=1.0)
    parser.add_argument("--pricing-source", required=True)
    return parser.parse_args()


def require_explicit_demo_eval() -> None:
    if os.getenv("TIA_AGENT_EVAL_CONFIRM_DEMO") != "1":
        raise RuntimeError("Set TIA_AGENT_EVAL_CONFIRM_DEMO=1 to run the live Demo evaluation.")


def _counter(**values: int) -> dict[str, int]:
    row = {key: 0 for key in STALE_COUNTER_KEYS}
    for key, value in values.items():
        if key not in row:
            raise KeyError(key)
        row[key] = int(value)
    return row


def _replacement_chain(snapshot: dict[str, object], source_id: str | UUID) -> list[dict[str, object]]:
    appointments = [row for row in snapshot.get("appointments", []) if isinstance(row, dict)]
    frontier = {str(source_id)}
    seen: set[str] = set()
    chain: list[dict[str, object]] = []
    while frontier:
        next_frontier: set[str] = set()
        for row in appointments:
            row_id = str(row.get("id") or "")
            parent = str(row.get("rescheduled_from_appointment_id") or "")
            if not row_id or row_id in seen or parent not in frontier:
                continue
            seen.add(row_id)
            chain.append(row)
            next_frontier.add(row_id)
        frontier = next_frontier
    return chain


def _task_snapshot(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    conversation_id: UUID | None,
) -> dict[str, Any] | None:
    if conversation_id is None:
        return None
    persisted = load_active_task(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation_id,
        patient_id=patient.id,
    )
    if persisted is None:
        return None
    task = persisted.active_task.model_dump(mode="json")
    return {
        "flow_id": str(persisted.flow_id),
        "flow_version": persisted.flow_version,
        "task_type": task.get("task_type"),
        "task_version": task.get("version"),
        "status": task.get("status"),
        "write_authorization": task.get("write_authorization"),
        "service": (task.get("constraints") or {}).get("service_id")
        if task.get("task_type") == "booking"
        else (task.get("replacement") or {}).get("service_id"),
        "doctor": (task.get("constraints") or {}).get("doctor_id")
        if task.get("task_type") == "booking"
        else (task.get("replacement") or {}).get("doctor_id"),
        "device": (task.get("constraints") or {}).get("device_key")
        if task.get("task_type") == "booking"
        else (task.get("replacement") or {}).get("device_key"),
        "date": (task.get("constraints") or {}).get("date")
        if task.get("task_type") == "booking"
        else (task.get("replacement") or {}).get("date"),
        "time": (task.get("constraints") or {}).get("time")
        if task.get("task_type") == "booking"
        else (task.get("replacement") or {}).get("time"),
        "package_usage": (task.get("constraints") or {}).get("package_usage")
        if task.get("task_type") == "booking"
        else (task.get("replacement") or {}).get("package_usage"),
        "target": task.get("target"),
        "grouped": task.get("grouped"),
        "pending_choice": task.get("option_snapshot"),
        "derived": task.get("derived"),
        "raw": task,
    }


def _trace_projection(turn) -> dict[str, Any]:
    trace = turn.structured_trace[-1] if turn.structured_trace else {}
    understanding = trace.get("understanding") or {}
    plan = trace.get("plan") or {}
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        steps = []
    outcomes = trace.get("outcomes") or []
    if not isinstance(outcomes, list):
        outcomes = []
    return {
        "interpreter_operations": understanding.get("operations") or [],
        "plan_steps": steps,
        "write_intents": [
            step.get("write_intent")
            for step in steps
            if isinstance(step, dict) and step.get("write_intent") is not None
        ],
        "verified_reads": list(turn.verified_reads),
        "writes_executed": {
            "attempted": bool(turn.write_attempted),
            "result": turn.write_result,
            "actions": turn.actions,
        },
        "outcomes": [
            {
                "status": row.get("status"),
                "response_goal": row.get("response_goal"),
                "facts": row.get("facts"),
                "action_result": row.get("action_result"),
            }
            for row in outcomes
            if isinstance(row, dict)
        ],
        "trace_active_task": trace.get("active_task"),
        "trace_persisted_task": trace.get("persisted_task"),
    }


def _send_with_state(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    scenario_id: str,
    number: int,
    message: str,
    conversation_id: UUID | None,
    evidence: list[dict[str, Any]],
    *,
    previous_turn=None,
):
    before = _task_snapshot(db, workspace, patient, conversation_id)
    response, turn = send_turn(
        db,
        workspace,
        patient,
        scenario_id,
        number,
        message,
        conversation_id,
    )
    after = _task_snapshot(db, workspace, patient, response.conversation_id)
    projected = _trace_projection(turn)
    evidence.append(
        {
            "turn_number": number,
            "customer_message": message,
            "active_task_before": before,
            "active_task_after": after,
            "recent_read_context_before": (
                list(previous_turn.verified_reads) if previous_turn is not None else []
            ),
            "recent_action_context_before": (
                list(previous_turn.actions) if previous_turn is not None else []
            ),
            "recent_read_context_after": list(turn.verified_reads),
            "recent_action_context_after": list(turn.actions),
            **projected,
        }
    )
    return response, turn


def _run_stateful_messages(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    scenario_id: str,
    messages: list[str],
    *,
    conversation_id: UUID | None = None,
    evidence: list[dict[str, Any]] | None = None,
):
    rows = evidence if evidence is not None else []
    turns = []
    current = conversation_id
    previous = None
    for number, message in enumerate(messages, start=1):
        response, turn = _send_with_state(
            db,
            workspace,
            patient,
            scenario_id,
            number,
            message,
            current,
            rows,
            previous_turn=previous,
        )
        current = response.conversation_id
        turns.append(turn)
        previous = turn
    return turns, current, rows


def _append_turn(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    scenario_id: str,
    turns: list,
    conversation_id: UUID,
    evidence: list[dict[str, Any]],
    message: str,
):
    previous = turns[-1] if turns else None
    response, turn = _send_with_state(
        db,
        workspace,
        patient,
        scenario_id,
        len(turns) + 1,
        message,
        conversation_id,
        evidence,
        previous_turn=previous,
    )
    turns.append(turn)
    return response.conversation_id, turn


def _doctor_row(catalog: dict[str, Any], doctor_id: UUID | str) -> dict[str, Any]:
    target = str(doctor_id)
    return next(
        row
        for row in catalog.get("doctors", [])
        if isinstance(row, dict) and str(row.get("id")) == target
    )


def _service_slot(
    db: Session,
    workspace: Workspace,
    service: Service,
    *,
    doctor_id: UUID | None = None,
    device_key: str | None = None,
    after_date: date | None = None,
):
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    today = datetime.now(UTC).date()
    for offset in range(1, 70):
        day = today + timedelta(days=offset)
        if after_date is not None and day <= after_date:
            continue
        available = adapter.get_availability(
            AvailabilityRequest(
                branch_id=branch_id,
                service_id=str(service.id),
                booking_date=day,
                doctor_id=str(doctor_id) if doctor_id else None,
                laser_device_key=device_key,
            )
        )
        if available.slots:
            slot = available.slots[0]
            doctor = _doctor_row(catalog, slot.doctor_id)
            return catalog, doctor, available, slot
    raise RuntimeError("EVAL_INFRA_ERROR: no suitable service slot")



def _cancellation_safe_service_slot(
    db: Session,
    workspace: Workspace,
    service: Service,
):
    catalog, doctor, _nearest_available, nearest_slot = _service_slot(
        db,
        workspace,
        service,
    )
    available, slot = _future_slot(
        db,
        workspace,
        service_id=str(service.id),
        doctor_id=str(doctor["id"]),
        after_date=nearest_slot.start_at.astimezone(UTC).date(),
    )
    return catalog, doctor, available, slot

def _two_doctor_slots(
    db: Session,
    workspace: Workspace,
    service: Service,
):
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    today = datetime.now(UTC).date()
    compatible = [
        row
        for row in catalog.get("doctors", [])
        if isinstance(row, dict)
        and row.get("id")
        and str(service.id) in {str(value) for value in (row.get("service_ids") or [])}
    ]
    found = []
    for doctor in compatible:
        for offset in range(2, 45):
            day = today + timedelta(days=offset)
            available = adapter.get_availability(
                AvailabilityRequest(
                    branch_id=branch_id,
                    service_id=str(service.id),
                    booking_date=day,
                    doctor_id=str(doctor["id"]),
                )
            )
            if available.slots:
                found.append((doctor, available, available.slots[0]))
                break
        if len(found) >= 2:
            return catalog, found[0], found[1]
    raise RuntimeError("EVAL_INFRA_ERROR: service has fewer than two bookable doctors")


def _laser_slot(
    db: Session,
    workspace: Workspace,
    *,
    device_key: str,
    doctor_id: UUID | None = None,
    after_date: date | None = None,
):
    service = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    return (service, *_service_slot(
        db,
        workspace,
        service,
        doctor_id=doctor_id,
        device_key=device_key,
        after_date=after_date,
    ))


def _active_appointments(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in snapshot.get("appointments") or []
        if row.get("status") in {"pending", "confirmed", "checked_in", "in_progress"}
    ]


def _new_packages(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    known = {row["id"] for row in before.get("packages") or []}
    return [row for row in after.get("packages") or [] if row["id"] not in known]


def _all_turn_writes_before(turns: list, final_index: int) -> bool:
    return any(turn.write_attempted for turn in turns[:final_index])


def _same_booking(row: dict[str, Any], *, service: Service, doctor_id: UUID | str, slot, device_key: str | None = None) -> bool:
    return (
        row.get("service_id") == str(service.id)
        and row.get("doctor_id") == str(doctor_id)
        and row.get("start_at") == slot.start_at.isoformat()
        and row.get("laser_device_key") == device_key
    )


def _side_read_writes(turns: list, indices: tuple[int, ...]) -> int:
    return sum(int(turns[index].write_attempted) for index in indices if 0 <= index < len(turns))


def _result(
    *,
    scenario_id: str,
    category: str,
    purpose: str,
    turns: list,
    before: dict[str, Any],
    after: dict[str, Any],
    evidence: list[dict[str, Any]],
    counters: dict[str, int],
    verification: dict[str, Any],
    deterministic_ok: bool,
    expected: str,
    issue_severity: str = "P1",
    issue_title: str = "Long-horizon state continuity contract mismatch",
    issue_detail: str = "",
    handoff_ok: bool | None = None,
) -> ScenarioResult:
    return make_result(
        scenario_id=scenario_id,
        category=category,
        purpose=purpose,
        turns=turns,
        before=before,
        after=after,
        verification={
            **verification,
            "turn_state_evidence": evidence,
            "state_continuity_counters": counters,
        },
        deterministic_ok=deterministic_ok and not any(counters.values()),
        expected=expected,
        issue_severity=issue_severity,
        issue_title=issue_title,
        issue_detail=issue_detail,
        handoff_ok=handoff_ok,
    )


def _future_target_after(
    db: Session,
    workspace: Workspace,
    appointment: Appointment,
    *,
    after_date: date | None = None,
):
    return _future_slot(
        db,
        workspace,
        service_id=str(appointment.service_id),
        doctor_id=str(appointment.doctor_id),
        device_key=appointment.laser_device_key,
        after_date=after_date,
        exclude_appointment_id=str(appointment.id),
    )




def case_01_booking_doctor_info_price_resume(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_01_booking_doctor_info_price_resume"
    patient = quiet_patient(db, workspace)
    service, _catalog, doctor, available, slot = _laser_slot(
        db, workspace, device_key="prime_lase"
    )
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"عايزة أحجز {service.name}",
            f"خليه مع {doctor_name(doctor)} وعلى Prime Lase",
            f"هو {doctor_name(doctor)} بيقدم خدمة {service.name}؟",
            "وسعر الجلسة على Prime Lase كام؟",
            f"كملي الحجز يوم {day}",
            f"الساعة {time_text} وكملي الحجز",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    correct = (
        len(created) == 1
        and _same_booking(
            created[0],
            service=service,
            doctor_id=doctor["id"],
            slot=slot,
            device_key="prime_lase",
        )
    )
    side_writes = _side_read_writes(turns, (2, 3))
    counters = _counter(
        stale_service_carryovers=int(bool(created) and created[0]["service_id"] != str(service.id)),
        stale_doctor_carryovers=int(bool(created) and created[0]["doctor_id"] != str(doctor["id"])),
        stale_device_carryovers=int(bool(created) and created[0]["laser_device_key"] != "prime_lase"),
        duplicate_writes=max(0, len(created) - 1),
        side_read_business_writes=side_writes,
    )
    return _result(
        scenario_id=scenario_id,
        category="long_booking_continuity",
        purpose="A booking task must survive doctor/service information and price side reads, then write exactly the selected canonical booking.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "expected_service_id": str(service.id),
            "expected_doctor_id": str(doctor["id"]),
            "expected_device_key": "prime_lase",
            "expected_start": slot.start_at.isoformat(),
            "created": created,
            "side_read_turns": [3, 4],
        },
        deterministic_ok=correct and side_writes == 0,
        expected="Side reads do not mutate business state; one final Prime Lase booking retains the chosen service/doctor/device and exact verified slot.",
        issue_severity="P1",
        issue_title="Long booking continuity lost identity or produced a side-read/duplicate write",
    )


def case_02_booking_multiple_corrections(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_02_booking_multiple_corrections"
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _catalog, first, second = _two_doctor_slots(db, workspace, service)
    doctor_a, available_a, slot_a = first
    doctor_b, available_b, slot_b = second
    day_a, _time_a = local_slot(available_a, slot_a)
    day_b, time_b = local_slot(available_b, slot_b)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"عايزة أحجز {service.name}",
            f"خليه يوم {day_a}",
            f"مع {doctor_name(doctor_a)}",
            f"لا غيري الدكتور وخليه مع {doctor_name(doctor_b)}",
            f"وكمان غيري اليوم وخليه {day_b}",
            f"قبل ما نكمل سعر {service.name} كام؟",
            f"الساعة {time_b} وكملي الحجز",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    correct = (
        len(created) == 1
        and _same_booking(
            created[0],
            service=service,
            doctor_id=doctor_b["id"],
            slot=slot_b,
        )
    )
    counters = _counter(
        stale_doctor_carryovers=int(bool(created) and created[0]["doctor_id"] != str(doctor_b["id"])),
        stale_date_time_carryovers=int(bool(created) and created[0]["start_at"] != slot_b.start_at.isoformat()),
        duplicate_writes=max(0, len(created) - 1),
        side_read_business_writes=_side_read_writes(turns, (5,)),
    )
    return _result(
        scenario_id=scenario_id,
        category="correction_chain",
        purpose="Repeated doctor/date/time corrections must invalidate old scheduling facts so only the latest constraints authorize the booking.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "initial_doctor_id": str(doctor_a["id"]),
            "final_doctor_id": str(doctor_b["id"]),
            "initial_date": day_a,
            "final_start": slot_b.start_at.isoformat(),
            "created": created,
        },
        deterministic_ok=correct,
        expected="Exactly one Hydrafacial booking uses the latest doctor/date/time; earlier availability never survives as write authority.",
        issue_severity="P1",
        issue_title="Correction chain wrote stale doctor/date/time constraints",
    )


def case_03_laser_device_correction_after_side_reads(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_03_laser_device_correction_after_side_reads"
    patient = quiet_patient(db, workspace)
    service, _catalog, prime_doctor, prime_av, prime_slot = _laser_slot(
        db, workspace, device_key="prime_lase"
    )
    _service2, _catalog2, _candela_doctor, _candela_av, _candela_slot = _laser_slot(
        db,
        workspace,
        device_key="candela_gentle",
        doctor_id=UUID(str(prime_doctor["id"])),
    )
    day, time_text = local_slot(prime_av, prime_slot)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"عايزة أحجز {service.name}",
            "اختاري Candela Gentle",
            f"هو {doctor_name(prime_doctor)} بيقدم الخدمة دي؟",
            "وسعر Candela Gentle كام؟",
            "لا غيري الجهاز وخليه Prime Lase",
            f"احجزي يوم {day} الساعة {time_text} مع {doctor_name(prime_doctor)}",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    correct = (
        len(created) == 1
        and _same_booking(
            created[0],
            service=service,
            doctor_id=prime_doctor["id"],
            slot=prime_slot,
            device_key="prime_lase",
        )
    )
    counters = _counter(
        stale_device_carryovers=int(bool(created) and created[0]["laser_device_key"] != "prime_lase"),
        duplicate_writes=max(0, len(created) - 1),
        side_read_business_writes=_side_read_writes(turns, (2, 3)),
    )
    return _result(
        scenario_id=scenario_id,
        category="device_correction_continuity",
        purpose="A laser task must survive side reads and replace Candela with Prime without stale device facts leaking into the final write.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "initial_device": "candela_gentle",
            "final_device": "prime_lase",
            "created": created,
            "final_expected_start": prime_slot.start_at.isoformat(),
        },
        deterministic_ok=correct,
        expected="Prime Lase is the only final device authority; Candela facts from earlier turns cannot authorize or contaminate the booking.",
        issue_severity="P1",
        issue_title="Old laser device survived a later explicit device correction",
    )


def case_04_service_replacement_invalidates_old_device(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_04_service_replacement_invalidates_old_device"
    patient = quiet_patient(db, workspace)
    laser, _cat_l, laser_doctor, _laser_av, _laser_slot_row = _laser_slot(
        db, workspace, device_key="prime_lase"
    )
    hydra = service_by_slug(db, workspace, "hydrafacial")
    _cat_h, hydra_doctor, hydra_av, hydra_slot = _service_slot(db, workspace, hydra)
    hydra_day, hydra_time = local_slot(hydra_av, hydra_slot)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"عايزة أحجز {laser.name}",
            f"خليه مع {doctor_name(laser_doctor)} وعلى Prime Lase",
            "سعر Prime Lase كام؟",
            f"غيرت رأيي، غيري الخدمة نفسها وخليها {hydra.name}",
            f"طيب سعر {hydra.name} كام؟",
            f"احجزي {hydra.name} يوم {hydra_day} الساعة {hydra_time} مع {doctor_name(hydra_doctor)}",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    correct = (
        len(created) == 1
        and _same_booking(
            created[0],
            service=hydra,
            doctor_id=hydra_doctor["id"],
            slot=hydra_slot,
        )
        and created[0]["laser_device_key"] is None
    )
    counters = _counter(
        stale_service_carryovers=int(bool(created) and created[0]["service_id"] != str(hydra.id)),
        stale_device_carryovers=int(bool(created) and created[0]["laser_device_key"] is not None),
        duplicate_writes=max(0, len(created) - 1),
        side_read_business_writes=_side_read_writes(turns, (2, 4)),
    )
    return _result(
        scenario_id=scenario_id,
        category="service_identity_invalidation",
        purpose="Replacing a laser service with Hydrafacial must clear laser-only state and re-ground later reads/writes on the new service.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "old_service_id": str(laser.id),
            "new_service_id": str(hydra.id),
            "created": created,
        },
        deterministic_ok=correct,
        expected="Final Hydrafacial booking contains no laser device and no stale laser-service scheduling fact.",
        issue_severity="P1",
        issue_title="Service replacement leaked incompatible laser identity into the final booking",
    )


def case_05_standard_to_laser_requires_device(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_05_standard_to_laser_requires_device"
    patient = quiet_patient(db, workspace)
    hydra = service_by_slug(db, workspace, "hydrafacial")
    _hcat, _hdoc, hav, hslot = _service_slot(db, workspace, hydra)
    hydra_day, _ = local_slot(hav, hslot)
    laser, _lcat, ldoc, lav, lslot = _laser_slot(db, workspace, device_key="prime_lase")
    laser_day, laser_time = local_slot(lav, lslot)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"عايزة أحجز {hydra.name}",
            f"خليه يوم {hydra_day}",
            f"قبل ما نكمل سعر {hydra.name} كام؟",
            f"غيري الخدمة وخليها {laser.name}",
            "اختاري Prime Lase",
            f"احجزي يوم {laser_day} الساعة {laser_time} مع {doctor_name(ldoc)}",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    correct = (
        len(created) == 1
        and _same_booking(
            created[0],
            service=laser,
            doctor_id=ldoc["id"],
            slot=lslot,
            device_key="prime_lase",
        )
    )
    writes_before_device = sum(int(turn.write_attempted) for turn in turns[:4])
    counters = _counter(
        stale_service_carryovers=int(bool(created) and created[0]["service_id"] != str(laser.id)),
        stale_date_time_carryovers=int(bool(created) and created[0]["start_at"] != lslot.start_at.isoformat()),
        stale_device_carryovers=int(bool(created) and created[0]["laser_device_key"] != "prime_lase"),
        duplicate_writes=max(0, len(created) - 1),
        side_read_business_writes=_side_read_writes(turns, (2,)),
    )
    return _result(
        scenario_id=scenario_id,
        category="service_identity_invalidation",
        purpose="Changing a standard service into laser must introduce the device requirement and prevent any write until the device is grounded.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "old_service_id": str(hydra.id),
            "new_service_id": str(laser.id),
            "created": created,
            "writes_before_device_selection": writes_before_device,
        },
        deterministic_ok=correct and writes_before_device == 0,
        expected="No booking is written from the old Hydrafacial state; the final laser booking requires and uses Prime Lase.",
        issue_severity="P1",
        issue_title="Standard-to-laser replacement bypassed device grounding or reused stale scheduling state",
    )


def case_06_package_side_read_financial_boundary_resume(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_06_package_side_read_financial_boundary_resume"
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _catalog, doctor, available, slot = _service_slot(db, workspace, service)
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"عايزة أحجز {service.name}",
            f"فيه باقات لـ {service.name}؟",
            f"لا مش هشتري باقة، كملي الحجز العادي يوم {day}",
            "طب أنا دفعت للحجز ده قبل كده ولا لأ؟",
        ],
        evidence=evidence,
    )
    financial_turn = turns[-1]
    task_after_financial = evidence[-1].get("active_task_after")
    handoff_present = financial_turn.handoff_state is not None
    handback = _claim_reply_and_handback(
        db,
        workspace,
        conversation_id,
        staff_text="الاستقبال راجع السؤال المالي، ممكن تكملي الحجز مع Tia.",
    )
    conversation_id, _ = _append_turn(
        db,
        workspace,
        patient,
        scenario_id,
        turns,
        conversation_id,
        evidence,
        f"كملي الحجز العادي الساعة {time_text} مع {doctor_name(doctor)}",
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    delta = db_delta(before, after)
    correct_booking = (
        len(created) == 1
        and _same_booking(
            created[0],
            service=service,
            doctor_id=doctor["id"],
            slot=slot,
        )
        and created[0].get("patient_package_id") is None
        and created[0].get("billing_context") == "standard"
    )
    financial_business_writes = int(financial_turn.write_attempted)
    counters = _counter(
        stale_package_carryovers=int(
            bool(created)
            and (
                created[0].get("patient_package_id") is not None
                or created[0].get("billing_context") != "standard"
            )
        ),
        unexpected_task_loss=int(task_after_financial is None),
        side_read_business_writes=_side_read_writes(turns, (1,)),
        financial_boundary_violations=int(
            financial_business_writes
            or bool(delta["payments"]["created"])
            or bool(delta["payments"]["changed"])
        ),
        human_ownership_writes=int(financial_business_writes),
        duplicate_writes=max(0, len(created) - 1),
    )
    return _result(
        scenario_id=scenario_id,
        category="package_context_and_financial_boundary",
        purpose="Package information must stay read-only, a financial interruption must hand off without business writes, and the standard booking task must survive a clean handback.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "financial_handoff_present": handoff_present,
            "task_after_financial": task_after_financial,
            "handback": handback,
            "created": created,
            "payments_delta": delta["payments"],
            "package_delta": delta["packages"],
            "package_usage_delta": delta["package_usages"],
        },
        deterministic_ok=(
            handoff_present
            and task_after_financial is not None
            and correct_booking
            and not delta["packages"]["created"]
            and not delta["package_usages"]["created"]
            and not delta["payments"]["created"]
        ),
        expected="Package side-read changes no entitlement; the payment-status question creates a Reception boundary with zero financial/business writes; after handback the same standard booking completes once.",
        issue_severity="P1",
        issue_title="Financial/package interruption corrupted or wrote through the active booking",
        handoff_ok=handoff_present,
    )


def case_07_buy_package_then_continue_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_07_buy_package_then_continue_booking"
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    offers = [
        offer
        for offer in list_package_offers(
            db,
            workspace_id=workspace.id,
            service_id=service.id,
            active_only=True,
        )
        if offer.device_key is None
    ]
    if not offers:
        raise RuntimeError("EVAL_INFRA_ERROR: no active Hydrafacial package offer")
    offer = offers[0]
    _catalog, doctor, available, slot = _service_slot(db, workspace, service)
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"وريني باقات {service.name}",
            f"اشتريلي باقة {offer.sessions_count} جلسات {service.name}",
            f"طيب سعر جلسة {service.name} لوحدها كام؟",
            f"عايزة أحجز جلسة {service.name} من الباقة اللي اشتريتها",
            f"خليها يوم {day} مع {doctor_name(doctor)}",
            f"الساعة {time_text} وكملي الحجز",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    packages = _new_packages(before, after)
    delta = db_delta(before, after)
    package_id = packages[0]["id"] if len(packages) == 1 else None
    linked = (
        len(created) == 1
        and package_id is not None
        and created[0].get("patient_package_id") == package_id
        and created[0].get("billing_context") == "package_prepaid"
        and len(delta["package_usages"]["created"]) == 1
    )
    counters = _counter(
        stale_package_carryovers=int(bool(created) and not linked),
        duplicate_writes=max(0, len(created) - 1) + max(0, len(packages) - 1),
        side_read_business_writes=_side_read_writes(turns, (0, 2)),
        financial_boundary_violations=int(bool(delta["payments"]["created"])),
    )
    return _result(
        scenario_id=scenario_id,
        category="package_long_horizon",
        purpose="A package purchase must remain canonical across an informational detour and later ground exactly one package-backed booking.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "offer": jsonable(offer),
            "new_packages": packages,
            "created": created,
            "package_usage_delta": delta["package_usages"],
            "payments_delta": delta["payments"],
            "linked_booking": linked,
        },
        deterministic_ok=(
            len(packages) == 1
            and linked
            and not delta["payments"]["created"]
        ),
        expected="Purchase occurs once, later booking explicitly uses that owned package once, one PackageUsage is created, and no payment is invented.",
        issue_severity="P1",
        issue_title="Package purchase/booking continuity duplicated or linked the wrong entitlement",
    )


def case_08_package_changes_externally_before_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_08_package_changes_externally_before_booking"
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    package = _seed_package_balance(
        db,
        workspace,
        patient,
        service,
        remaining=2,
        name="Batch7 mutable Hydrafacial package",
    )
    _catalog, doctor, available, slot = _service_slot(db, workspace, service)
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"عايزة أحجز {service.name} من الباقة بتاعتي",
            f"خليها يوم {day}",
            f"مع {doctor_name(doctor)}",
        ],
        evidence=evidence,
    )
    package.status = "cancelled"
    db.flush()
    conversation_id, _ = _append_turn(
        db,
        workspace,
        patient,
        scenario_id,
        turns,
        conversation_id,
        evidence,
        f"قبل ما نكمل سعر {service.name} كام؟",
    )
    conversation_id, _ = _append_turn(
        db,
        workspace,
        patient,
        scenario_id,
        turns,
        conversation_id,
        evidence,
        f"الساعة {time_text} وكملي الحجز من الباقة",
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    delta = db_delta(before, after)
    package_usage_ids = delta["package_usages"]["created"]
    stale_link = any(row.get("patient_package_id") == str(package.id) for row in created)
    counters = _counter(
        stale_package_carryovers=int(stale_link or bool(package_usage_ids)),
        duplicate_writes=max(0, len(created) - 1),
        side_read_business_writes=_side_read_writes(turns, (3,)),
    )
    return _result(
        scenario_id=scenario_id,
        category="canonical_entitlement_revalidation",
        purpose="A package entitlement that becomes invalid during a long booking must be revalidated before write; conversation history cannot keep it usable.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "package_id": str(package.id),
            "package_status_before_confirmation": package.status,
            "created": created,
            "package_usage_delta": delta["package_usages"],
            "stale_package_link": stale_link,
        },
        deterministic_ok=not stale_link and not package_usage_ids and len(created) == 0,
        expected="Canonical cancelled entitlement wins: no PackageUsage and no package-backed or silently-standard booking is written from stale package context.",
        issue_severity="P1",
        issue_title="Externally invalidated package was still used or silently converted into a booking",
    )


def case_09_booking_detour_then_reschedule(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_09_booking_detour_then_reschedule"
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _catalog, doctor, available, slot = _service_slot(db, workspace, service)
    source_day, source_time = local_slot(available, slot)
    target_av, target_slot = _future_slot(
        db,
        workspace,
        service_id=str(service.id),
        doctor_id=str(doctor["id"]),
        after_date=slot.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
    )
    target_day, target_time = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"احجزيلي {service.name} يوم {source_day} الساعة {source_time} مع {doctor_name(doctor)}",
            f"سعر {service.name} كام؟",
            "وريني مواعيدي الجاية",
            "عايزة أغير ميعاد الحجز اللي عملناه",
            f"خليه يوم {target_day}",
            f"لا خلي الساعة {target_time} وكملي التغيير",
            "تمام",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    source_candidates = [
        row for row in created if row.get("rescheduled_from_appointment_id") is None
    ]
    source = source_candidates[0] if source_candidates else None
    replacements = (
        _replacement_rows(after, source["id"]) if source is not None else []
    )
    replacement_chain = (
        _replacement_chain(after, source["id"]) if source is not None else []
    )
    source_after = (
        _appointment_by_id(after, source["id"]) if source is not None else None
    )
    correct = (
        source is not None
        and source_after is not None
        and source_after["status"] == "rescheduled"
        and len(replacements) == 1
        and replacements[0]["start_at"] == target_slot.start_at.isoformat()
    )
    counters = _counter(
        duplicate_writes=max(0, len(replacement_chain) - 1),
        stale_lifecycle_writes=max(0, len(replacement_chain) - 1),
        stale_date_time_carryovers=int(bool(replacements) and replacements[0]["start_at"] != target_slot.start_at.isoformat()),
        wrong_appointment_writes=int(source is None or (source_after is not None and source_after["status"] != "rescheduled")),
        side_read_business_writes=_side_read_writes(turns, (1, 2)),
    )
    return _result(
        scenario_id=scenario_id,
        category="long_lifecycle_chain",
        purpose="A completed booking must remain the canonical lifecycle target through price/list detours and a multi-turn reschedule correction.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "source": source,
            "source_after": source_after,
            "replacements": replacements,
            "replacement_chain": replacement_chain,
            "target_start": target_slot.start_at.isoformat(),
        },
        deterministic_ok=correct,
        expected="Initial booking occurs once; later reschedule targets that booking only and creates one replacement at the corrected time.",
        issue_severity="P1",
        issue_title="Long lifecycle chain duplicated or rescheduled the wrong/stale appointment",
    )


def case_10_two_appointments_persisted_reschedule_target(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_10_two_appointments_persisted_reschedule_target"
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _catalog, doctor, av_a, slot_a = _service_slot(db, workspace, service)
    _av_b, slot_b = _future_slot(
        db,
        workspace,
        service_id=str(service.id),
        doctor_id=str(doctor["id"]),
        after_date=slot_a.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
    )
    av_target, slot_target = _future_slot(
        db,
        workspace,
        service_id=str(service.id),
        doctor_id=str(doctor["id"]),
        after_date=slot_b.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
    )
    apt_a = _seed_future_appointment(
        db,
        workspace,
        patient,
        service=service,
        doctor_id=UUID(str(doctor["id"])),
        slot=slot_a,
    )
    apt_b = _seed_future_appointment(
        db,
        workspace,
        patient,
        service=service,
        doctor_id=UUID(str(doctor["id"])),
        slot=slot_b,
    )
    day_a, _ = local_slot(av_a, slot_a)
    target_day, target_time = local_slot(av_target, slot_target)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"عايزة أغير ميعاد حجز {service.name} اللي يوم {day_a}",
            f"هو {doctor_name(doctor)} بيقدم {service.name}؟",
            f"خليه يوم {target_day}",
            f"وسعر {service.name} كام؟",
            f"خليه الساعة {target_time}",
            "تمام كملي التغيير",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    a_after = _appointment_by_id(after, apt_a.id)
    b_after = _appointment_by_id(after, apt_b.id)
    replacements = _replacement_rows(after, apt_a.id)
    wrong_b_replacements = _replacement_rows(after, apt_b.id)
    correct = (
        a_after["status"] == "rescheduled"
        and b_after["status"] == "confirmed"
        and len(replacements) == 1
        and replacements[0]["start_at"] == slot_target.start_at.isoformat()
        and not wrong_b_replacements
    )
    counters = _counter(
        wrong_active_task_target=int(bool(wrong_b_replacements) or b_after["status"] != "confirmed"),
        duplicate_writes=max(0, len(replacements) - 1),
        stale_date_time_carryovers=int(bool(replacements) and replacements[0]["start_at"] != slot_target.start_at.isoformat()),
        wrong_appointment_writes=int(bool(wrong_b_replacements) or b_after["status"] != "confirmed"),
        side_read_business_writes=_side_read_writes(turns, (1, 3)),
    )
    return _result(
        scenario_id=scenario_id,
        category="persisted_lifecycle_target",
        purpose="With two similar appointments, the selected reschedule target must survive informational detours and later date/time corrections.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "appointment_a": str(apt_a.id),
            "appointment_b": str(apt_b.id),
            "a_after": a_after,
            "b_after": b_after,
            "replacements_for_a": replacements,
            "replacements_for_b": wrong_b_replacements,
        },
        deterministic_ok=correct,
        expected="Only appointment A is rescheduled; appointment B remains confirmed and untouched through all side reads.",
        issue_severity="P1",
        issue_title="Persisted reschedule target switched to another similar appointment",
    )


def case_11_cancel_one_then_modify_other(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_11_cancel_one_then_modify_other"
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _catalog, doctor, av_a, slot_a = _cancellation_safe_service_slot(
        db,
        workspace,
        service,
    )
    av_b, slot_b = _future_slot(
        db,
        workspace,
        service_id=str(service.id),
        doctor_id=str(doctor["id"]),
        after_date=slot_a.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
    )
    av_target, slot_target = _future_slot(
        db,
        workspace,
        service_id=str(service.id),
        doctor_id=str(doctor["id"]),
        after_date=slot_b.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
    )
    apt_a = _seed_future_appointment(
        db, workspace, patient, service=service, doctor_id=UUID(str(doctor["id"])), slot=slot_a
    )
    apt_b = _seed_future_appointment(
        db, workspace, patient, service=service, doctor_id=UUID(str(doctor["id"])), slot=slot_b
    )
    day_a, _ = local_slot(av_a, slot_a)
    day_b, _ = local_slot(av_b, slot_b)
    target_day, target_time = local_slot(av_target, slot_target)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"الغي حجز {service.name} اللي يوم {day_a}",
            f"سعر {service.name} كام؟",
            "وريني مواعيدي الجاية",
            f"غيري ميعاد حجز {service.name} اللي يوم {day_b}",
            f"خليه يوم {target_day} الساعة {target_time}",
            "تمام كملي",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    a_after = _appointment_by_id(after, apt_a.id)
    b_after = _appointment_by_id(after, apt_b.id)
    replacements_b = _replacement_rows(after, apt_b.id)
    replacements_a = _replacement_rows(after, apt_a.id)
    correct = (
        a_after["status"] == "cancelled"
        and b_after["status"] == "rescheduled"
        and len(replacements_b) == 1
        and replacements_b[0]["start_at"] == slot_target.start_at.isoformat()
        and not replacements_a
    )
    counters = _counter(
        wrong_active_task_target=int(bool(replacements_a) or a_after["status"] != "cancelled"),
        duplicate_writes=max(0, len(replacements_b) - 1),
        stale_lifecycle_writes=int(bool(replacements_a)),
        wrong_appointment_writes=int(bool(replacements_a) or b_after["status"] != "rescheduled"),
        side_read_business_writes=_side_read_writes(turns, (1, 2)),
    )
    return _result(
        scenario_id=scenario_id,
        category="multi_appointment_lifecycle",
        purpose="After cancelling appointment A and taking informational detours, a later reschedule must target appointment B without recent-action confusion.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "appointment_a": str(apt_a.id),
            "appointment_b": str(apt_b.id),
            "a_after": a_after,
            "b_after": b_after,
            "replacements_for_a": replacements_a,
            "replacements_for_b": replacements_b,
        },
        deterministic_ok=correct,
        expected="A is cancelled exactly once; B is the only later reschedule target and receives one replacement.",
        issue_severity="P1",
        issue_title="Recent lifecycle context redirected a later action to the wrong appointment",
    )


def case_12_reception_edits_appointment_during_conversation(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_12_reception_edits_appointment_during_conversation"
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _catalog, first, second = _two_doctor_slots(db, workspace, service)
    doctor_a, _av_a, slot_a = first
    doctor_b, av_b, slot_b = second
    source = _seed_future_appointment(
        db,
        workspace,
        patient,
        service=service,
        doctor_id=UUID(str(doctor_a["id"])),
        slot=slot_a,
    )
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"ميعاد {service.name} بتاعي امتى؟",
            "ومع مين؟",
        ],
        evidence=evidence,
    )

    source.doctor_id = UUID(str(doctor_b["id"]))
    source.start_at = slot_b.start_at
    source.end_at = slot_b.end_at
    source.busy_start_at = slot_b.start_at
    source.busy_end_at = slot_b.end_at
    source.duration_minutes = slot_b.duration_minutes
    source.price_minor = slot_b.price_minor
    db.flush()

    canonical_day, canonical_time = local_slot(av_b, slot_b)
    target_av, target_slot = _future_slot(
        db,
        workspace,
        service_id=str(service.id),
        doctor_id=str(doctor_b["id"]),
        after_date=slot_b.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
        exclude_appointment_id=str(source.id),
    )
    target_day, target_time = local_slot(target_av, target_slot)
    conversation_id, turn3 = _append_turn(
        db,
        workspace,
        patient,
        scenario_id,
        turns,
        conversation_id,
        evidence,
        "طب ميعادي دلوقتي امتى ومع مين؟",
    )
    conversation_id, _ = _append_turn(
        db,
        workspace,
        patient,
        scenario_id,
        turns,
        conversation_id,
        evidence,
        f"غيريه ليوم {target_day} الساعة {target_time}",
    )
    conversation_id, _ = _append_turn(
        db,
        workspace,
        patient,
        scenario_id,
        turns,
        conversation_id,
        evidence,
        "تمام كملي التغيير",
    )
    after = extended_state_snapshot(db, workspace, patient)
    source_after = _appointment_by_id(after, source.id)
    replacements = _replacement_rows(after, source.id)
    replacement_chain = _replacement_chain(after, source.id)
    response3 = turn3.agent_response or ""
    canonical_seen = (
        doctor_name(doctor_b) in response3
        or canonical_time.lstrip("0") in response3
        or canonical_day in response3
    )
    correct = (
        source_after["status"] == "rescheduled"
        and len(replacements) == 1
        and replacements[0]["doctor_id"] == str(doctor_b["id"])
        and replacements[0]["start_at"] == target_slot.start_at.isoformat()
    )
    counters = _counter(
        stale_doctor_carryovers=int(bool(replacements) and replacements[0]["doctor_id"] != str(doctor_b["id"])),
        stale_date_time_carryovers=int(bool(replacements) and replacements[0]["start_at"] != target_slot.start_at.isoformat()),
        wrong_active_task_target=int(source_after["id"] != str(source.id)),
        duplicate_writes=max(0, len(replacement_chain) - 1),
        stale_lifecycle_writes=max(int(not correct and bool(replacements)), max(0, len(replacement_chain) - 1)),
        wrong_appointment_writes=int(not correct and bool(replacements)),
    )
    return _result(
        scenario_id=scenario_id,
        category="canonical_truth_over_history",
        purpose="Reception can canonically change an appointment during a conversation; later reads and lifecycle writes must use the DB state, not earlier chat facts.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "source_id": str(source.id),
            "old_doctor_id": str(doctor_a["id"]),
            "old_start": slot_a.start_at.isoformat(),
            "canonical_doctor_id_after_external_edit": str(doctor_b["id"]),
            "canonical_start_after_external_edit": slot_b.start_at.isoformat(),
            "turn3_response": response3,
            "canonical_change_visible_in_followup": canonical_seen,
            "source_after": source_after,
            "replacements": replacements,
            "replacement_chain": replacement_chain,
        },
        deterministic_ok=correct and canonical_seen,
        expected="The follow-up reflects the canonical reception edit; the subsequent reschedule targets that same current appointment and uses fresh doctor/time state.",
        issue_severity="P1",
        issue_title="Canonical appointment edit was ignored in later lifecycle state",
    )



def _active_reschedule_targets(
    active_task: object,
    appointment_id: str,
) -> bool:
    return bool(
        isinstance(active_task, dict)
        and active_task.get("task_type") == "reschedule"
        and (active_task.get("target") or {}).get("appointment_id") == appointment_id
    )

def case_13_external_cancel_before_followup_action(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_13_external_cancel_before_followup_action"
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _catalog, doctor, av, slot = _service_slot(db, workspace, service)
    source = _seed_future_appointment(
        db,
        workspace,
        patient,
        service=service,
        doctor_id=UUID(str(doctor["id"])),
        slot=slot,
    )
    day, _ = local_slot(av, slot)
    target_av, target_slot = _future_slot(
        db,
        workspace,
        service_id=str(service.id),
        doctor_id=str(doctor["id"]),
        after_date=slot.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
        exclude_appointment_id=str(source.id),
    )
    target_day, target_time = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"عايزة أغير ميعاد حجز {service.name} يوم {day}",
            f"قبل ما نكمل سعر {service.name} كام؟",
            f"و{doctor_name(doctor)} بيقدم الخدمة دي؟",
        ],
        evidence=evidence,
    )
    source.status = "cancelled"
    source.cancelled_at = datetime.now(UTC)
    source.cancellation_reason = "Batch 7 external reception cancellation"
    db.flush()

    conversation_id, _ = _append_turn(
        db,
        workspace,
        patient,
        scenario_id,
        turns,
        conversation_id,
        evidence,
        f"غيريه ليوم {target_day} الساعة {target_time}",
    )
    conversation_id, _ = _append_turn(
        db,
        workspace,
        patient,
        scenario_id,
        turns,
        conversation_id,
        evidence,
        "تمام كملي",
    )
    after = extended_state_snapshot(db, workspace, patient)
    source_after = _appointment_by_id(after, source.id)
    replacements = _replacement_rows(after, source.id)
    cancel_followup_index = 3
    active_after_cancel = (
        evidence[cancel_followup_index].get("active_task_after")
        if len(evidence) > cancel_followup_index
        else None
    )
    counters = _counter(
        wrong_active_task_target=int(
            _active_reschedule_targets(active_after_cancel, str(source.id))
        ),
        duplicate_writes=max(0, len(replacements) - 1),
        stale_lifecycle_writes=int(bool(replacements)),
        wrong_appointment_writes=int(bool(replacements)),
        side_read_business_writes=_side_read_writes(turns, (1, 2)),
    )
    return _result(
        scenario_id=scenario_id,
        category="canonical_truth_over_history",
        purpose="A long reschedule discussion must fail closed if Reception cancels the target before the customer completes the lifecycle action.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "source_id": str(source.id),
            "source_after": source_after,
            "replacements": replacements,
            "external_cancelled_before_turn": 4,
            "active_task_after_cancel": active_after_cancel,
        },
        deterministic_ok=source_after["status"] == "cancelled" and not replacements,
        expected="Fresh canonical appointment state wins; the cancelled appointment is not rescheduled and no replacement is created.",
        issue_severity="P1" if replacements else "P2",
        issue_title=(
            "Cancelled canonical appointment received a stale reschedule write"
            if replacements
            else "Cancelled canonical appointment left a stale reschedule task active"
        ),
    )


def case_14_abandon_old_booking_start_new(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_14_abandon_old_booking_start_new"
    patient = quiet_patient(db, workspace)
    hydra = service_by_slug(db, workspace, "hydrafacial")
    _hcat, _hdoc, hav, hslot = _service_slot(db, workspace, hydra)
    hydra_day, _ = local_slot(hav, hslot)
    laser, _lcat, ldoc, lav, lslot = _laser_slot(db, workspace, device_key="prime_lase")
    laser_day, laser_time = local_slot(lav, lslot)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"عايزة أحجز {hydra.name}",
            f"خليه يوم {hydra_day}",
            "سيبي الحجز ده خلاص ومتكمليش فيه",
            "طب سعر ليزر الإبط على Prime Lase كام؟",
            f"عايزة أحجز {laser.name} على Prime Lase مع {doctor_name(ldoc)} يوم {laser_day}",
            f"الساعة {laser_time} وكملي الحجز",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    abandoned_after = evidence[2].get("active_task_after") if len(evidence) > 2 else None
    correct = (
        len(created) == 1
        and _same_booking(
            created[0],
            service=laser,
            doctor_id=ldoc["id"],
            slot=lslot,
            device_key="prime_lase",
        )
    )
    counters = _counter(
        stale_service_carryovers=int(bool(created) and created[0]["service_id"] != str(laser.id)),
        stale_doctor_carryovers=int(bool(created) and created[0]["doctor_id"] != str(ldoc["id"])),
        stale_device_carryovers=int(bool(created) and created[0]["laser_device_key"] != "prime_lase"),
        stale_date_time_carryovers=int(bool(created) and created[0]["start_at"] != lslot.start_at.isoformat()),
        unexpected_task_restart=int(abandoned_after is not None),
        duplicate_writes=max(0, len(created) - 1),
        side_read_business_writes=_side_read_writes(turns, (3,)),
    )
    return _result(
        scenario_id=scenario_id,
        category="active_task_boundary",
        purpose="Explicitly abandoning booking A must remove its state; a later unrelated read and new booking B must start clean.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "abandoned_task_after_turn3": abandoned_after,
            "created": created,
            "old_service_id": str(hydra.id),
            "new_service_id": str(laser.id),
        },
        deterministic_ok=abandoned_after is None and correct,
        expected="Hydrafacial task is explicitly cleared; only the new Prime Lase booking is created and no old constraints leak.",
        issue_severity="P1",
        issue_title="Explicitly abandoned booking leaked state into the new booking",
    )


def case_15_ambiguous_new_intent_preserves_active_task(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_15_ambiguous_new_intent_preserves_active_task"
    patient = quiet_patient(db, workspace)
    hydra = service_by_slug(db, workspace, "hydrafacial")
    _catalog, doctor, available, slot = _service_slot(db, workspace, hydra)
    day, time_text = local_slot(available, slot)
    laser = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"عايزة أحجز {hydra.name}",
            f"خليه يوم {day} مع {doctor_name(doctor)}",
            "طب والليزر؟",
            "قصدي كنت بس بسأل، كملي الهيدرافيشل زي ما هو",
            f"سعر {hydra.name} كام؟",
            f"الساعة {time_text} وكملي الحجز",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ambiguous_after = evidence[2].get("active_task_after") if len(evidence) > 2 else None
    ambiguous_service = (ambiguous_after or {}).get("service")
    correct = (
        len(created) == 1
        and _same_booking(
            created[0],
            service=hydra,
            doctor_id=doctor["id"],
            slot=slot,
        )
        and created[0].get("service_id") != str(laser.id)
    )
    counters = _counter(
        stale_service_carryovers=int(bool(created) and created[0]["service_id"] != str(hydra.id)),
        wrong_active_task_target=int(ambiguous_service == str(laser.id)),
        unexpected_task_restart=int(ambiguous_service == str(laser.id)),
        duplicate_writes=max(0, len(created) - 1),
        side_read_business_writes=_side_read_writes(turns, (2, 4)),
    )
    return _result(
        scenario_id=scenario_id,
        category="active_task_boundary",
        purpose="A vague side intent must not silently replace an active Hydrafacial booking with a guessed laser task.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "active_task_after_ambiguous_turn": ambiguous_after,
            "created": created,
            "hydrafacial_service_id": str(hydra.id),
            "laser_service_id": str(laser.id),
        },
        deterministic_ok=correct and ambiguous_service != str(laser.id),
        expected="Ambiguous 'والليزر؟' cannot become new write authority; the original Hydrafacial task remains recoverable and completes once.",
        issue_severity="P1",
        issue_title="Ambiguous side intent silently replaced or corrupted the active booking",
    )


def case_16_completed_booking_long_detour_repeat_confirmation(db: Session, workspace: Workspace) -> ScenarioResult:
    scenario_id = "b7_16_completed_booking_long_detour_repeat_confirmation"
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _catalog, doctor, available, slot = _service_slot(db, workspace, service)
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    evidence: list[dict[str, Any]] = []
    turns, conversation_id, evidence = _run_stateful_messages(
        db,
        workspace,
        patient,
        scenario_id,
        [
            f"احجزيلي {service.name} يوم {day} الساعة {time_text} مع {doctor_name(doctor)}",
            f"سعر {service.name} كام؟",
            "مواعيد العيادة بتكون امتى؟",
            "وريني حجوزاتي الجاية",
            "تمام احجزيه زي ما اتفقنا",
        ],
        evidence=evidence,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    active = [
        row
        for row in created
        if row.get("status") in {"pending", "confirmed", "checked_in", "in_progress"}
    ]
    exact = [
        row
        for row in active
        if _same_booking(
            row,
            service=service,
            doctor_id=doctor["id"],
            slot=slot,
        )
    ]
    restarted_task = evidence[-1].get("active_task_after") if evidence else None
    counters = _counter(
        unexpected_task_restart=int(restarted_task is not None),
        duplicate_writes=max(0, len(active) - 1),
        stale_service_carryovers=int(bool(active) and any(row["service_id"] != str(service.id) for row in active)),
        stale_date_time_carryovers=int(bool(active) and any(row["start_at"] != slot.start_at.isoformat() for row in active)),
        side_read_business_writes=_side_read_writes(turns, (1, 2, 3)),
    )
    return _result(
        scenario_id=scenario_id,
        category="recent_action_duplicate_protection",
        purpose="A completed booking followed by several informational turns and a repeated confirmation must revalidate the canonical recent action and avoid duplication.",
        turns=turns,
        before=before,
        after=after,
        evidence=evidence,
        counters=counters,
        verification={
            "conversation_id": str(conversation_id),
            "created": created,
            "active_task_after_repeat": restarted_task,
            "active_created": active,
            "exact_matching_active": exact,
            "repeat_confirmation_turn": 5,
        },
        deterministic_ok=len(active) == 1 and len(exact) == 1,
        expected="Exactly one canonical booking remains; the later 'احجزيه زي ما اتفقنا' is acknowledged or safely handled without a duplicate write.",
        issue_severity="P2",
        issue_title=(
            "Repeated confirmation after a long detour duplicated the completed booking"
            if len(active) > 1
            else "Repeated confirmation restarted a completed booking task"
        ),
    )


CASES: list[ScenarioFn] = [
    case_01_booking_doctor_info_price_resume,
    case_02_booking_multiple_corrections,
    case_03_laser_device_correction_after_side_reads,
    case_04_service_replacement_invalidates_old_device,
    case_05_standard_to_laser_requires_device,
    case_06_package_side_read_financial_boundary_resume,
    case_07_buy_package_then_continue_booking,
    case_08_package_changes_externally_before_booking,
    case_09_booking_detour_then_reschedule,
    case_10_two_appointments_persisted_reschedule_target,
    case_11_cancel_one_then_modify_other,
    case_12_reception_edits_appointment_during_conversation,
    case_13_external_cancel_before_followup_action,
    case_14_abandon_old_booking_start_new,
    case_15_ambiguous_new_intent_preserves_active_task,
    case_16_completed_booking_long_detour_repeat_confirmation,
]


def _run_case(engine, workspace_slug: str, case_fn: ScenarioFn) -> ScenarioResult:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        acquire_eval_advisory_lock(db, namespace="tia-agent-eval-batch-07")
        workspace = db.scalar(select(Workspace).where(Workspace.slug == workspace_slug))
        if workspace is None:
            raise RuntimeError("Workspace not found")
        assert_demo_only(workspace)
        _ensure_batch2_catalog_fixtures(db, workspace)
        return case_fn(db, workspace)
    except Exception as exc:  # noqa: BLE001
        return ScenarioResult(
            id=case_fn.__name__.removeprefix("case_"),
            category="infrastructure",
            purpose="Scenario execution failed before review.",
            turns=[],
            state_before={},
            state_after={},
            db_verification={
                "state_continuity_counters": _counter(),
            },
            evaluation=default_evaluation(
                db_ok=False,
                grounding_ok=False,
            ),
            issues=[],
            token_usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "uncached_input_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
                "metadata_missing_calls": 0,
            },
            execution_error=f"{type(exc).__name__}: {exc}",
            review={
                "status": "INFRASTRUCTURE_FAILURE",
                "expected": "",
                "observed": {},
                "reviewer_notes": f"{type(exc).__name__}: {exc}",
                "severity": None,
                "root_cause": "Infrastructure/provider noise or test-data problem",
            },
        )
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


def _state_counter_summary(results: list[ScenarioResult]) -> dict[str, int]:
    totals = {key: 0 for key in STALE_COUNTER_KEYS}
    for result in results:
        counters = result.db_verification.get("state_continuity_counters") or {}
        for key in totals:
            totals[key] += int(counters.get(key) or 0)
    return totals


def _write_reports(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# Tia Agent Evaluation — Batch 07 Long-Horizon / Cross-Intent Raw Baseline",
        "",
        f"- Batch runtime base SHA: {payload['run_metadata']['batch7_base_sha']}",
        f"- Harness SHA: {payload['run_metadata']['git_sha']}",
        f"- Scenario version: {payload['run_metadata']['scenario_version']}",
        f"- Fixture version: {payload['run_metadata']['demo_seed']['fixture_version']}",
        f"- Model: {payload['run_metadata']['model']}",
        f"- Reasoning: {payload['run_metadata']['reasoning_effort']}",
        f"- Scenarios executed: {len(payload['scenario_results'])}",
        "",
        "This is evaluation evidence only. Deterministic findings are guards; final severity and root-cause grouping require manual trace/DB review.",
        "",
    ]
    for row in payload["scenario_results"]:
        lines.extend(
            [
                f"## {row['id']}",
                "",
                f"Category: {row['category']}",
                f"Purpose: {row['purpose']}",
                f"Review status: {row['review']['status']}",
                f"Expected: {row['review']['expected']}",
                "",
            ]
        )
        for turn in row["turns"]:
            lines.append(f"Customer {turn['turn_number']}: {turn['user_message']}")
            lines.append(f"Tia: {turn['agent_response']}")
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
        lines.append("State / DB verification:")
        lines.append(json.dumps(row["db_verification"], ensure_ascii=False, indent=2))
        if row["issues"]:
            lines.append("Deterministic findings:")
            for issue in row["issues"]:
                lines.append(
                    f"- {issue['severity']}: {issue['title']} — {issue['detail']}"
                )
        else:
            lines.append("Deterministic findings: none; manual review still required.")
        lines.append("")
    lines.extend(
        [
            "## Batch summary",
            "",
            json.dumps(payload["batch_summary"], ensure_ascii=False, indent=2),
        ]
    )
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
                }
                for turn in row.get("turns") or []
            ],
            "state_continuity_counters": (
                row.get("db_verification", {}).get("state_continuity_counters") or {}
            ),
        }
        print(
            "EVAL_SCENARIO_COMPACT="
            + json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
    print("EVAL_COMPACT_END", flush=True)


def main() -> int:
    ns = parse_args()
    require_explicit_demo_eval()
    if not all(
        value > 0
        for value in (
            ns.input_price_per_million,
            ns.cached_input_price_per_million,
            ns.output_price_per_million,
        )
    ):
        raise RuntimeError("Current provider pricing must be supplied explicitly.")

    engine = __import__("sqlalchemy").create_engine(
        settings.database_url,
        pool_pre_ping=True,
    )
    with Session(engine) as db:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == ns.workspace_slug))
        if workspace is None:
            raise RuntimeError("Demo workspace not found.")
        assert_demo_only(workspace)
        _ensure_batch2_catalog_fixtures(db, workspace)
        catalog = build_clinic_catalog(db, workspace)
        demo_seed = {
            "workspace_id": str(workspace.id),
            "workspace_slug": workspace.slug,
            "history_loader_limit": settings.agent_history_messages,
            "fixture_version": BATCH7_FIXTURE_VERSION,
            "active_branch_count": len(
                [
                    row
                    for row in catalog.get("branches", [])
                    if isinstance(row, dict) and row.get("is_active", True)
                ]
            ),
            "service_count": len(
                [
                    row
                    for row in catalog.get("services", [])
                    if isinstance(row, dict) and row.get("is_active", True)
                ]
            ),
            "doctor_count": len(
                [
                    row
                    for row in catalog.get("doctors", [])
                    if isinstance(row, dict) and row.get("id")
                ]
            ),
            "package_offer_count": len(
                list_package_offers(
                    db,
                    workspace_id=workspace.id,
                    active_only=True,
                )
            ),
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
    state_counters = _state_counter_summary(results)
    total_actual_cost = sum(float(row.cost.get("actual_total_usd") or 0) for row in results)
    total_without_cache = sum(
        float(row.cost.get("without_explicit_cache_usd") or 0) for row in results
    )
    total_turns = int(summary.get("total_turns") or 0)
    total_calls = int(summary.get("total_llm_calls") or 0)
    total_tokens = int(summary.get("tokens", {}).get("total_tokens") or 0)
    summary["state_continuity_counters"] = state_counters
    summary["average_turns_per_scenario"] = round(
        total_turns / len(results), 2
    ) if results else 0.0
    summary["tokens_per_customer_turn"] = round(
        total_tokens / total_turns, 2
    ) if total_turns else 0.0
    summary["llm_calls_per_customer_turn"] = round(
        total_calls / total_turns, 3
    ) if total_turns else 0.0
    summary["provider_latency_ms"] = int(
        summary.get("stage_metrics", {}).get("all", {}).get("latency_ms") or 0
    )
    summary["e2e_latency_ms"] = sum(
        int(turn.latency_ms) for row in results for turn in row.turns
    )
    summary["retries"] = sum(
        int(summary.get("stage_metrics", {}).get(stage, {}).get("retries") or 0)
        for stage in ("interpreter", "responder")
    )
    summary["fallbacks"] = sum(
        int(summary.get("stage_metrics", {}).get(stage, {}).get("fallback_calls") or 0)
        for stage in ("interpreter", "responder")
    )
    summary["cost"] = {
        "actual_usd": round(total_actual_cost, 8),
        "without_explicit_cache_usd": round(total_without_cache, 8),
        "saving_usd": round(max(0.0, total_without_cache - total_actual_cost), 8),
        "saving_percent": round(
            ((total_without_cache - total_actual_cost) / total_without_cache * 100.0)
            if total_without_cache > 0
            else 0.0,
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
            "batch7_base_sha": ns.batch7_base_sha,
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
    json_path = output_dir / f"batch_07_raw_{timestamp}.json"
    md_path = output_dir / f"batch_07_raw_{timestamp}.md"
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
    print(f"TOKENS_PER_TURN={summary['tokens_per_customer_turn']}")
    print(f"LLM_CALLS_PER_TURN={summary['llm_calls_per_customer_turn']}")
    print(f"ACTUAL_COST_USD={summary['cost']['actual_usd']}")
    print(f"WITHOUT_CACHE_USD={summary['cost']['without_explicit_cache_usd']}")
    print(f"CACHE_SAVING_PERCENT={summary['cost']['saving_percent']}")
    print(
        "STATE_CONTINUITY_COUNTERS="
        + json.dumps(state_counters, separators=(",", ":"))
    )
    return 2 if stopped_for_p0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
