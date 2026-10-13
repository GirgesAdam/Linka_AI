from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.agents.v2.turn_interpreter as turn_interpreter
import app.services.agent_v2.orchestrator as orchestrator_runtime
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.booking_settings import BookingSettings
from app.models.clinic_inventory import ServiceDevicePrice
from app.models.message import Message
from app.schemas.agent import AgentChatRequest
from app.services.agent_v2.live_chat import run_agent_chat
from app.services.agent_v2.state_persistence import load_active_task
from scripts.run_grouped_multiservice_intent_live_review import build_fixture, rollback_session

CAIRO = ZoneInfo("Africa/Cairo")
REPORT_PATH = Path("artifacts/explicit-daypart-live-review.json")

SAFETY_COUNTERS = {
    "wrong_time_writes": 0,
    "wrong_writes": 0,
    "false_success": 0,
    "silent_am_pm_mutation": 0,
    "wrong_device": 0,
    "cross_device_visits": 0,
    "lost_services": 0,
    "duplicate_appointments": 0,
    "duplicate_proposals": 0,
    "persisted_audit_writes": 0,
}

RAW: list[dict[str, object]] = []
_ORIGINAL_INVOKE = turn_interpreter.invoke_typed_structured_output


def _dump(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_dump(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def _capture_invoke(*args, **kwargs):
    value = _ORIGINAL_INVOKE(*args, **kwargs)
    RAW.append(_dump(value))
    return value


turn_interpreter.invoke_typed_structured_output = _capture_invoke

RUNTIME_TRACE: dict[str, Any] = {}
_ORIGINAL_AUTO_DEVICE = orchestrator_runtime._auto_resolve_single_booking_device
_ORIGINAL_PLAN_PROGRESS = orchestrator_runtime.plan_active_task_progress
_ORIGINAL_APPLY_STATE = orchestrator_runtime.apply_step_state
_ORIGINAL_EXECUTE_READS = orchestrator_runtime.execute_step_reads
_ORIGINAL_PREPARE_PROPOSAL = orchestrator_runtime._prepare_booking_proposal


def _reset_runtime_trace(*, active_task_before: Any, appointments_before: Any) -> None:
    RUNTIME_TRACE.clear()
    RUNTIME_TRACE.update(
        {
            "active_task_before": _dump(active_task_before),
            "appointments_before": _dump(appointments_before),
            "device_resolution": [],
            "read_batches": [],
            "availability_requests": [],
            "proposals_created": [],
        }
    )


def _capture_auto_device(step, reads):
    resolved = _ORIGINAL_AUTO_DEVICE(step, reads)
    if resolved.facts.get("device_auto_resolved") is True:
        RUNTIME_TRACE["device_resolution"].append(
            {
                "device_key": resolved.facts.get("device_key"),
                "compatible_visit_device_keys": resolved.facts.get("compatible_visit_device_keys"),
                "before_facts": _dump(step.facts),
                "after_facts": _dump(resolved.facts),
            }
        )
        RUNTIME_TRACE["_await_device_transition"] = True
    return resolved


def _capture_plan_progress(state, *, operation_index, context):
    planned = _ORIGINAL_PLAN_PROGRESS(
        state, operation_index=operation_index, context=context
    )
    if RUNTIME_TRACE.get("_device_transition_done") is True:
        RUNTIME_TRACE["second_progression_plan"] = _dump(planned)
        RUNTIME_TRACE["_await_second_transition"] = True
        RUNTIME_TRACE["_device_transition_done"] = False
    return planned


def _capture_apply_state(current_task, *args, **kwargs):
    step = kwargs.get("step")
    transition = _ORIGINAL_APPLY_STATE(current_task, *args, **kwargs)
    facts = getattr(step, "facts", {}) if step is not None else {}
    if isinstance(facts, dict) and facts.get("device_auto_resolved") is True:
        RUNTIME_TRACE["active_task_after_device_resolution"] = _dump(transition.active_task)
        RUNTIME_TRACE["_await_device_transition"] = False
        RUNTIME_TRACE["_device_transition_done"] = True
    elif RUNTIME_TRACE.get("_await_second_transition") is True:
        RUNTIME_TRACE["active_task_after_second_progression"] = _dump(transition.active_task)
        RUNTIME_TRACE["_await_second_transition"] = False
    return transition


def _capture_execute_reads(step, context):
    bundle = _ORIGINAL_EXECUTE_READS(step, context)
    requests = [
        {"kind": read.kind, "parameters": _dump(read.parameters)}
        for read in step.reads
    ]
    results = [
        {"kind": result.kind, "ok": result.ok, "payload": _dump(result.payload)}
        for result in bundle.results
    ]
    RUNTIME_TRACE["read_batches"].append(
        {
            "requests": requests,
            "results": results,
            "verification": _dump(bundle.verification),
        }
    )
    for request in requests:
        if request["kind"] == "availability":
            RUNTIME_TRACE["availability_requests"].append(request["parameters"])
    return bundle


def _capture_prepare_proposal(step, *args, **kwargs):
    advanced, proposal = _ORIGINAL_PREPARE_PROPOSAL(step, *args, **kwargs)
    if proposal is not None:
        RUNTIME_TRACE["proposals_created"].append(_dump(proposal))
    return advanced, proposal


orchestrator_runtime._auto_resolve_single_booking_device = _capture_auto_device
orchestrator_runtime.plan_active_task_progress = _capture_plan_progress
orchestrator_runtime.apply_step_state = _capture_apply_state
orchestrator_runtime.execute_step_reads = _capture_execute_reads
orchestrator_runtime._prepare_booking_proposal = _capture_prepare_proposal


@dataclass
class Context:
    scenario: str
    db: Session
    workspace: Any
    patient: Any
    doctor: Any
    underarm: Any
    bikini: Any
    arms: Any
    conversation_id: UUID | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)


def _setup(db: Session, scenario: str, *, hole: bool = False) -> Context:
    workspace, patient, doctor, underarm, bikini, arms = build_fixture(db)
    if hole:
        bikini.duration_minutes = 15
        price = db.scalar(
            select(ServiceDevicePrice).where(
                ServiceDevicePrice.workspace_id == workspace.id,
                ServiceDevicePrice.service_id == bikini.id,
                ServiceDevicePrice.device_key == "candela_gentle",
            )
        )
        assert price is not None
        price.duration_minutes = 15
        settings_row = db.scalar(
            select(BookingSettings).where(BookingSettings.workspace_id == workspace.id)
        )
        assert settings_row is not None
        settings_row.slot_interval_minutes = 15
        db.flush()
        _create_occupancy(
            db,
            workspace=workspace,
            patient_id=patient.id,
            doctor_id=doctor.id,
            branch_id=workspace.primary_branch_id,
            service=bikini,
            start_local=datetime(2026, 10, 15, 16, 15, tzinfo=CAIRO),
            end_local=datetime(2026, 10, 15, 16, 30, tzinfo=CAIRO),
        )
    db.commit()
    return Context(
        scenario=scenario,
        db=db,
        workspace=workspace,
        patient=patient,
        doctor=doctor,
        underarm=underarm,
        bikini=bikini,
        arms=arms,
    )


def _create_occupancy(
    db: Session,
    *,
    workspace: Any,
    patient_id: UUID,
    doctor_id: UUID,
    branch_id: UUID,
    service: Any,
    start_local: datetime,
    end_local: datetime,
) -> None:
    from app.models.patient import Patient

    blocker = Patient(
        workspace_id=workspace.id,
        first_name="RA",
        last_name="Blocker",
        preferred_language="ar",
        source="other",
        status="active",
    )
    db.add(blocker)
    db.flush()
    start_at = start_local.astimezone(UTC)
    end_at = end_local.astimezone(UTC)
    db.add(
        Appointment(
            workspace_id=workspace.id,
            patient_id=blocker.id,
            branch_id=branch_id,
            doctor_id=doctor_id,
            doctor_assignment_known=True,
            service_id=service.id,
            status="pending",
            source="ai",
            start_at=start_at,
            end_at=end_at,
            busy_start_at=start_at,
            busy_end_at=end_at,
            duration_minutes=15,
            price_minor=service.price_minor,
            discount_minor=0,
            currency="EGP",
            laser_device_key="candela_gentle",
            laser_device_name="Candela Gentle",
        )
    )
    db.flush()


def _active_task(ctx: Context) -> dict[str, Any] | None:
    if ctx.conversation_id is None:
        return None
    persisted = load_active_task(
        ctx.db,
        workspace_id=ctx.workspace.id,
        conversation_id=ctx.conversation_id,
        patient_id=ctx.patient.id,
    )
    if persisted is None:
        return None
    return persisted.active_task.model_dump(mode="json")


def _appointments(ctx: Context) -> list[dict[str, Any]]:
    rows = list(
        ctx.db.scalars(
            select(Appointment)
            .where(
                Appointment.workspace_id == ctx.workspace.id,
                Appointment.patient_id == ctx.patient.id,
            )
            .order_by(Appointment.start_at, Appointment.created_at)
        )
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        additional = list(
            ctx.db.scalars(
                select(AppointmentAdditionalService).where(
                    AppointmentAdditionalService.workspace_id == ctx.workspace.id,
                    AppointmentAdditionalService.appointment_id == row.id,
                )
            )
        )
        result.append(
            {
                "id": str(row.id),
                "status": row.status,
                "start_at": row.start_at.isoformat(),
                "service_id": str(row.service_id),
                "doctor_id": str(row.doctor_id),
                "laser_device_key": row.laser_device_key,
                "additional_services": [
                    {
                        "id": str(line.id),
                        "service_id": str(line.service_id),
                        "laser_device_key": line.laser_device_key,
                    }
                    for line in additional
                ],
            }
        )
    return result


def _chat(ctx: Context, customer: str) -> dict[str, Any]:
    RAW.clear()
    active_before = _active_task(ctx)
    appointments_before = _appointments(ctx)
    _reset_runtime_trace(
        active_task_before=active_before,
        appointments_before=appointments_before,
    )
    response = run_agent_chat(
        db=ctx.db,
        workspace=ctx.workspace,
        payload=AgentChatRequest(
            patient_id=ctx.patient.id,
            conversation_id=ctx.conversation_id,
            channel="whatsapp",
            message=customer,
        ),
    )
    ctx.conversation_id = response.conversation_id
    outbound = ctx.db.get(Message, response.outbound_message_id) if response.outbound_message_id else None
    metadata = dict(outbound.metadata_json or {}) if outbound is not None else {}
    appointments_after = _appointments(ctx)
    runtime_trace = {
        key: _dump(value)
        for key, value in RUNTIME_TRACE.items()
        if not key.startswith("_")
    }
    if len(runtime_trace.get("proposals_created") or []) > 1:
        SAFETY_COUNTERS["duplicate_proposals"] += 1
    record = {
        "customer": customer,
        "agent": response.reply or "",
        "model": response.model,
        "raw_structured": list(RAW),
        "active_task_before": active_before,
        "active_task": _active_task(ctx),
        "active_task_after_device_resolution": runtime_trace.get("active_task_after_device_resolution"),
        "active_task_after_second_progression": runtime_trace.get("active_task_after_second_progression"),
        "device_resolution": runtime_trace.get("device_resolution") or [],
        "second_progression_plan": runtime_trace.get("second_progression_plan"),
        "availability_requests": runtime_trace.get("availability_requests") or [],
        "read_batches": runtime_trace.get("read_batches") or [],
        "proposals_created": runtime_trace.get("proposals_created") or [],
        "read_context": metadata.get("v2_read_context"),
        "availability_reference_context": metadata.get("v2_availability_reference_context"),
        "booking_proposal": metadata.get("v2_booking_proposal"),
        "appointments_before": appointments_before,
        "appointments": appointments_after,
        "db_effect": {
            "appointments_before": appointments_before,
            "appointments_after": appointments_after,
            "appointment_delta": len(appointments_after) - len(appointments_before),
        },
    }
    ctx.turns.append(record)
    print(
        "RA_DAYPART_TURN="
        + json.dumps(
            {"scenario": ctx.scenario, **record},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ),
        flush=True,
    )
    return record


def _task_time(record: dict[str, Any]) -> str | None:
    task = record.get("active_task") or {}
    constraints = task.get("constraints") or {}
    time_value = constraints.get("time") or {}
    if time_value.get("start_time"):
        return str(time_value["start_time"])
    read_context = record.get("read_context") or {}
    read_time = read_context.get("time") or {}
    if read_time.get("start_time"):
        return str(read_time["start_time"])
    return _appointment_clock(record)


def _proposal(record: dict[str, Any]) -> dict[str, Any] | None:
    task = record.get("active_task") or {}
    proposal = task.get("proposal")
    return proposal if isinstance(proposal, dict) else None


def _reference_options(record: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("availability_reference_context", "read_context"):
        source = record.get(key)
        if not isinstance(source, dict):
            continue
        options = source.get("availability_reference_options")
        if isinstance(options, list):
            return [item for item in options if isinstance(item, dict)]
    return []


def _option_clock(option: dict[str, Any]) -> str:
    payload = option.get("payload") if isinstance(option.get("payload"), dict) else option
    for key in ("start_local", "start_at", "start"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(CAIRO).strftime("%H:%M")
        except ValueError:
            text = str(value)
            return text[11:16] if "T" in text else text[:5]
    return ""


def _appointment_clock(record: dict[str, Any]) -> str | None:
    appointments = record.get("appointments") or []
    if len(appointments) != 1:
        return None
    start = datetime.fromisoformat(str(appointments[0]["start_at"]).replace("Z", "+00:00"))
    return start.astimezone(CAIRO).strftime("%H:%M")


def _raw_period(record: dict[str, Any]) -> str | None:
    for payload in record.get("raw_structured") or []:
        if not isinstance(payload, dict):
            continue
        operations = payload.get("operations")
        if isinstance(operations, list) and operations and isinstance(operations[0], dict):
            return str(operations[0].get("time_period_provenance") or "none")
    return None


def _raw_semantic_times(record: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for payload in record.get("raw_structured") or []:
        if not isinstance(payload, dict):
            continue
        operations = payload.get("operations")
        if not isinstance(operations, list):
            continue
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            entities = operation.get("entities")
            time_value = entities.get("time") if isinstance(entities, dict) else None
            if not isinstance(time_value, dict) or time_value.get("start_time") in (None, ""):
                continue
            values.append(
                {
                    "operation_type": operation.get("type"),
                    "time": time_value,
                    "time_period_provenance": operation.get("time_period_provenance"),
                    "selection_time_period_provenance": operation.get(
                        "selection_time_period_provenance"
                    ),
                }
            )
    return values


def _quality_assessment(
    record: dict[str, Any],
    *,
    functional: bool = True,
    unnecessary_clarification: bool = False,
    context_loss: bool = False,
    contradiction: bool = False,
) -> dict[str, str]:
    reply = str(record.get("agent") or "").strip()
    fallback = any(
        token in reply.lower()
        for token in ("fallback", "system error", "internal error", "\u062d\u0635\u0644 \u062e\u0637\u0623", "\u062a\u0639\u0630\u0631 \u062a\u0646\u0641\u064a\u0630")
    )
    leak = any(
        token in reply
        for token in ("||TIA_", "time_period_provenance", "device_key", "service_id")
    )
    fragments = [
        item.strip()
        for item in reply.replace("\u061f", "\n").replace(".", "\n").splitlines()
        if len(item.strip()) >= 8
    ]
    repeated = len(fragments) != len(set(fragments))
    natural_flag = not reply or fallback
    return {
        "Functional": "PASS" if functional else "FAIL",
        "Naturalness": "FLAG" if natural_flag else "CLEAN",
        "Repetition": "FLAG" if repeated else "CLEAN",
        "Unnecessary clarification": "FLAG" if unnecessary_clarification else "CLEAN",
        "Fallback-looking": "FLAG" if fallback else "CLEAN",
        "Context loss": "FLAG" if context_loss else "CLEAN",
        "Contradiction": "FLAG" if contradiction else "CLEAN",
        "Internal metadata leak": "FLAG" if leak else "CLEAN",
    }


def _annotate(
    record: dict[str, Any],
    *,
    functional: bool = True,
    unnecessary_clarification: bool = False,
    context_loss: bool = False,
    contradiction: bool = False,
) -> None:
    record["evidence"] = {
        "CUSTOMER": record.get("customer"),
        "AGENT": record.get("agent"),
        "Raw structured semantic time": _raw_semantic_times(record),
        "time_period_provenance": [
            item.get("time_period_provenance") for item in _raw_semantic_times(record)
        ],
        "Canonical normalized time": _task_time(record),
        "Active task BEFORE": record.get("active_task_before"),
        "Active task AFTER device resolution": record.get(
            "active_task_after_device_resolution"
        ),
        "Active task AFTER second progression": record.get(
            "active_task_after_second_progression"
        ),
        "Device resolution": record.get("device_resolution"),
        "Availability request": record.get("availability_requests"),
        "Proposal": _proposal(record) or record.get("booking_proposal"),
        "DB effect": record.get("db_effect"),
    }
    record["assessment"] = _quality_assessment(
        record,
        functional=functional,
        unnecessary_clarification=unnecessary_clarification,
        context_loss=context_loss,
        contradiction=contradiction,
    )


def _require(condition: bool, *, counter: str | None = None, message: str) -> None:
    if condition:
        return
    if counter:
        SAFETY_COUNTERS[counter] += 1
    raise AssertionError(message)


def _task_service_ids(record: dict[str, Any]) -> list[str]:
    task = record.get("active_task")
    if isinstance(task, dict):
        raw = task.get("requested_service_ids")
        if isinstance(raw, list):
            return [str(item) for item in raw]
    proposal = _proposal(record)
    if isinstance(proposal, dict):
        raw = proposal.get("service_ids")
        if isinstance(raw, list):
            return [str(item) for item in raw]
    return []


def _appointment_service_ids(record: dict[str, Any]) -> set[str]:
    appointments = record.get("appointments") or []
    if len(appointments) != 1 or not isinstance(appointments[0], dict):
        return set()
    appointment = appointments[0]
    result = {str(appointment.get("service_id") or "")}
    for line in appointment.get("additional_services") or []:
        if isinstance(line, dict) and line.get("service_id") not in (None, ""):
            result.add(str(line["service_id"]))
    result.discard("")
    return result


def _proposal_clock(proposal: dict[str, Any] | None) -> str | None:
    if not isinstance(proposal, dict) or proposal.get("start_at") in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(proposal["start_at"]).replace("Z", "+00:00")).astimezone(
            CAIRO
        ).strftime("%H:%M")
    except ValueError:
        return None


def _ra1(ctx: Context) -> None:
    record = _chat(
        ctx,
        "\u0639\u0627\u064a\u0632\u0629 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 4:15 \u0627\u0644\u0639\u0635\u0631 \u0628\u0627\u0644\u0636\u0628\u0637",
    )
    _require(
        _task_time(record) == "16:15",
        counter="silent_am_pm_mutation",
        message="RA1 canonical time != 16:15",
    )
    _require(
        _raw_period(record) == "explicit_pm",
        counter="silent_am_pm_mutation",
        message="RA1 missing explicit_pm provenance",
    )
    _require(
        not record["appointments"],
        counter="wrong_writes",
        message="RA1 unavailable exact time wrote appointment",
    )
    reply = str(record["agent"])
    _require(
        any(token in reply for token in ("\u0645\u0634 \u0645\u062a\u0627\u062d", "\u063a\u064a\u0631 \u0645\u062a\u0627\u062d", "\u0645\u0634 \u0641\u0627\u0636\u064a")),
        counter="false_success",
        message="RA1 did not report requested time unavailable",
    )
    _require(
        "\u062a\u062d\u0628 \u062a\u062d\u062c\u0632 \u064a\u0648\u0645 \u0625\u064a\u0647" not in reply and "\u064a\u0648\u0645 \u0625\u064a\u0647" not in reply,
        counter="false_success",
        message="RA1 asked for a date already supplied by the customer",
    )
    clocks = {_option_clock(item) for item in _reference_options(record)}
    _require(
        "16:00" in clocks and "16:30" in clocks,
        counter="false_success",
        message=f"RA1 verified same-day alternatives missing: {sorted(clocks)}",
    )
    _require(
        bool(record.get("availability_requests")),
        counter="false_success",
        message="RA1 never reached canonical availability",
    )
    _annotate(record)


def _ra2(ctx: Context) -> None:
    record = _chat(
        ctx,
        "\u0639\u0627\u064a\u0632\u0629 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 4:15 \u0627\u0644\u0635\u0628\u062d \u0628\u0627\u0644\u0636\u0628\u0637",
    )
    _require(
        _task_time(record) == "04:15",
        counter="silent_am_pm_mutation",
        message="RA2 explicit morning was mutated",
    )
    _require(
        _raw_period(record) == "explicit_am",
        counter="silent_am_pm_mutation",
        message="RA2 missing explicit_am provenance",
    )
    _require(
        not record["appointments"],
        counter="wrong_writes",
        message="RA2 wrote an out-of-hours unintended appointment",
    )
    _annotate(record)


def _ra3(ctx: Context) -> None:
    record = _chat(
        ctx,
        "\u0639\u0627\u064a\u0632\u0629 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 16:15 \u0628\u0627\u0644\u0636\u0628\u0637",
    )
    _require(
        _task_time(record) == "16:15",
        counter="silent_am_pm_mutation",
        message="RA3 literal 16:15 was mutated",
    )
    _require(
        _raw_period(record) == "none",
        counter="silent_am_pm_mutation",
        message="RA3 literal 24-hour clock received invented AM/PM provenance",
    )
    _require(
        not record["appointments"],
        counter="wrong_writes",
        message="RA3 blocked literal 16:15 wrote appointment",
    )
    _annotate(record)


def _ra4(ctx: Context) -> None:
    record = _chat(
        ctx,
        "\u0639\u0627\u064a\u0632\u0629 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 3",
    )
    _require(
        _task_time(record) == "15:00",
        counter="silent_am_pm_mutation",
        message="RA4 bare 3 did not resolve to normal clinic 15:00",
    )
    _require(
        _raw_period(record) == "none",
        counter="silent_am_pm_mutation",
        message="RA4 inferred clinic period was mislabeled explicit",
    )
    _require(
        len(record["appointments"]) == 1,
        counter="duplicate_appointments",
        message="RA4 did not produce exactly one appointment",
    )
    _require(
        _appointment_clock(record) == "15:00",
        counter="wrong_time_writes",
        message="RA4 wrote outside canonical 15:00",
    )
    _require(
        str(record["appointments"][0].get("status") or "") == "pending",
        counter="false_success",
        message="RA4 booking creation changed attendance/confirmation truth",
    )
    _annotate(record)


def _ra5(ctx: Context) -> None:
    expected_services = {str(ctx.underarm.id), str(ctx.bikini.id)}
    first = _chat(
        ctx,
        "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0648\u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0645\u0639 \u0628\u0639\u0636 \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 11\u060c "
        "\u0645\u0639 \u062f\u0643\u062a\u0648\u0631 \u0648\u0627\u062d\u062f \u0648\u0648\u0631\u064a\u0646\u064a \u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0642\u0628\u0644 \u0627\u0644\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0626\u064a",
    )
    _require(
        _task_time(first) == "11:00",
        counter="silent_am_pm_mutation",
        message="RA5 initial bare 11 did not resolve to 11:00 in clinic context",
    )
    _require(
        set(_task_service_ids(first)) == expected_services,
        counter="lost_services",
        message="RA5 initial grouped service set was lost",
    )
    initial_proposal = _proposal(first)
    if initial_proposal is not None:
        _require(
            _proposal_clock(initial_proposal) == "11:00",
            counter="wrong_time_writes",
            message="RA5 initial proposal did not match 11:00 task",
        )
    _annotate(first)

    corrected = _chat(ctx, "\u062e\u0644\u064a\u0647 12 \u0628\u062f\u0644 11")
    _require(
        _task_time(corrected) == "12:00",
        counter="wrong_time_writes",
        message="RA5 corrected canonical task time != 12:00",
    )
    _require(
        set(_task_service_ids(corrected)) == expected_services,
        counter="lost_services",
        message="RA5 time correction lost grouped services",
    )
    proposal = _proposal(corrected)
    progressed = corrected
    if proposal is None:
        progressed = _chat(
            ctx,
            "\u062a\u0645\u0627\u0645\u060c \u0643\u0645\u0644\u064a \u0646\u0641\u0633 \u0627\u0644\u062d\u062c\u0632 \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 \u0627\u0644\u0633\u0627\u0639\u0629 12 \u0648\u0648\u0631\u064a\u0646\u064a \u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0642\u0628\u0644 \u0627\u0644\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0626\u064a",
        )
        _require(
            _task_time(progressed) == "12:00",
            counter="wrong_time_writes",
            message="RA5 progression moved away from corrected 12:00",
        )
        proposal = _proposal(progressed)
    _require(
        isinstance(proposal, dict),
        counter="false_success",
        message="RA5 corrected grouped booking did not produce a proposal",
    )
    _require(
        _proposal_clock(proposal) == "12:00",
        counter="wrong_time_writes",
        message="RA5 proposal did not use corrected 12:00",
    )
    _require(
        "T11:00" not in str(proposal.get("start_at") or ""),
        counter="wrong_time_writes",
        message="RA5 stale 11:00 proposal survived correction",
    )
    _annotate(corrected)
    if progressed is not corrected:
        _annotate(progressed)

    confirmed = _chat(ctx, "\u062a\u0645\u0627\u0645\u060c \u0623\u0643\u062f \u0627\u0644\u062d\u062c\u0632")
    _require(
        len(confirmed["appointments"]) == 1,
        counter="duplicate_appointments",
        message="RA5 confirmation did not produce exactly one appointment",
    )
    _require(
        _appointment_clock(confirmed) == "12:00",
        counter="wrong_time_writes",
        message="RA5 DB appointment did not use corrected 12:00",
    )
    _require(
        "11:00" not in {_appointment_clock(confirmed) or ""},
        counter="wrong_time_writes",
        message="RA5 DB retained stale 11:00 write",
    )
    _require(
        _appointment_service_ids(confirmed) == expected_services,
        counter="lost_services",
        message="RA5 final appointment lost a grouped service",
    )
    _annotate(confirmed)


def _ra6(ctx: Context) -> None:
    expected_services = {str(ctx.underarm.id), str(ctx.bikini.id)}
    first = _chat(
        ctx,
        "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0648\u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0645\u0639 \u0628\u0639\u0636 \u0641\u064a \u0646\u0641\u0633 \u0627\u0644\u0632\u064a\u0627\u0631\u0629 \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 "
        "\u0627\u0644\u0633\u0627\u0639\u0629 4:30 \u0627\u0644\u0639\u0635\u0631 \u0628\u0627\u0644\u0636\u0628\u0637\u060c \u0645\u0639 \u062f\u0643\u062a\u0648\u0631 \u0648\u0627\u062d\u062f \u0648\u0648\u0631\u064a\u0646\u064a \u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0642\u0628\u0644 \u0627\u0644\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0626\u064a",
    )
    _require(
        _task_time(first) == "16:30",
        counter="silent_am_pm_mutation",
        message="RA6 grouped explicit PM time != 16:30",
    )
    raw_periods = {
        str(item.get("time_period_provenance") or "none")
        for item in _raw_semantic_times(first)
    }
    _require(
        "explicit_pm" in raw_periods,
        counter="silent_am_pm_mutation",
        message="RA6 grouped turn lost explicit_pm provenance",
    )
    _require(
        set(_task_service_ids(first)) == expected_services,
        counter="lost_services",
        message="RA6 grouped task lost requested services",
    )
    first_task = first.get("active_task") or {}
    constraints = first_task.get("constraints") if isinstance(first_task, dict) else {}
    if isinstance(constraints, dict) and constraints.get("device_key") not in (None, ""):
        _require(
            constraints.get("device_key") == "candela_gentle",
            counter="wrong_device",
            message="RA6 selected wrong visit-level device",
        )
    _annotate(first)

    proposal = _proposal(first)
    progressed = first
    if proposal is None:
        progressed = _chat(
            ctx,
            "\u062a\u0645\u0627\u0645\u060c \u0627\u0644\u0633\u0639\u0631 \u0645\u0646\u0627\u0633\u0628. \u0643\u0645\u0644\u064a \u0646\u0641\u0633 \u0627\u0644\u062d\u062c\u0632 \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 \u0627\u0644\u0633\u0627\u0639\u0629 4:30 \u0627\u0644\u0639\u0635\u0631 "
            "\u0648\u0648\u0631\u064a\u0646\u064a \u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0642\u0628\u0644 \u0627\u0644\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0626\u064a",
        )
        _require(
            _task_time(progressed) == "16:30",
            counter="wrong_time_writes",
            message="RA6 progression moved away from 16:30",
        )
        _require(
            set(_task_service_ids(progressed)) == expected_services,
            counter="lost_services",
            message="RA6 progression lost grouped services",
        )
        proposal = _proposal(progressed)
        _annotate(progressed)
    _require(
        isinstance(proposal, dict),
        counter="false_success",
        message="RA6 grouped booking did not produce confirmation proposal",
    )
    _require(
        set(str(item) for item in proposal.get("service_ids") or []) == expected_services,
        counter="lost_services",
        message="RA6 proposal lost services",
    )
    _require(
        _proposal_clock(proposal) == "16:30",
        counter="wrong_time_writes",
        message="RA6 proposal time != 16:30",
    )
    _require(
        str(proposal.get("doctor_id") or "") == str(ctx.doctor.id),
        counter="wrong_writes",
        message="RA6 proposal used wrong doctor",
    )
    _require(
        proposal.get("device_key") == "candela_gentle",
        counter="wrong_device",
        message="RA6 proposal used wrong visit-level device",
    )

    confirmed = _chat(ctx, "\u062a\u0645\u0627\u0645\u060c \u0623\u0643\u062f \u0627\u0644\u062d\u062c\u0632")
    _require(
        len(confirmed["appointments"]) == 1,
        counter="duplicate_appointments",
        message="RA6 did not preserve ONE appointment semantics",
    )
    appointment = confirmed["appointments"][0]
    _require(
        _appointment_clock(confirmed) == "16:30",
        counter="wrong_time_writes",
        message="RA6 DB time != canonical 16:30",
    )
    _require(
        str(appointment.get("status") or "") == "pending",
        counter="false_success",
        message="RA6 booking creation incorrectly implied attendance confirmation",
    )
    _require(
        _appointment_service_ids(confirmed) == expected_services,
        counter="lost_services",
        message="RA6 DB visit lost requested services",
    )
    device_keys = {
        str(value)
        for value in [
            appointment.get("laser_device_key"),
            *[
                line.get("laser_device_key")
                for line in appointment.get("additional_services") or []
                if isinstance(line, dict)
            ],
        ]
        if value not in (None, "")
    }
    _require(
        device_keys == {"candela_gentle"},
        counter="cross_device_visits",
        message=f"RA6 cross-device visit detected: {sorted(device_keys)}",
    )
    _require(
        str(appointment.get("doctor_id") or "") == str(ctx.doctor.id),
        counter="wrong_writes",
        message="RA6 DB appointment used wrong doctor",
    )
    _annotate(confirmed)


SCENARIOS: list[tuple[str, bool, Callable[[Context], None]]] = [
    ("RA1", True, _ra1),
    ("RA2", False, _ra2),
    ("RA3", True, _ra3),
    ("RA4", False, _ra4),
    ("RA5", False, _ra5),
    ("RA6", False, _ra6),
]


def _persisted_appointments(workspace_id: UUID) -> int:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            return int(
                db.scalar(
                    select(func.count()).select_from(Appointment).where(
                        Appointment.workspace_id == workspace_id
                    )
                )
                or 0
            )
    finally:
        engine.dispose()


def main() -> int:
    results: list[dict[str, Any]] = []
    failures = 0
    for name, hole, validator in SCENARIOS:
        workspace_id: UUID | None = None
        turns: list[dict[str, Any]] = []
        error: str | None = None
        try:
            with rollback_session() as db:
                ctx = _setup(db, name, hole=hole)
                workspace_id = ctx.workspace.id
                validator(ctx)
                turns = list(ctx.turns)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            error = f"{type(exc).__name__}: {exc}"
        persisted = _persisted_appointments(workspace_id) if workspace_id is not None else 0
        if persisted:
            SAFETY_COUNTERS["persisted_audit_writes"] += persisted
            failures += 1
            error = (error + "; " if error else "") + f"persisted_audit_writes={persisted}"
        results.append(
            {
                "scenario": name,
                "status": "PASS" if error is None else "FAIL",
                "error": error,
                "turns": turns,
                "persisted_appointments_after_rollback": persisted,
            }
        )
        print(
            "RA_DAYPART_RESULT="
            + json.dumps(results[-1], ensure_ascii=False, separators=(",", ":"), default=str),
            flush=True,
        )

    payload = {
        "status": "PASS" if failures == 0 else "FAIL",
        "scenario_count": len(results),
        "results": results,
        "safety_counters": SAFETY_COUNTERS,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("RA_DAYPART_SUMMARY=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
    return 0 if failures == 0 and all(value == 0 for value in SAFETY_COUNTERS.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
