from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from langchain_openai import ChatOpenAI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import v2_benchmark_core as core
import v2_benchmark_scenarios as scenarios
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import Appointment
from app.models.message import Message
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.agent_v2 import orchestrator as orchestrator_module

TARGET_HEAD = "17463a79dff3580ee661ad53c5ece32852237e85"
APPROVED_BASE = "555bcf496665aa7691a301f27c5ea96ef60aeab1"
REPORT = Path("artifacts/f8-targeted-live.json")
SCHEMA_REPORT = Path("artifacts/f8-evidence-schema-preflight.json")
_PREFLIGHT_PROVIDER_CALLS = 0

PORTFOLIO = (
    ("f8_live_l1_one_hop_preserve", "L1", 3, "appointment one-hop continuity across pricing read"),
    ("f8_live_l2_second_hop_clear", "L2", 4, "appointment anchor clears after second harmless side read"),
    ("f8_live_l3_explicit_b_wins", "L3", 3, "explicit different appointment authority replaces carried anchor"),
)
ORDER = [row[0] for row in PORTFOLIO]
META = {
    row[0]: {"label": row[1], "planned_turns": row[2], "topic": row[3]}
    for row in PORTFOLIO
}

_EVIDENCE_TURNS: list[dict[str, Any]] = []
_CURRENT_SEMANTIC_INPUTS: list[dict[str, Any]] = []
_CURRENT_SCENARIO = ""
_CURRENT_TURN = 0
_ORIGINAL_CORE_SEND = core.send
_ORIGINAL_INTERPRET = orchestrator_module.interpret_customer_turn_v2


def git_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def verify_output_path() -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    probe = REPORT.parent / ".f8-preflight-write-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def install_provider_forbidden_guard() -> None:
    global _PREFLIGHT_PROVIDER_CALLS
    _PREFLIGHT_PROVIDER_CALLS = 0

    def forbidden_generate(self, *args, **kwargs):
        global _PREFLIGHT_PROVIDER_CALLS
        _PREFLIGHT_PROVIDER_CALLS += 1
        raise RuntimeError("Provider invocation is forbidden during F8 preflight")

    ChatOpenAI._generate = forbidden_generate


def _service_for_appointment(db: Session, appointment: Appointment) -> Service:
    service = db.get(Service, appointment.service_id)
    if service is None:
        raise RuntimeError("F8 fixture appointment service missing")
    return service


def _appointment_fixture(db: Session, workspace: Workspace):
    patient = core.patient_without_future(db, workspace)
    appointment = core.base._seed_upcoming(db, workspace, patient, 1)[0]
    service = _service_for_appointment(db, appointment)
    tz = ZoneInfo(workspace.timezone)
    local = appointment.start_at.astimezone(tz)
    return patient, appointment, service, tz, local


def _seed_later_appointment(
    db: Session,
    workspace: Workspace,
    *,
    patient,
    source: Appointment,
) -> Appointment:
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    tz = ZoneInfo(workspace.timezone)
    start_day = source.start_at.astimezone(tz).date()
    for offset in range(1, 22):
        day = start_day + timedelta(days=offset)
        result = adapter.get_availability(
            AvailabilityRequest(
                branch_id=str(source.branch_id),
                service_id=str(source.service_id),
                booking_date=day,
                doctor_id=str(source.doctor_id),
            )
        )
        if not result.slots:
            continue
        slot = result.slots[0]
        row = Appointment(
            workspace_id=workspace.id,
            patient_id=patient.id,
            branch_id=UUID(str(slot.branch_id)),
            doctor_id=UUID(str(slot.doctor_id)),
            service_id=UUID(str(slot.service_id)),
            status="confirmed",
            source="staff",
            start_at=slot.start_at,
            end_at=slot.end_at,
            busy_start_at=slot.start_at,
            busy_end_at=slot.end_at,
            duration_minutes=slot.duration_minutes,
            price_minor=slot.price_minor,
            currency=slot.currency,
            payment_status="unpaid",
            payment_method="unknown",
            billing_context="standard",
            confirmed_at=datetime.now(UTC),
        )
        db.add(row)
        db.flush()
        return row
    raise RuntimeError("F8 L3 fixture could not seed a later distinct-day appointment")


