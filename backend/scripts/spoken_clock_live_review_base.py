from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
from scripts.run_grouped_multiservice_intent_live_review import build_fixture

CAIRO = ZoneInfo("Africa/Cairo")


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


def _dump_provider_value(value: Any) -> Any:
    return _dump(value)


def _capture_invoke(*args, **kwargs):
    value = _ORIGINAL_INVOKE(*args, **kwargs)
    RAW.append(_dump_provider_value(value))
    return value


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


def install_runtime_instrumentation() -> None:
    """Install live-review tracing only for the explicit reviewer execution window."""
    turn_interpreter.invoke_typed_structured_output = _capture_invoke
    orchestrator_runtime._auto_resolve_single_booking_device = _capture_auto_device
    orchestrator_runtime.plan_active_task_progress = _capture_plan_progress
    orchestrator_runtime.apply_step_state = _capture_apply_state
    orchestrator_runtime.execute_step_reads = _capture_execute_reads
    orchestrator_runtime._prepare_booking_proposal = _capture_prepare_proposal


def uninstall_runtime_instrumentation() -> None:
    """Restore production functions so importing reviewer helpers cannot contaminate tests."""
    turn_interpreter.invoke_typed_structured_output = _ORIGINAL_INVOKE
    orchestrator_runtime._auto_resolve_single_booking_device = _ORIGINAL_AUTO_DEVICE
    orchestrator_runtime.plan_active_task_progress = _ORIGINAL_PLAN_PROGRESS
    orchestrator_runtime.apply_step_state = _ORIGINAL_APPLY_STATE
    orchestrator_runtime.execute_step_reads = _ORIGINAL_EXECUTE_READS
    orchestrator_runtime._prepare_booking_proposal = _ORIGINAL_PREPARE_PROPOSAL


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
        "RA_CLOCK_TURN="
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


def _nearest_alternative_options(record: dict[str, Any]) -> list[dict[str, Any]]:
    task = record.get("active_task") or {}
    constraints = task.get("constraints") if isinstance(task, dict) else {}
    date_value = constraints.get("date") if isinstance(constraints, dict) else {}
    requested_date = (
        str(date_value.get("start_date"))
        if isinstance(date_value, dict) and date_value.get("start_date")
        else None
    )
    options: list[dict[str, Any]] = []
    for batch in record.get("read_batches") or []:
        if not isinstance(batch, dict):
            continue
        results = batch.get("results")
        if not isinstance(results, list):
            continue
        for result in results:
            if (
                not isinstance(result, dict)
                or result.get("kind") != "availability"
                or result.get("ok") is not True
            ):
                continue
            payload = result.get("payload")
            nearest = (
                payload.get("nearest_alternative_slots")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(nearest, list):
                continue
            for item in nearest:
                if not isinstance(item, dict):
                    continue
                start_local = str(item.get("start_local") or "")
                if requested_date and not start_local.startswith(f"{requested_date}T"):
                    continue
                options.append(item)
    return options


def _reply_mentions_clock(reply: str, clock: str) -> bool:
    try:
        hour_text, minute = clock.split(":", 1)
        hour = int(hour_text)
    except (ValueError, AttributeError):
        return False
    hour_12 = hour % 12 or 12
    variants = {clock, f"{hour_12}:{minute}"}
    if minute == "00":
        variants.update(
            {
                f"{hour_12} مساء",
                f"{hour_12} العصر",
                f"{hour_12}:00 مساء",
                f"{hour_12}:00 العصر",
            }
        )
    return any(value in reply for value in variants)


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