def f8_l1(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient, appointment, service, _tz, local = _appointment_fixture(db, workspace)
    actual = local.strftime("%H:%M")
    claimed = (local + timedelta(hours=1)).strftime("%H:%M")
    return core.PreparedScenario(
        patient=patient,
        messages=[
            f"ممكن تفكرني بتفاصيل ميعادي لـ{service.name} يوم {local.date().isoformat()}؟",
            f"وعلى جنب، سعر جلسة {service.name} كام دلوقتي؟",
            f"أنا فاكر إن نفس المعاد كان الساعة {claimed} مش {actual}، صح ولا أنا لخبطت؟",
        ],
        observations=[
            "f8_live_case=L1_one_hop_preserve",
            f"appointment_a_id={appointment.id}",
            f"appointment_a_actual_time={actual}",
            f"claimed_time={claimed}",
            "expected_write_delta=0",
        ],
    )


def f8_l2(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient, appointment, service, _tz, local = _appointment_fixture(db, workspace)
    actual = local.strftime("%H:%M")
    claimed = (local + timedelta(hours=1)).strftime("%H:%M")
    return core.PreparedScenario(
        patient=patient,
        messages=[
            f"فكرني بميعادي لـ{service.name} يوم {local.date().isoformat()} لو سمحت.",
            f"بالمناسبة، سعر جلسة {service.name} كام؟",
            "وطب الجلسة نفسها مدتها قد إيه تقريبًا؟",
            f"طيب أنا فاكر إن المعاد كان الساعة {claimed}، صح؟",
        ],
        observations=[
            "f8_live_case=L2_second_hop_clear",
            f"appointment_a_id={appointment.id}",
            f"appointment_a_actual_time={actual}",
            f"claimed_time={claimed}",
            "expected_second_side_anchor_absent=true",
            "expected_write_delta=0",
        ],
    )


def f8_l3(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient, appointment_a, service, tz, local_a = _appointment_fixture(db, workspace)
    appointment_b = _seed_later_appointment(
        db,
        workspace,
        patient=patient,
        source=appointment_a,
    )
    local_b = appointment_b.start_at.astimezone(tz)
    actual_b = local_b.strftime("%H:%M")
    claimed_b = (local_b + timedelta(hours=1)).strftime("%H:%M")
    return core.PreparedScenario(
        patient=patient,
        messages=[
            f"راجعلي ميعاد {service.name} اللي عندي يوم {local_a.date().isoformat()}، الساعة كام؟",
            f"وعلى السريع، جلسة {service.name} سعرها كام؟",
            (
                f"وبالنسبة للمعاد التاني لنفس الخدمة يوم {local_b.date().isoformat()}، "
                f"أنا فاكره الساعة {claimed_b} مش {actual_b}؛ راجعلي الوقت الصح."
            ),
        ],
        observations=[
            "f8_live_case=L3_explicit_b_wins",
            f"appointment_a_id={appointment_a.id}",
            f"appointment_b_id={appointment_b.id}",
            f"appointment_b_date={local_b.date().isoformat()}",
            f"appointment_b_actual_time={actual_b}",
            f"claimed_time={claimed_b}",
            "expected_write_delta=0",
        ],
    )


def install_handlers() -> None:
    scenarios._HANDLERS.update(
        {
            "f8_live_l1_one_hop_preserve": f8_l1,
            "f8_live_l2_second_hop_clear": f8_l2,
            "f8_live_l3_explicit_b_wins": f8_l3,
        }
    )
    for sid, meta in META.items():
        scenarios.TOPICS[sid] = meta["topic"]


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_content_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value or "")


def stage_from_messages(messages: Any) -> str:
    text = "\n".join(
        _content_text(getattr(message, "content", "")) for message in (messages or [])
    )
    markers = (
        ("V2 semantic turn interpreter", "full_turn_interpreter"),
        ("narrow semantic resolver for the latest availability follow-up", "availability_reference"),
        ("presentation structure for verified clinic availability", "availability_composer"),
        ("presentation structure for completed clinic actions", "terminal_composer"),
        ("presentation structure for verified clinic pricing", "price_device_composer"),
        ("customer-facing assistant for an aesthetic clinic", "generic_responder"),
    )
    for marker, stage in markers:
        if marker in text:
            return stage
    return "unknown"


def install_stage_telemetry() -> None:
    original = core._ORIGINAL_GENERATE

    def capture(self, *args, **kwargs):
        messages = args[0] if args else kwargs.get("messages")
        stage = stage_from_messages(messages)
        result = original(self, *args, **kwargs)
        usage: dict[str, Any] = {}
        llm_output = getattr(result, "llm_output", None)
        if isinstance(llm_output, dict) and isinstance(llm_output.get("token_usage"), dict):
            usage = dict(llm_output["token_usage"])
        if not usage:
            try:
                raw = getattr(result.generations[0].message, "usage_metadata", None)
                if isinstance(raw, dict):
                    usage = dict(raw)
            except Exception:
                pass

        def pick(*keys: str) -> int:
            for key in keys:
                value = usage.get(key)
                if isinstance(value, int):
                    return value
            return 0

        input_tokens = pick("prompt_tokens", "input_tokens", "input_token_count")
        output_tokens = pick("completion_tokens", "output_tokens", "output_token_count")
        total_tokens = pick("total_tokens", "total_token_count") or input_tokens + output_tokens
        model_name = getattr(self, "model_name", None) or getattr(self, "model", None)
        core._TOKEN_BUCKET.append(
            {
                "stage": stage,
                "model": str(model_name) if model_name else None,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            }
        )
        return result

    ChatOpenAI._generate = capture


def _latest_v2_outbound_metadata(
    db: Session,
    *,
    workspace: Workspace,
    conversation_id: UUID | None,
) -> dict[str, Any] | None:
    if conversation_id is None:
        return None
    message = db.scalar(
        select(Message)
        .where(
            Message.workspace_id == workspace.id,
            Message.conversation_id == conversation_id,
            Message.sender_type == "ai",
            Message.direction == "outbound",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    if message is None:
        return None
    metadata = dict(message.metadata_json or {})
    return metadata if metadata.get("runtime") == "v2" else None


def _capture_interpreter(*args, **kwargs):
    semantic_context = kwargs.get("semantic_context")
    recent: dict[str, Any] | None = None
    appointment_ref = None
    resolved_id = None
    if semantic_context is not None:
        raw = semantic_context.model_input.get("recent_verified_read")
        recent = core.jsonable(raw) if isinstance(raw, dict) else None
        if isinstance(raw, dict):
            value = raw.get("appointment_ref")
            if isinstance(value, str) and value:
                appointment_ref = value
                resolved_id = semantic_context.resolve(value, expected_kind="appointment")
    _CURRENT_SEMANTIC_INPUTS.append(
        {
            "recent_verified_read": recent,
            "appointment_ref_present": appointment_ref is not None,
            "appointment_ref": appointment_ref,
            "appointment_ref_resolves_to": str(resolved_id) if resolved_id is not None else None,
        }
    )
    return _ORIGINAL_INTERPRET(*args, **kwargs)


def _plan_evidence(plan: dict[str, Any] | None) -> dict[str, Any]:
    steps = plan.get("steps") if isinstance(plan, dict) else None
    reads: list[dict[str, Any]] = []
    write_intents: list[Any] = []
    operations: list[str] = []
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            operation = step.get("operation_type")
            if isinstance(operation, str):
                operations.append(operation)
            raw_reads = step.get("reads")
            if isinstance(raw_reads, list):
                reads.extend(item for item in raw_reads if isinstance(item, dict))
            write_intents.append(step.get("write_intent"))
    appointment_read_ids = []
    for read in reads:
        if read.get("kind") != "appointments":
            continue
        parameters = read.get("parameters")
        if isinstance(parameters, dict) and parameters.get("appointment_id") not in (None, ""):
            appointment_read_ids.append(str(parameters["appointment_id"]))
    return {
        "operations": operations,
        "reads": reads,
        "appointment_read_ids": appointment_read_ids,
        "write_intents": write_intents,
        "has_write_intent": any(value is not None for value in write_intents),
    }


def _understanding_evidence(understanding: dict[str, Any] | None) -> list[dict[str, Any]]:
    operations = understanding.get("operations") if isinstance(understanding, dict) else None
    output: list[dict[str, Any]] = []
    if not isinstance(operations, list):
        return output
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        entities = operation.get("entities") if isinstance(operation.get("entities"), dict) else {}
        output.append(
            {
                "type": operation.get("type"),
                "appointment_fact_challenge": operation.get("appointment_fact_challenge"),
                "continues_previous": operation.get("continues_previous"),
                "fresh_task": operation.get("fresh_task"),
                "active_task_relationship": operation.get("active_task_relationship"),
                "claimed_time": entities.get("time"),
                "appointment_entity": entities.get("appointment"),
            }
        )
    return output


def _context_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("v2_read_context")
    return core.jsonable(value) if isinstance(value, dict) else None


def _make_turn_evidence(
    *,
    scenario: str,
    turn_number: int,
    customer: str,
    assistant: str | None,
    previous_context: dict[str, Any] | None,
    current_context: dict[str, Any] | None,
    semantic_inputs: list[dict[str, Any]],
    understanding: dict[str, Any] | None,
    plan: dict[str, Any] | None,
    db_before: dict[str, Any],
    db_after: dict[str, Any],
    reference_semantic: dict[str, Any] | None,
) -> dict[str, Any]:
    plan_evidence = _plan_evidence(plan)
    semantic = semantic_inputs[-1] if semantic_inputs else {
        "recent_verified_read": None,
        "appointment_ref_present": False,
        "appointment_ref": None,
        "appointment_ref_resolves_to": None,
    }
    return {
        "scenario": scenario,
        "turn_number": turn_number,
        "customer": customer,
        "assistant": assistant,
        "previous_v2_read_context": previous_context,
        "current_v2_read_context": current_context,
        "previous_appointment_id": (
            previous_context.get("appointment_id") if isinstance(previous_context, dict) else None
        ),
        "current_appointment_id": (
            current_context.get("appointment_id") if isinstance(current_context, dict) else None
        ),
        "current_operation_type": (
            current_context.get("operation_type") if isinstance(current_context, dict) else None
        ),
        "recent_verified_read": semantic.get("recent_verified_read"),
        "appointment_ref_present": bool(semantic.get("appointment_ref_present")),
        "appointment_ref": semantic.get("appointment_ref"),
        "appointment_ref_resolves_to": semantic.get("appointment_ref_resolves_to"),
        "semantic_inputs": semantic_inputs,
        "structured_understanding": understanding,
        "structured_operations": _understanding_evidence(understanding),
        "planner": plan_evidence,
        "canonical_appointment_read_ids": plan_evidence["appointment_read_ids"],
        "write_intent_present": plan_evidence["has_write_intent"],
        "reference_semantic": reference_semantic,
        "db_before": db_before,
        "db_after": db_after,
        "db_delta": core.delta(db_before, db_after),
    }


def _evidence_send(
    db: Session,
    workspace: Workspace,
    patient,
    message: str,
    conversation_id: UUID | None,
):
    global _CURRENT_TURN
    previous_metadata = _latest_v2_outbound_metadata(
        db,
        workspace=workspace,
        conversation_id=conversation_id,
    )
    previous_context = _context_from_metadata(previous_metadata)
    before = core.state(db, workspace, patient)
    _CURRENT_SEMANTIC_INPUTS.clear()
    record, new_conversation_id = _ORIGINAL_CORE_SEND(
        db,
        workspace,
        patient,
        message,
        conversation_id,
    )
    after = core.state(db, workspace, patient)
    current_metadata = _latest_v2_outbound_metadata(
        db,
        workspace=workspace,
        conversation_id=new_conversation_id,
    )
    current_context = _context_from_metadata(current_metadata)
    reference_semantic = (
        core.jsonable(current_metadata.get("v2_reference_semantic"))
        if isinstance(current_metadata, dict)
        and isinstance(current_metadata.get("v2_reference_semantic"), dict)
        else None
    )
    _CURRENT_TURN += 1
    _EVIDENCE_TURNS.append(
        _make_turn_evidence(
            scenario=_CURRENT_SCENARIO,
            turn_number=_CURRENT_TURN,
            customer=message,
            assistant=record.assistant,
            previous_context=previous_context,
            current_context=current_context,
            semantic_inputs=list(_CURRENT_SEMANTIC_INPUTS),
            understanding=record.understanding,
            plan=record.plan,
            db_before=before,
            db_after=after,
            reference_semantic=reference_semantic,
        )
    )
    return record, new_conversation_id


def install_evidence_capture() -> None:
    orchestrator_module.interpret_customer_turn_v2 = _capture_interpreter
    core.send = _evidence_send


def fixture_preflight() -> dict[str, Any]:
    install_handlers()
    verify_output_path()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    rows: list[dict[str, Any]] = []
    try:
        with engine.connect() as bootstrap_connection:
            bootstrap_connection.exec_driver_sql("select 1")
        for sid in ORDER:
            connection = engine.connect()
            outer = connection.begin()
            db = Session(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                workspace = db.scalar(
                    select(Workspace).where(Workspace.slug == core.WORKSPACE_SLUG)
                )
                if workspace is None:
                    raise RuntimeError("Workspace tia not found")
                prepared = scenarios.prepare_scenario(db, workspace, sid)
                expected = META[sid]["planned_turns"]
                if len(prepared.messages) != expected:
                    raise RuntimeError(
                        f"{sid}: expected {expected} messages, got {len(prepared.messages)}"
                    )
                rows.append(
                    {
                        "scenario": sid,
                        "label": META[sid]["label"],
                        "messages": list(prepared.messages),
                        "observations": list(prepared.observations),
                        "status": "READY",
                    }
                )
            finally:
                db.close()
                if outer.is_active:
                    outer.rollback()
                connection.close()
    finally:
        engine.dispose()
    if _PREFLIGHT_PROVIDER_CALLS != 0:
        raise RuntimeError(
            f"Provider calls occurred during fixture preflight: {_PREFLIGHT_PROVIDER_CALLS}"
        )
    return {
        "head": git_head(),
        "scenario_count": len(rows),
        "turns": sum(META[sid]["planned_turns"] for sid in ORDER),
        "provider_calls": _PREFLIGHT_PROVIDER_CALLS,
        "database_bootstrap": "PASS",
        "scenario_reset_capability": "PASS",
        "transaction_rollbacks": len(rows),
        "database_writes_persisted": False,
        "evidence_output_path": str(REPORT),
        "scenarios": rows,
    }


def evidence_schema_preflight() -> dict[str, Any]:
    verify_output_path()
    sample = _make_turn_evidence(
        scenario="synthetic_f8_observability_sample",
        turn_number=3,
        customer="synthetic challenge",
        assistant="synthetic canonical answer",
        previous_context={
            "operation_type": "pricing",
            "service_id": "service-1",
            "appointment_id": "appointment-A",
        },
        current_context={
            "operation_type": "appointment_list",
            "appointment_id": "appointment-A",
        },
        semantic_inputs=[
            {
                "recent_verified_read": {
                    "operation_type": "pricing",
                    "service_ref": "S1",
                    "appointment_ref": "A1",
                },
                "appointment_ref_present": True,
                "appointment_ref": "A1",
                "appointment_ref_resolves_to": "appointment-A",
            }
        ],
        understanding={
            "operations": [
                {
                    "type": "appointment_list",
                    "entities": {
                        "time": {
                            "mode": "exact",
                            "start_time": "11:00",
                        }
                    },
                    "appointment_fact_challenge": "time",
                    "continues_previous": True,
                    "fresh_task": False,
                    "active_task_relationship": "unspecified",
                }
            ]
        },
        plan={
            "steps": [
                {
                    "operation_type": "appointment_list",
                    "reads": [
                        {
                            "kind": "appointments",
                            "parameters": {"appointment_id": "appointment-A"},
                        }
                    ],
                    "write_intent": None,
                }
            ]
        },
        db_before={"appointments": 1, "payments": 0},
        db_after={"appointments": 1, "payments": 0},
        reference_semantic=None,
    )
    required = {
        "previous_v2_read_context",
        "current_v2_read_context",
        "current_operation_type",
        "appointment_ref_present",
        "appointment_ref_resolves_to",
        "structured_understanding",
        "planner",
        "canonical_appointment_read_ids",
        "write_intent_present",
        "db_before",
        "db_after",
        "db_delta",
    }
    missing = sorted(required - set(sample))
    if missing:
        raise RuntimeError(f"F8 evidence schema missing fields: {missing}")
    if sample["previous_v2_read_context"]["appointment_id"] != "appointment-A":
        raise RuntimeError("F8 evidence schema failed previous context serialization")
    if sample["current_v2_read_context"]["appointment_id"] != "appointment-A":
        raise RuntimeError("F8 evidence schema failed current context serialization")
    if sample["appointment_ref_present"] is not True:
        raise RuntimeError("F8 evidence schema failed appointment_ref presence")
    if sample["appointment_ref_resolves_to"] != "appointment-A":
        raise RuntimeError("F8 evidence schema failed appointment_ref resolution")
    if sample["canonical_appointment_read_ids"] != ["appointment-A"]:
        raise RuntimeError("F8 evidence schema failed canonical appointment reread serialization")
    if sample["write_intent_present"] is not False:
        raise RuntimeError("F8 evidence schema failed write-intent serialization")
    payload = {
        "head": git_head(),
        "status": "PASS",
        "provider_calls": _PREFLIGHT_PROVIDER_CALLS,
        "required_fields": sorted(required),
        "synthetic_sample": sample,
    }
    if payload["provider_calls"] != 0:
        raise RuntimeError(
            f"Provider calls occurred during evidence-schema preflight: {payload['provider_calls']}"
        )
    SCHEMA_REPORT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return payload


def percentile(values: list[int], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    lo, hi = math.floor(index), math.ceil(index)
    if lo == hi:
        return float(ordered[lo])
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def quality_flags(reply: str | None) -> dict[str, bool]:
    text = str(reply or "")
    lowered = text.casefold()
    return {
        "empty": not bool(text.strip()),
        "uuid_leak": bool(
            re.search(
                r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
                text,
                re.I,
            )
        ),
        "internal_marker_leak": any(
            marker in lowered
            for marker in (
                "option_ref",
                "response_goal",
                "write_ready",
                "structured_understanding",
                "appointment_ref",
            )
        ),
        "error_text": any(
            marker in lowered
            for marker in ("traceback", "internal server error", "structuredoutputerror")
        ),
    }


def run_targeted() -> int:
    global _CURRENT_SCENARIO, _CURRENT_TURN
    if git_head() != TARGET_HEAD:
        raise RuntimeError(f"Exact-head guard failed: {git_head()} != {TARGET_HEAD}")
    install_handlers()
    install_stage_telemetry()
    install_evidence_capture()
    _EVIDENCE_TURNS.clear()
    started = datetime.now(UTC)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    results = []
    try:
        for index, sid in enumerate(ORDER, 1):
            _CURRENT_SCENARIO = sid
            _CURRENT_TURN = 0
            print(f"[{index:02d}/{len(ORDER)}] {sid}", flush=True)
            result = core.execute_one(engine, sid, META[sid]["topic"])
            results.append(result)
            print(
                "F8_SCENARIO="
                + json.dumps(
                    {
                        "scenario": sid,
                        "label": META[sid]["label"],
                        "error": result.error,
                        "turns": len(result.turns),
                        "db_delta": result.db_delta,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                flush=True,
            )
    finally:
        engine.dispose()

    evidence_map = {
        (item["scenario"], int(item["turn_number"])): item
        for item in _EVIDENCE_TURNS
    }
    conversations = []
    all_calls: list[dict[str, Any]] = []
    durations: list[int] = []
    for result in results:
        item = asdict(result)
        sid = result.name
        item["portfolio"] = META[sid]
        for turn_index, turn in enumerate(item["turns"], 1):
            turn["turn_number"] = turn_index
            turn["quality_flags"] = quality_flags(turn.get("assistant"))
            turn["f8_evidence"] = evidence_map.get((sid, turn_index))
            for call in (turn.get("token_usage") or {}).get("calls", []):
                all_calls.append({"scenario": sid, "turn": turn_index, **call})
            durations.append(int(turn.get("duration_ms") or 0))
        conversations.append(item)

    by_stage: dict[str, dict[str, Any]] = {}
    for call in all_calls:
        stage = str(call.get("stage") or "unknown")
        bucket = by_stage.setdefault(
            stage,
            {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "models": Counter(),
            },
        )
        bucket["calls"] += 1
        bucket["input_tokens"] += int(call.get("input_tokens") or 0)
        bucket["output_tokens"] += int(call.get("output_tokens") or 0)
        bucket["total_tokens"] += int(call.get("total_tokens") or 0)
        bucket["models"][str(call.get("model"))] += 1
    for bucket in by_stage.values():
        bucket["models"] = dict(bucket["models"])

    same_stage_repeats = []
    for item in conversations:
        for turn in item["turns"]:
            counts = Counter(
                str(call.get("stage") or "unknown")
                for call in (turn.get("token_usage") or {}).get("calls", [])
            )
            for stage, count in counts.items():
                if count > 1:
                    same_stage_repeats.append(
                        {
                            "scenario": item["name"],
                            "turn": turn["turn_number"],
                            "stage": stage,
                            "count": count,
                        }
                    )
    fallback_calls = [
        call
        for call in all_calls
        if str(call.get("model") or "").casefold() == "gpt-5-mini"
    ]
    unknown_stage_calls = [
        call for call in all_calls if call.get("stage") == "unknown"
    ]
    payload = {
        "audit_metadata": {
            "audit_name": "SENIOR-CORRECTNESS-F8-03 POST-MERGE TARGETED LIVE",
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "head": git_head(),
            "approved_base": APPROVED_BASE,
            "approved_head": TARGET_HEAD,
            "model_configuration": {
                "provider": settings.llm_provider,
                "primary_model": settings.openai_model,
                "fallback_model": settings.openai_fallback_model,
                "reasoning_effort": settings.openai_reasoning_effort,
                "fallback_reasoning_effort": settings.openai_fallback_reasoning_effort,
            },
            "database_writes_persisted": False,
            "c7": "OUT_OF_SCOPE",
            "phase_3_3": "NOT_RUN",
        },
        "inventory": {
            "conversation_count": len(PORTFOLIO),
            "planned_turns": sum(row[2] for row in PORTFOLIO),
            "scenarios": [{"id": row[0], **META[row[0]]} for row in PORTFOLIO],
        },
        "conversations": conversations,
        "turn_evidence": list(_EVIDENCE_TURNS),
        "telemetry": {
            "generations": len(all_calls),
            "input_tokens": sum(int(call.get("input_tokens") or 0) for call in all_calls),
            "output_tokens": sum(int(call.get("output_tokens") or 0) for call in all_calls),
            "total_tokens": sum(int(call.get("total_tokens") or 0) for call in all_calls),
            "by_stage": by_stage,
            "same_stage_repeats": same_stage_repeats,
            "fallback_calls": fallback_calls,
            "unknown_stage_calls": unknown_stage_calls,
        },
        "latency": {
            "turns": len(durations),
            "p50_ms": percentile(durations, 0.50),
            "p90_ms": percentile(durations, 0.90),
            "p95_ms": percentile(durations, 0.95),
            "max_ms": max(durations) if durations else None,
            "over_15s": sum(value > 15000 for value in durations),
            "over_20s": sum(value > 20000 for value in durations),
        },
        "runtime_errors": [
            {"scenario": result.name, "error": result.error}
            for result in results
            if result.error
        ],
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        "F8_SUMMARY="
        + json.dumps(
            {
                "conversations": len(conversations),
                "turns": sum(len(item["turns"]) for item in conversations),
                "generations": len(all_calls),
                "runtime_errors": len(payload["runtime_errors"]),
                "fallback_calls": len(fallback_calls),
                "same_stage_repeats": len(same_stage_repeats),
                "unknown_stage_calls": len(unknown_stage_calls),
                "evidence_turns": len(_EVIDENCE_TURNS),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    print(f"REPORT={REPORT}", flush=True)
    return 1 if payload["runtime_errors"] else 0


def inventory_only() -> int:
    if git_head() != TARGET_HEAD:
        raise RuntimeError(f"Exact-head guard failed: {git_head()} != {TARGET_HEAD}")
    install_provider_forbidden_guard()
    payload = {
        "target_head": TARGET_HEAD,
        "tested_head": git_head(),
        "conversation_count": len(PORTFOLIO),
        "planned_turns": sum(row[2] for row in PORTFOLIO),
        "provider_calls": _PREFLIGHT_PROVIDER_CALLS,
        "scenarios": [{"id": row[0], **META[row[0]]} for row in PORTFOLIO],
    }
    if payload["conversation_count"] != 3:
        raise RuntimeError(f"F8 inventory conversation mismatch: {payload}")
    if payload["planned_turns"] != 10:
        raise RuntimeError(f"F8 inventory turn mismatch: {payload}")
    if payload["provider_calls"] != 0:
        raise RuntimeError(
            f"Provider calls occurred during inventory preflight: {payload['provider_calls']}"
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--fixture-preflight", action="store_true")
    parser.add_argument("--evidence-schema-preflight", action="store_true")
    args = parser.parse_args()
    if args.inventory_only:
        return inventory_only()
    if args.fixture_preflight:
        if git_head() != TARGET_HEAD:
            raise RuntimeError(f"Exact-head guard failed: {git_head()} != {TARGET_HEAD}")
        install_provider_forbidden_guard()
        print(
            json.dumps(
                fixture_preflight(),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    if args.evidence_schema_preflight:
        if git_head() != TARGET_HEAD:
            raise RuntimeError(f"Exact-head guard failed: {git_head()} != {TARGET_HEAD}")
        install_provider_forbidden_guard()
        print(
            json.dumps(
                evidence_schema_preflight(),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    return run_targeted()


if __name__ == "__main__":
    raise SystemExit(main())
