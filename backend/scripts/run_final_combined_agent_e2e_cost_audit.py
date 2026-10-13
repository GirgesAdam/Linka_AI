from __future__ import annotations

import inspect
import json
import math
import os
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from langchain_core.callbacks import BaseCallbackHandler
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.agents.model_provider as model_provider
from app.core.config import settings
from app.core.whatsapp_booking_actions import attendance_confirm_action_id
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.branch import Branch
from app.models.conversation import Conversation
from app.models.doctor_service import DoctorService
from app.models.message import Message
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.service import Service
from app.schemas.agent import AgentChatRequest
from app.services.agent_v2.live_chat import run_agent_chat
from app.services.agent_v2.state_persistence import load_active_task
from app.services.whatsapp_interactions import (
    process_whatsapp_booking_action,
    whatsapp_booking_dispatch_metadata,
)
from scripts.run_grouped_multiservice_intent_live_review import (
    _device_price,
    rollback_session,
)
from scripts.run_grouped_multiservice_intent_live_review import (
    build_fixture as build_grouped_fixture,
)

CAIRO = ZoneInfo("Africa/Cairo")
PRICING_SNAPSHOT_DATE = "2026-10-13"
ARTIFACT_PATH = Path("artifacts/final-combined-agent-e2e-cost-audit.json")
LUNA_SHORT_CONTEXT_LIMIT = 272_000

# Audit-only pricing snapshot. These values are deliberately not production constants.
PRICING = {
    "gpt-6-luna": {
        "standard_short": {"input": 0.10, "cached": 0.01, "cache_write": 0.125, "output": 0.50},
        "standard_long": {"input": 0.20, "cached": 0.02, "cache_write": 0.25, "output": 0.75},
    },
    "gpt-5-mini": {
        "standard_short": {"input": 0.25, "cached": 0.025, "cache_write": None, "output": 2.00},
    },
}

SAFETY_COUNTERS = {
    "wrong_writes": 0,
    "partial_writes": 0,
    "false_success": 0,
    "duplicate_appointments": 0,
    "wrong_time_writes": 0,
    "wrong_doctor_writes": 0,
    "wrong_device_writes": 0,
    "cross_device_visits": 0,
    "lost_requested_services": 0,
    "wrong_price_claims": 0,
    "double_counted_prices": 0,
    "hidden_availability_gaps": 0,
    "fabricated_availability": 0,
    "internal_marker_leaks": 0,
    "accidental_pending_to_confirmed": 0,
    "persisted_audit_writes": 0,
}


class AuditFunctionalDefect(AssertionError):
    def __init__(self, payload: dict[str, object]) -> None:
        super().__init__(str(payload.get("message") or payload.get("id") or "audit defect"))
        self.payload = payload


@dataclass
class UsageCall:
    scenario_id: str
    turn_number: int
    customer_intent: str
    call_stage: str
    stage_group: str
    requested_model: str | None
    actual_model: str | None
    service_tier: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    latency_ms: int
    success: bool
    usage_status: str
    attempt_number: int = 1
    retry: bool = False
    fallback: bool = False
    cost_usd: float | None = None
    pricing_status: str = "UNPRICED"


class UsageCollector(BaseCallbackHandler):
    def __init__(self) -> None:
        self.calls: list[UsageCall] = []
        self._current: tuple[str, int, str] | None = None
        self._starts: dict[UUID, tuple[float, str, str, str | None]] = {}

    def begin_turn(self, scenario_id: str, turn_number: int, customer_intent: str) -> None:
        self._current = (scenario_id, turn_number, customer_intent)

    def end_turn(self) -> None:
        self._current = None

    @staticmethod
    def _stack_stage() -> tuple[str, str]:
        filenames = [frame.filename.replace("\\", "/").lower() for frame in inspect.stack()[2:36]]
        joined = "\n".join(filenames)
        stages = (
            ("availability_reference_interpreter.py", "interpreter:availability-reference", "interpreter"),
            ("turn_interpreter.py", "interpreter:turn", "interpreter"),
            ("price_device_composer.py", "composer:price-device", "composer"),
            ("availability_composer.py", "composer:availability", "composer"),
            ("terminal_composer.py", "composer:terminal", "composer"),
            ("responder.py", "composer:responder", "composer"),
            ("followup_composer.py", "composer:followup", "composer"),
            ("grounded_response.py", "composer:grounded", "composer"),
        )
        for needle, stage, group in stages:
            if needle in joined:
                return stage, group
        return "other", "other"

    @staticmethod
    def _requested_model(serialized: dict[str, Any], metadata: dict[str, Any] | None) -> str | None:
        metadata = metadata or {}
        for key in ("ls_model_name", "model_name", "model"):
            value = metadata.get(key)
            if value:
                return str(value)
        kwargs = serialized.get("kwargs") if isinstance(serialized, dict) else None
        if isinstance(kwargs, dict):
            value = kwargs.get("model") or kwargs.get("model_name")
            if value:
                return str(value)
        return None

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        del messages, parent_run_id, tags, kwargs
        stage, group = self._stack_stage()
        self._starts[run_id] = (
            perf_counter(),
            stage,
            group,
            self._requested_model(serialized, metadata),
        )

    @staticmethod
    def _detail_sum(details: dict[str, Any] | None, suffix: str) -> int:
        if not isinstance(details, dict):
            return 0
        return sum(
            int(value or 0)
            for key, value in details.items()
            if str(key).endswith(suffix) and isinstance(value, int | float)
        )

    def _context(self) -> tuple[str, int, str]:
        return self._current or ("UNKNOWN", 0, "")

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        del parent_run_id, tags, kwargs
        started, stage, group, requested = self._starts.pop(
            run_id, (perf_counter(), "other", "other", None)
        )
        scenario_id, turn_number, intent = self._context()
        message = None
        try:
            message = response.generations[0][0].message
        except (AttributeError, IndexError, TypeError):
            pass
        usage = getattr(message, "usage_metadata", None) if message is not None else None
        metadata = getattr(message, "response_metadata", None) if message is not None else None
        metadata = metadata if isinstance(metadata, dict) else {}
        actual_model = metadata.get("model_name") or metadata.get("model") or requested
        service_tier = metadata.get("service_tier")
        if isinstance(usage, dict):
            input_details = usage.get("input_token_details") or {}
            output_details = usage.get("output_token_details") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
            cached = self._detail_sum(input_details, "cache_read")
            cache_write = self._detail_sum(input_details, "cache_creation")
            reasoning = self._detail_sum(output_details, "reasoning")
            usage_status = "MEASURED"
        else:
            input_tokens = output_tokens = total_tokens = None
            cached = cache_write = reasoning = None
            usage_status = "UNMEASURED"
        self.calls.append(
            UsageCall(
                scenario_id=scenario_id,
                turn_number=turn_number,
                customer_intent=intent,
                call_stage=stage,
                stage_group=group,
                requested_model=requested,
                actual_model=str(actual_model) if actual_model else None,
                service_tier=str(service_tier) if service_tier else None,
                input_tokens=input_tokens,
                cached_input_tokens=cached,
                cache_write_tokens=cache_write,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning,
                total_tokens=total_tokens,
                latency_ms=int((perf_counter() - started) * 1000),
                success=True,
                usage_status=usage_status,
            )
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        del error, parent_run_id, tags, kwargs
        started, stage, group, requested = self._starts.pop(
            run_id, (perf_counter(), "other", "other", None)
        )
        scenario_id, turn_number, intent = self._context()
        self.calls.append(
            UsageCall(
                scenario_id=scenario_id,
                turn_number=turn_number,
                customer_intent=intent,
                call_stage=stage,
                stage_group=group,
                requested_model=requested,
                actual_model=requested,
                service_tier=None,
                input_tokens=None,
                cached_input_tokens=None,
                cache_write_tokens=None,
                output_tokens=None,
                reasoning_tokens=None,
                total_tokens=None,
                latency_ms=int((perf_counter() - started) * 1000),
                success=False,
                usage_status="UNMEASURED",
            )
        )

    def finalize_attempts(self) -> None:
        counts: dict[tuple[str, int, str, str], int] = {}
        fallback_name = (settings.openai_fallback_model or "").strip()
        for call in self.calls:
            requested = (call.requested_model or call.actual_model or "unknown").strip()
            key = (call.scenario_id, call.turn_number, call.call_stage, requested)
            counts[key] = counts.get(key, 0) + 1
            call.attempt_number = counts[key]
            call.retry = call.attempt_number > 1
            actual = (call.actual_model or requested).lower()
            call.fallback = bool(fallback_name and actual.startswith(fallback_name.lower()))


COLLECTOR = UsageCollector()
_ORIGINAL_MODEL_FACTORY = model_provider._cached_openai_model


def install_usage_collector() -> None:
    def instrumented_model_factory(**kwargs: Any):
        model = _ORIGINAL_MODEL_FACTORY(**kwargs)
        model.callbacks = [COLLECTOR]
        return model

    model_provider._cached_openai_model = instrumented_model_factory


@dataclass
class ScenarioContext:
    scenario_id: str
    db: Session
    workspace: Any
    patient: Patient
    doctor: Any
    branch: Branch
    services: dict[str, Service]
    conversation_id: UUID | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)
    completed_booking: bool = False


SCENARIO_RESULTS: list[dict[str, Any]] = []
DEFECTS: list[dict[str, Any]] = []
ALL_TURNS: list[dict[str, Any]] = []
COMPLETED_BOOKING_SCENARIOS: set[str] = set()


def _git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def _add_service(
    db: Session,
    *,
    workspace_id: UUID,
    doctor_id: UUID,
    name: str,
    slug: str,
    price_minor: int,
    duration_minutes: int,
    requires_laser_device: bool,
) -> Service:
    service = Service(
        workspace_id=workspace_id,
        name=name,
        slug=f"{slug}-{uuid4().hex[:8]}",
        operational_category="laser" if requires_laser_device else "dermatology",
        description=f"Synthetic audit service: {name}",
        duration_minutes=duration_minutes,
        price_minor=price_minor,
        currency="EGP",
        requires_laser_device=requires_laser_device,
        is_active=True,
    )
    db.add(service)
    db.flush()
    db.add(
        DoctorService(
            workspace_id=workspace_id,
            doctor_id=doctor_id,
            service_id=service.id,
            is_active=True,
        )
    )
    db.flush()
    return service


def _fixture(
    db: Session,
    scenario_id: str,
    *,
    with_package: bool = False,
    with_combo: bool = False,
) -> ScenarioContext:
    workspace, patient, doctor, underarm, bikini, arms = build_grouped_fixture(db)
    branch = db.get(Branch, workspace.primary_branch_id)
    assert branch is not None
    hydra = _add_service(
        db,
        workspace_id=workspace.id,
        doctor_id=doctor.id,
        name="Hydrafacial",
        slug="hydrafacial",
        price_minor=120_000,
        duration_minutes=45,
        requires_laser_device=False,
    )
    face_prime = _add_service(
        db,
        workspace_id=workspace.id,
        doctor_id=doctor.id,
        name="Face Laser Prime",
        slug="face-laser-prime",
        price_minor=80_000,
        duration_minutes=20,
        requires_laser_device=True,
    )
    combo = None
    if with_combo:
        combo = _add_service(
            db,
            workspace_id=workspace.id,
            doctor_id=doctor.id,
            name="Underarm + Bikini Laser Combo",
            slug="underarm-bikini-combo",
            price_minor=110_000,
            duration_minutes=45,
            requires_laser_device=True,
        )
    _device_price(
        db,
        workspace=workspace,
        service=underarm,
        device_key="prime_lase",
        device_name="Prime Lase",
        price_minor=60_000,
        duration_minutes=20,
    )
    _device_price(
        db,
        workspace=workspace,
        service=arms,
        device_key="prime_lase",
        device_name="Prime Lase",
        price_minor=100_000,
        duration_minutes=40,
    )
    _device_price(
        db,
        workspace=workspace,
        service=face_prime,
        device_key="prime_lase",
        device_name="Prime Lase",
        price_minor=80_000,
        duration_minutes=20,
    )
    if combo is not None:
        _device_price(
            db,
            workspace=workspace,
            service=combo,
            device_key="candela_gentle",
            device_name="Candela Gentle",
            price_minor=110_000,
            duration_minutes=45,
        )
    if with_package:
        db.add(
            PatientPackage(
                workspace_id=workspace.id,
                patient_id=patient.id,
                service_id=underarm.id,
                name="6 Sessions Underarm - Candela",
                sessions_purchased=6,
                opening_sessions_remaining=4,
                sessions_total_known=True,
                sale_price_minor=250_000,
                standalone_session_price_minor_at_purchase=50_000,
                laser_device_key="candela_gentle",
                laser_device_name="Candela Gentle",
                currency="EGP",
                purchased_at=datetime.now(UTC) - timedelta(days=30),
                status="active",
                source="staff",
            )
        )
    db.flush()
    return ScenarioContext(
        scenario_id=scenario_id,
        db=db,
        workspace=workspace,
        patient=patient,
        doctor=doctor,
        branch=branch,
        services={
            "underarm": underarm,
            "bikini": bikini,
            "arms": arms,
            "hydra": hydra,
            "face_prime": face_prime,
            **({"combo": combo} if combo is not None else {}),
        },
    )


def _create_occupancy(
    ctx: ScenarioContext,
    *,
    start_local: datetime,
    end_local: datetime,
    service_key: str = "bikini",
    device_key: str = "candela_gentle",
) -> Appointment:
    blocker = Patient(
        workspace_id=ctx.workspace.id,
        first_name="Audit",
        last_name="Blocker",
        preferred_language="ar",
        source="other",
        status="active",
    )
    ctx.db.add(blocker)
    ctx.db.flush()
    service = ctx.services[service_key]
    start_at = start_local.astimezone(UTC)
    end_at = end_local.astimezone(UTC)
    appointment = Appointment(
        workspace_id=ctx.workspace.id,
        patient_id=blocker.id,
        branch_id=ctx.branch.id,
        doctor_id=ctx.doctor.id,
        doctor_assignment_known=True,
        service_id=service.id,
        status="pending",
        source="ai",
        start_at=start_at,
        end_at=end_at,
        busy_start_at=start_at,
        busy_end_at=end_at,
        duration_minutes=max(1, int((end_at - start_at).total_seconds() // 60)),
        price_minor=service.price_minor,
        discount_minor=0,
        currency="EGP",
        laser_device_key=device_key,
        laser_device_name="Candela Gentle" if device_key == "candela_gentle" else "Prime Lase",
    )
    ctx.db.add(appointment)
    ctx.db.commit()
    return appointment


def _seed_pending_appointment(ctx: ScenarioContext, *, start_local: datetime) -> Appointment:
    service = ctx.services["hydra"]
    start_at = start_local.astimezone(UTC)
    end_at = start_at + timedelta(minutes=service.duration_minutes)
    appointment = Appointment(
        workspace_id=ctx.workspace.id,
        patient_id=ctx.patient.id,
        branch_id=ctx.branch.id,
        doctor_id=ctx.doctor.id,
        doctor_assignment_known=True,
        service_id=service.id,
        status="pending",
        source="ai",
        start_at=start_at,
        end_at=end_at,
        busy_start_at=start_at,
        busy_end_at=end_at,
        duration_minutes=service.duration_minutes,
        price_minor=service.price_minor,
        discount_minor=0,
        currency="EGP",
    )
    ctx.db.add(appointment)
    ctx.db.commit()
    return appointment


def _ensure_conversation(ctx: ScenarioContext) -> Conversation:
    if ctx.conversation_id is not None:
        conversation = ctx.db.get(Conversation, ctx.conversation_id)
        assert conversation is not None
        return conversation
    now = datetime.now(UTC)
    conversation = Conversation(
        workspace_id=ctx.workspace.id,
        patient_id=ctx.patient.id,
        channel="whatsapp",
        status="open",
        owner_type="ai",
        unread_count=0,
        started_at=now,
        last_message_at=now,
    )
    ctx.db.add(conversation)
    ctx.db.commit()
    ctx.conversation_id = conversation.id
    return conversation


def _active_task_payload(ctx: ScenarioContext) -> dict[str, Any] | None:
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


def _appointment_snapshot(ctx: ScenarioContext) -> list[dict[str, Any]]:
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
                "service_id": str(row.service_id),
                "doctor_id": str(row.doctor_id),
                "status": row.status,
                "start_at": row.start_at.isoformat(),
                "laser_device_key": row.laser_device_key,
                "patient_package_id": str(row.patient_package_id) if row.patient_package_id else None,
                "billing_context": row.billing_context,
                "additional_service_ids": [str(item.service_id) for item in additional],
                "additional_device_keys": [item.laser_device_key for item in additional],
            }
        )
    return result


def _human_flags(reply: str, active_task: dict[str, Any] | None) -> dict[str, str]:
    text = reply or ""
    lowered = text.lower()
    marker = "FLAG" if "tia_laser_device" in lowered or "||" in text else "CLEAN"
    repeated = "CLEAN"
    pieces = [part.strip() for part in text.replace("؟", ".").replace("!", ".").split(".") if part.strip()]
    normalized = [" ".join(part.split()).lower() for part in pieces]
    if len(normalized) != len(set(normalized)):
        repeated = "FLAG"
    clarifying = "CLEAN"
    requested = (active_task or {}).get("requested_service_ids") or []
    if requested and any(phrase in text for phrase in ("أنهي خدمة", "أي خدمة", "تقصد خدمة إيه", "تقصدي خدمة إيه")):
        clarifying = "FLAG"
    fallback = "FLAG" if ("معليش" in text and ("وضح" in text or "توض" in text)) else "CLEAN"
    natural = "FLAG" if repeated == "FLAG" or fallback == "FLAG" else "CLEAN"
    return {
        "Functional": "PENDING_SCENARIO_VALIDATION",
        "Naturalness": natural,
        "Repetition": repeated,
        "Unnecessary clarification": clarifying,
        "Fallback-looking": fallback,
        "Context loss": "CLEAN",
        "Contradiction": "CLEAN",
        "Internal metadata leak": marker,
    }


def _turn_record(ctx: ScenarioContext, *, customer: str, response: Any) -> dict[str, Any]:
    outbound = ctx.db.get(Message, response.outbound_message_id) if response.outbound_message_id else None
    metadata = dict(outbound.metadata_json or {}) if outbound is not None else {}
    task = _active_task_payload(ctx)
    record = {
        "scenario": ctx.scenario_id,
        "turn": len(ctx.turns) + 1,
        "customer": customer,
        "agent": response.reply or "",
        "model": response.model,
        "active_task": task,
        "read_context": metadata.get("v2_read_context"),
        "availability_reference_context": metadata.get("v2_availability_reference_context"),
        "action_context": metadata.get("v2_action_context"),
        "booking_proposal": metadata.get("v2_booking_proposal"),
        "appointments": _appointment_snapshot(ctx),
    }
    record["human_review"] = _human_flags(str(record["agent"]), task)
    ctx.turns.append(record)
    ALL_TURNS.append(record)
    print("AUDIT_TURN=" + json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
    return record


def _chat(ctx: ScenarioContext, customer: str) -> dict[str, Any]:
    turn_number = len(ctx.turns) + 1
    COLLECTOR.begin_turn(ctx.scenario_id, turn_number, customer)
    try:
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
    finally:
        COLLECTOR.end_turn()
    ctx.conversation_id = response.conversation_id
    return _turn_record(ctx, customer=customer, response=response)


def _button(ctx: ScenarioContext, *, customer_label: str, action_id: str) -> dict[str, Any]:
    conversation = _ensure_conversation(ctx)
    turn_number = len(ctx.turns) + 1
    COLLECTOR.begin_turn(ctx.scenario_id, turn_number, customer_label)
    now = datetime.now(UTC)
    inbound = Message(
        workspace_id=ctx.workspace.id,
        conversation_id=conversation.id,
        channel_connection_id=conversation.channel_connection_id,
        sender_type="patient",
        direction="inbound",
        created_at=now,
        message_type="text",
        content=customer_label,
        delivery_status="received",
        metadata_json={
            "agent_run_id": str(uuid4()),
            "interactive_reply": {"type": "button_reply", "id": action_id},
        },
    )
    conversation.last_message_at = now
    ctx.db.add(inbound)
    ctx.db.commit()
    try:
        response = process_whatsapp_booking_action(
            ctx.db,
            workspace=ctx.workspace,
            patient=ctx.patient,
            conversation=conversation,
            inbound=inbound,
        )
    finally:
        COLLECTOR.end_turn()
    if response is None:
        raise AuditFunctionalDefect(
            {"id": f"{ctx.scenario_id}-structured-action", "severity": "P1", "message": "Structured customer action was not handled."}
        )
    return _turn_record(ctx, customer=customer_label, response=response)


def _proposal_confirm_button(ctx: ScenarioContext) -> str:
    assert ctx.conversation_id is not None
    outbound = ctx.db.scalar(
        select(Message)
        .where(
            Message.conversation_id == ctx.conversation_id,
            Message.sender_type == "ai",
            Message.direction == "outbound",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    if outbound is None:
        raise AuditFunctionalDefect({"id": f"{ctx.scenario_id}-proposal", "severity": "P1", "message": "No outbound proposal message."})
    metadata = whatsapp_booking_dispatch_metadata(ctx.db, message=outbound)
    interactive = metadata.get("whatsapp_interactive")
    buttons = interactive.get("buttons") if isinstance(interactive, dict) else None
    if not isinstance(buttons, list) or not buttons:
        raise AuditFunctionalDefect({"id": f"{ctx.scenario_id}-proposal", "severity": "P1", "message": "No canonical proposal confirm button."})
    return str(buttons[0]["id"])


def _require(
    condition: bool,
    *,
    ctx: ScenarioContext,
    defect_id: str,
    message: str,
    severity: str = "P1",
    safety_counter: str | None = None,
    record: dict[str, Any] | None = None,
) -> None:
    if condition:
        return
    if safety_counter:
        SAFETY_COUNTERS[safety_counter] += 1
    payload = {
        "id": defect_id,
        "severity": severity,
        "scenario": ctx.scenario_id,
        "message": message,
        "customer_text": (record or {}).get("customer"),
        "agent_text": (record or {}).get("agent"),
        "canonical_state": (record or {}).get("active_task"),
        "db_effect": (record or {}).get("appointments"),
        "root_boundary": "audit-observed; follow-up investigation required",
        "recommended_follow_up_task": f"Investigate {defect_id} on exact audited SHA without patching this audit branch.",
    }
    DEFECTS.append(payload)
    print("AUDIT_DEFECT=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
    raise AuditFunctionalDefect(payload)


def _service_ids(record: dict[str, Any]) -> list[str]:
    task = record.get("active_task") or {}
    requested = [str(value) for value in task.get("requested_service_ids") or []]
    if requested:
        return requested
    constraints = task.get("constraints") or {}
    service_id = constraints.get("service_id")
    return [str(service_id)] if service_id else []


def _proposal(record: dict[str, Any]) -> dict[str, Any] | None:
    task = record.get("active_task") or {}
    proposal = task.get("proposal")
    return proposal if isinstance(proposal, dict) else None


def _reference_options(record: dict[str, Any]) -> list[dict[str, Any]]:
    for source_key in ("availability_reference_context", "read_context"):
        source = record.get(source_key)
        if isinstance(source, dict):
            options = source.get("availability_reference_options")
            if isinstance(options, list):
                return [item for item in options if isinstance(item, dict)]
    return []


def _option_start(option: dict[str, Any]) -> str:
    payload = option.get("payload") if isinstance(option.get("payload"), dict) else option
    for key in ("start_local", "start_at", "start"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if value:
            return str(value)
    return ""


def _scenario_pass(ctx: ScenarioContext, *, completed_booking: bool = False) -> None:
    for turn in ctx.turns:
        turn["human_review"]["Functional"] = "PASS"
        if turn["human_review"]["Internal metadata leak"] == "FLAG":
            SAFETY_COUNTERS["internal_marker_leaks"] += 1
            raise AuditFunctionalDefect(
                {"id": f"{ctx.scenario_id}-marker-leak", "severity": "P0", "scenario": ctx.scenario_id, "message": "Internal marker leaked to customer reply."}
            )
    SCENARIO_RESULTS.append(
        {"scenario": ctx.scenario_id, "functional": "PASS", "turns": ctx.turns, "completed_booking": completed_booking}
    )
    if completed_booking:
        COMPLETED_BOOKING_SCENARIOS.add(ctx.scenario_id)


def _run_isolated(
    scenario_id: str,
    body,
    *,
    with_package: bool = False,
    with_combo: bool = False,
) -> None:
    workspace_id: UUID | None = None
    with rollback_session() as db:
        ctx = _fixture(db, scenario_id, with_package=with_package, with_combo=with_combo)
        workspace_id = ctx.workspace.id
        body(ctx)
    assert workspace_id is not None
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as verification:
            persisted = int(
                verification.scalar(
                    select(func.count()).select_from(Appointment).where(Appointment.workspace_id == workspace_id)
                )
                or 0
            )
            if persisted:
                SAFETY_COUNTERS["persisted_audit_writes"] += persisted
                raise AuditFunctionalDefect(
                    {"id": f"{scenario_id}-rollback", "severity": "P0", "scenario": scenario_id, "message": f"Audit rollback leaked {persisted} appointment writes."}
                )
    finally:
        engine.dispose()


def _complete_booking_from_current_state(
    ctx: ScenarioContext,
    *,
    send_text_confirmation: bool = True,
) -> dict[str, Any]:
    if send_text_confirmation:
        record = _chat(ctx, "\u0623\u064a\u0648\u0647 \u0623\u0643\u062f \u0627\u0644\u062d\u062c\u0632")
    else:
        if not ctx.turns:
            raise AuditFunctionalDefect(
                {
                    "id": f"{ctx.scenario_id}-missing-proposal-turn",
                    "severity": "P1",
                    "scenario": ctx.scenario_id,
                    "message": "No current turn exists to confirm.",
                }
            )
        record = ctx.turns[-1]
    if record.get("appointments"):
        return record
    if _proposal(record) is not None:
        return _button(
            ctx,
            customer_label="[\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u062d\u062c\u0632]",
            action_id=_proposal_confirm_button(ctx),
        )
    return record


def scenario_c1(ctx: ScenarioContext) -> None:
    _chat(ctx, "عايزة أحجز ليزر بكيني يوم السبت 17 أكتوبر الساعة 8 مساء")
    final = _chat(ctx, "أيوه أكد الحجز")
    rows = final["appointments"]
    _require(len(rows) == 1, ctx=ctx, defect_id="C1-write-count", message="Simple booking did not create exactly one appointment.", safety_counter="wrong_writes", record=final)
    row = rows[0]
    _require(row["status"] == "pending", ctx=ctx, defect_id="C1-status", message="Booking creation did not remain pending.", safety_counter="accidental_pending_to_confirmed", record=final)
    _require(row["laser_device_key"] == "candela_gentle", ctx=ctx, defect_id="C1-device", message="Singleton compatible device was not selected canonically.", safety_counter="wrong_device_writes", record=final)
    _scenario_pass(ctx, completed_booking=True)


def scenario_c2(ctx: ScenarioContext) -> None:
    first = _chat(ctx, "عايزة أحجز ليزر إبط يوم السبت 17 أكتوبر الساعة 7 مساء")
    _require("Candela" in first["agent"] and "Prime" in first["agent"], ctx=ctx, defect_id="C2-device-choice", message="Different-price multi-device choice was not grounded once.", record=first)
    _chat(ctx, "Candela Gentle")
    third = _chat(ctx, "السعر كام؟")
    task = third.get("active_task") or {}
    constraints = task.get("constraints") or {}
    _require(constraints.get("device_key") == "candela_gentle", ctx=ctx, defect_id="C2-device-persist", message="Selected device did not persist.", record=third)
    _require("500" in third["agent"].replace(",", ""), ctx=ctx, defect_id="C2-price", message="Selected-device price was not grounded as 500 EGP.", safety_counter="wrong_price_claims", record=third)
    _require(not third["appointments"], ctx=ctx, defect_id="C2-premature-write", message="Read-only device/price journey wrote an appointment.", safety_counter="wrong_writes", record=third)
    _scenario_pass(ctx)


def scenario_c3(ctx: ScenarioContext) -> None:
    _chat(ctx, "عايزة أحجز ليزر إبط وليزر بكيني مع بعض في نفس الزيارة على Candela Gentle يوم السبت 17 أكتوبر الساعة 6 مساء")
    final = _complete_booking_from_current_state(ctx)
    rows = final["appointments"]
    count_counter = "duplicate_appointments" if len(rows) > 1 else "wrong_writes"
    _require(len(rows) == 1, ctx=ctx, defect_id="C3-appointment-count", message="Grouped booking did not create exactly one appointment after canonical confirmation.", safety_counter=count_counter, record=final)
    row = rows[0]
    _require(set([row["service_id"], *row["additional_service_ids"]]) == {str(ctx.services["underarm"].id), str(ctx.services["bikini"].id)}, ctx=ctx, defect_id="C3-services", message="Grouped visit lost or duplicated a service.", safety_counter="lost_requested_services", record=final)
    _require(row["laser_device_key"] == "candela_gentle" and all(value in (None, "candela_gentle") for value in row["additional_device_keys"]), ctx=ctx, defect_id="C3-cross-device", message="Grouped visit split laser devices.", safety_counter="cross_device_visits", record=final)
    _require(row["status"] == "pending", ctx=ctx, defect_id="C3-status", message="Grouped booking was not pending after creation.", safety_counter="accidental_pending_to_confirmed", record=final)
    _scenario_pass(ctx, completed_booking=True)


def scenario_c4(ctx: ScenarioContext) -> None:
    first = _chat(ctx, "عايزة أحجز ليزر إبط وليزر بكيني مع بعض في نفس الزيارة على Candela Gentle")
    second = _chat(ctx, "السعر الإجمالي كام؟")
    expected = {str(ctx.services["underarm"].id), str(ctx.services["bikini"].id)}
    _require(set(_service_ids(second)) == expected, ctx=ctx, defect_id="C4-service-set", message="Known grouped service set was not preserved during total-price read.", safety_counter="lost_requested_services", record=second)
    _require("1200" in second["agent"].replace(",", ""), ctx=ctx, defect_id="C4-total", message="Canonical grouped total was not 1200 EGP.", safety_counter="wrong_price_claims", record=second)
    _require("أنهي خدمة" not in second["agent"] and "أي خدمة" not in second["agent"], ctx=ctx, defect_id="C4-unnecessary-clarification", message="Agent re-asked a known service during grouped total price.", record=second)
    _require("TIA_LASER_DEVICE" not in first["agent"] + second["agent"] and "||" not in first["agent"] + second["agent"], ctx=ctx, defect_id="C4-marker", message="Internal device marker leaked.", safety_counter="internal_marker_leaks", record=second)
    _scenario_pass(ctx)


def scenario_c5(ctx: ScenarioContext) -> None:
    expected = {str(ctx.services["underarm"].id), str(ctx.services["bikini"].id)}
    _chat(ctx, "عايزة أحجز ليزر إبط وبكيني مع بعض على Candela Gentle يوم السبت 17 أكتوبر")
    _chat(ctx, "مين الدكتور اللي بيعمل الخدمتين؟")
    _chat(ctx, "مواعيد العيادة إيه؟")
    price = _chat(ctx, "والسعر الإجمالي كام؟")
    final = _chat(ctx, "تمام كمل الحجز")
    for record in (price, final):
        _require(set(_service_ids(record)) == expected, ctx=ctx, defect_id="C5-context", message="Side question lost grouped booking context.", safety_counter="lost_requested_services", record=record)
    _require("1200" in price["agent"].replace(",", ""), ctx=ctx, defect_id="C5-price", message="Price side read lost grouped total truth.", safety_counter="wrong_price_claims", record=price)
    _scenario_pass(ctx)


def scenario_c6(ctx: ScenarioContext) -> None:
    _chat(ctx, "عايزة أحجز ليزر إبط وبكيني مع بعض على Candela Gentle يوم السبت 17 أكتوبر الساعة 6 مساء")
    narrowed = _chat(ctx, "خليها ليزر إبط بس")
    price = _chat(ctx, "السعر الإجمالي كام؟")
    expected = [str(ctx.services["underarm"].id)]
    _require(_service_ids(narrowed) == expected and _service_ids(price) == expected, ctx=ctx, defect_id="C6-narrowing", message="Canonical narrowing did not reduce service set to Underarm only.", safety_counter="lost_requested_services", record=narrowed)
    _require(_proposal(narrowed) is None, ctx=ctx, defect_id="C6-stale-proposal", message="Old grouped proposal survived service narrowing.", safety_counter="wrong_writes", record=narrowed)
    _require("500" in price["agent"].replace(",", ""), ctx=ctx, defect_id="C6-price", message="Narrowed Underarm pricing was not canonical.", safety_counter="wrong_price_claims", record=price)
    _scenario_pass(ctx)


def scenario_c7(ctx: ScenarioContext) -> None:
    _chat(ctx, "عايزة أحجز ليزر إبط وبكيني مع بعض على Candela Gentle يوم السبت 17 أكتوبر")
    replaced = _chat(ctx, "بدل ليزر بكيني خليها Full Arms وخلي ليزر الإبط زي ما هو")
    expected = {str(ctx.services["underarm"].id), str(ctx.services["arms"].id)}
    _require(set(_service_ids(replaced)) == expected, ctx=ctx, defect_id="C7-replacement", message="Retained/replaced canonical service set is incorrect.", safety_counter="lost_requested_services", record=replaced)
    task = replaced.get("active_task") or {}
    _require((task.get("constraints") or {}).get("device_key") == "candela_gentle", ctx=ctx, defect_id="C7-device", message="Compatible selected device was not revalidated/preserved.", record=replaced)
    _scenario_pass(ctx)


def scenario_c8(ctx: ScenarioContext) -> None:
    _chat(ctx, "عايزة أحجز ليزر بكيني يوم الثلاثاء 20 أكتوبر 2026")
    _chat(ctx, "مين الدكتور اللي بيعمل الخدمة دي؟")
    corrected = _chat(ctx, "خليه الأربعاء 21 أكتوبر 2026")
    task = corrected.get("active_task") or {}
    date_constraint = (task.get("constraints") or {}).get("date") or {}
    date_text = json.dumps(date_constraint, ensure_ascii=False)
    _require("2026-10-21" in date_text and "2026-10-20" not in date_text, ctx=ctx, defect_id="C8-date", message="Wednesday did not authoritatively replace Tuesday.", record=corrected)
    _require(_service_ids(corrected) == [str(ctx.services["bikini"].id)], ctx=ctx, defect_id="C8-context", message="Date correction lost service context.", safety_counter="lost_requested_services", record=corrected)
    _scenario_pass(ctx)


def scenario_c9(ctx: ScenarioContext) -> None:
    _chat(ctx, "عايزة أحجز ليزر بكيني على Candela Gentle يوم الثلاثاء 20 أكتوبر 2026 الساعة 11 صباحًا")
    corrected = _chat(ctx, "خليه 12 بدل 11")
    task = corrected.get("active_task") or {}
    time_constraint = json.dumps((task.get("constraints") or {}).get("time") or {}, ensure_ascii=False)
    _require("12:00" in time_constraint and "11:00" not in time_constraint, ctx=ctx, defect_id="C9-task-time", message="Task time correction did not replace 11:00 with 12:00.", record=corrected)
    final = _complete_booking_from_current_state(ctx)
    rows = final["appointments"]
    written_local = datetime.fromisoformat(rows[0]["start_at"]).astimezone(CAIRO) if len(rows) == 1 else None
    _require(
        written_local is not None and written_local.strftime("%H:%M") == "12:00",
        ctx=ctx,
        defect_id="C9-db-time",
        message="DB write did not use corrected 12:00 Cairo-local time.",
        safety_counter="wrong_time_writes",
        record=final,
    )
    _scenario_pass(ctx, completed_booking=True)


def _hole_setup(ctx: ScenarioContext) -> None:
    _create_occupancy(
        ctx,
        start_local=datetime(2026, 10, 15, 16, 15, tzinfo=CAIRO),
        end_local=datetime(2026, 10, 15, 16, 30, tzinfo=CAIRO),
    )


def scenario_c10(ctx: ScenarioContext) -> None:
    _hole_setup(ctx)
    record = _chat(ctx, "وريني مواعيد ليزر بكيني يوم الخميس 15 أكتوبر 2026 من الساعة 4 العصر")
    starts = [_option_start(item) for item in _reference_options(record)]
    _require(any("T16:00" in value for value in starts), ctx=ctx, defect_id="C10-1600", message="16:00 valid slot was hidden.", safety_counter="hidden_availability_gaps", record=record)
    _require(not any("T16:15" in value for value in starts), ctx=ctx, defect_id="C10-gap", message="Blocked 16:15 appeared available.", safety_counter="fabricated_availability", record=record)
    _require(any("T16:30" in value or "T16:45" in value or "T17:" in value for value in starts), ctx=ctx, defect_id="C10-after-gap", message="Post-gap availability was hidden.", safety_counter="hidden_availability_gaps", record=record)
    _scenario_pass(ctx)


def scenario_c11(ctx: ScenarioContext) -> None:
    _hole_setup(ctx)
    record = _chat(ctx, "عايزة ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 4:15 العصر بالضبط")
    _require(not record["appointments"], ctx=ctx, defect_id="C11-write", message="Unavailable exact time caused a write.", safety_counter="wrong_writes", record=record)
    _require("4:15" not in record["agent"] or any(word in record["agent"] for word in ("مش متاح", "غير متاح", "مش فاضي")), ctx=ctx, defect_id="C11-false-slot", message="Agent silently accepted unavailable 16:15.", safety_counter="fabricated_availability", record=record)
    _require(bool(_reference_options(record)), ctx=ctx, defect_id="C11-alternatives", message="Verified alternatives were not presented for unavailable exact time.", record=record)
    _scenario_pass(ctx)


def scenario_c12(ctx: ScenarioContext) -> None:
    availability = _chat(ctx, "وريني مواعيد ليزر بكيني يوم الثلاثاء 20 أكتوبر 2026")
    options = _reference_options(availability)
    _require(bool(options), ctx=ctx, defect_id="C12-options", message="No displayed option context to select later.", record=availability)
    selected_start = _option_start(options[0])
    _chat(ctx, "السعر كام؟")
    clock = selected_start.split("T", 1)[1][:5] if "T" in selected_start else "10:00"
    selected = _chat(ctx, f"تمام اختار الميعاد اللي كان ظاهر الساعة {clock}")
    proposal = _proposal(selected)
    _require(proposal is not None and clock in str(proposal.get("start_at") or ""), ctx=ctx, defect_id="C12-selection", message="Displayed option was lost after side read.", record=selected)
    _scenario_pass(ctx)


def scenario_c13(ctx: ScenarioContext) -> None:
    proposal_turn = _chat(ctx, "عايزة أحجز ليزر بكيني على Candela Gentle يوم السبت 17 أكتوبر 2026 الساعة 8 مساء")
    proposal = _proposal(proposal_turn)
    _require(proposal is not None, ctx=ctx, defect_id="C13-proposal", message="Initial valid booking did not create a proposal.", record=proposal_turn)
    proposal_id = str(proposal["proposal_id"])
    side = _chat(ctx, "عنوان العيادة إيه؟")
    side_proposal = _proposal(side)
    _require(side_proposal is not None and str(side_proposal["proposal_id"]) == proposal_id, ctx=ctx, defect_id="C13-continuity", message="Valid proposal did not survive informational side read.", record=side)
    action_id = _proposal_confirm_button(ctx)
    final = _button(ctx, customer_label="[تأكيد الحجز]", action_id=action_id)
    _require(len(final["appointments"]) == 1, ctx=ctx, defect_id="C13-duplicate", message="Proposal confirmation did not create exactly one appointment.", safety_counter="duplicate_appointments", record=final)
    _scenario_pass(ctx, completed_booking=True)


def scenario_c14(ctx: ScenarioContext) -> None:
    _chat(ctx, "عايزة أحجز ليزر بكيني على Candela Gentle يوم السبت 17 أكتوبر 2026 الساعة 7 مساء")
    final = _complete_booking_from_current_state(ctx)
    rows = final["appointments"]
    _require(len(rows) == 1 and rows[0]["status"] == "pending", ctx=ctx, defect_id="C14-status", message="Successful booking creation did not persist pending status.", safety_counter="accidental_pending_to_confirmed", record=final)
    lowered = final["agent"].lower()
    _require("تأكيد الحضور" not in final["agent"] and "attendance confirmed" not in lowered, ctx=ctx, defect_id="C14-wording", message="Booking acknowledgment falsely claimed attendance confirmation.", safety_counter="false_success", record=final)
    _scenario_pass(ctx, completed_booking=True)


def scenario_c15(ctx: ScenarioContext) -> None:
    _chat(ctx, "عايزة أحجز ليزر بكيني على Candela Gentle يوم السبت 17 أكتوبر 2026 الساعة 5 مساء")
    booked = _complete_booking_from_current_state(ctx)
    _require(len(booked["appointments"]) == 1 and booked["appointments"][0]["status"] == "pending", ctx=ctx, defect_id="C15-seed", message="Could not establish pending booking for repeat-confirm audit.", record=booked)
    repeated = _chat(ctx, "أكد")
    rows = repeated["appointments"]
    _require(len(rows) == 1, ctx=ctx, defect_id="C15-duplicate", message="Bare repeat confirmation duplicated appointment.", safety_counter="duplicate_appointments", record=repeated)
    _require(rows[0]["status"] == "pending", ctx=ctx, defect_id="C15-attendance", message="Bare repeat confirmation mutated pending to confirmed.", safety_counter="accidental_pending_to_confirmed", record=repeated)
    _scenario_pass(ctx, completed_booking=True)


def scenario_c16(ctx: ScenarioContext) -> None:
    tomorrow = datetime.now(CAIRO).date() + timedelta(days=1)
    appointment = _seed_pending_appointment(
        ctx,
        start_local=datetime.combine(tomorrow, time(18, 0), tzinfo=CAIRO),
    )
    _ensure_conversation(ctx)
    action_id = attendance_confirm_action_id(appointment.id)
    final = _button(ctx, customer_label="[تأكيد الحضور]", action_id=action_id)
    ctx.db.refresh(appointment)
    _require(appointment.status == "confirmed", ctx=ctx, defect_id="C16-transition", message="Canonical attendance action did not transition pending to confirmed.", safety_counter="false_success", record=final)
    _require(bool(final["agent"].strip()), ctx=ctx, defect_id="C16-ack", message="Attendance confirmation had no natural acknowledgment.", record=final)
    _scenario_pass(ctx)


def scenario_c17(ctx: ScenarioContext) -> None:
    _chat(ctx, "عايزة أحجز جلسة ليزر إبط من الباكدج بتاعتي على Candela Gentle يوم السبت 17 أكتوبر 2026 الساعة 4 مساء")
    final = _complete_booking_from_current_state(ctx)
    rows = final["appointments"]
    _require(len(rows) == 1, ctx=ctx, defect_id="C17-write", message="Package-backed booking did not create one appointment.", safety_counter="wrong_writes", record=final)
    row = rows[0]
    _require(row["patient_package_id"] is not None and row["billing_context"] == "package_prepaid", ctx=ctx, defect_id="C17-package", message="Active package context was not used for booking.", safety_counter="wrong_writes", record=final)
    _require(row["status"] == "pending", ctx=ctx, defect_id="C17-status", message="Package-backed booking did not remain pending.", safety_counter="accidental_pending_to_confirmed", record=final)
    _require("1200" not in final["agent"] and "500 جنيه مستحق" not in final["agent"], ctx=ctx, defect_id="C17-payable", message="Package-backed booking made a false payable-total claim.", safety_counter="wrong_price_claims", record=final)
    usage_count = int(ctx.db.scalar(select(func.count()).select_from(PackageUsage).where(PackageUsage.workspace_id == ctx.workspace.id)) or 0)
    _require(usage_count == 1, ctx=ctx, defect_id="C17-usage", message="Package reservation ledger did not contain exactly one usage.", safety_counter="partial_writes", record=final)
    _scenario_pass(ctx, completed_booking=True)


def scenario_c18(ctx: ScenarioContext) -> None:
    record = _chat(ctx, "عايزة أحجز ليزر بكيني و Face Laser Prime مع بعض في نفس الزيارة")
    _require(not record["appointments"], ctx=ctx, defect_id="C18-write", message="Zero-common-device grouped request wrote an appointment.", safety_counter="cross_device_visits", record=record)
    proposal = _proposal(record)
    _require(proposal is None, ctx=ctx, defect_id="C18-proposal", message="Zero-common-device grouped request produced a proposal.", safety_counter="cross_device_visits", record=record)
    _scenario_pass(ctx)


def scenario_c19(ctx: ScenarioContext) -> None:
    _chat(ctx, "عايزة أحجز ليزر إبط وبكيني مع بعض في نفس الزيارة على Candela Gentle")
    replaced = _chat(ctx, "بدل الخدمتين بخدمة Underarm + Bikini Laser Combo بس")
    combo_id = str(ctx.services["combo"].id)
    _require(_service_ids(replaced) == [combo_id], ctx=ctx, defect_id="C19-set", message="Combo replacement retained covered constituent services.", safety_counter="lost_requested_services", record=replaced)
    price = _chat(ctx, "السعر الإجمالي كام؟")
    _require(_service_ids(price) == [combo_id], ctx=ctx, defect_id="C19-resurrect", message="Constituent services resurrected after combo replacement.", safety_counter="double_counted_prices", record=price)
    _require("1100" in price["agent"].replace(",", ""), ctx=ctx, defect_id="C19-price", message="Combo price was double-counted or incorrect.", safety_counter="double_counted_prices", record=price)
    _scenario_pass(ctx)


def scenario_c20(ctx: ScenarioContext) -> None:
    expected = {str(ctx.services["underarm"].id), str(ctx.services["bikini"].id)}
    _chat(ctx, "عايزة أحجز ليزر إبط وبكيني مع بعض في نفس الزيارة")
    _chat(ctx, "على Candela Gentle")
    _chat(ctx, "يوم السبت 17 أكتوبر 2026")
    availability = _chat(ctx, "وريني المواعيد المتاحة")
    _chat(ctx, "مين الدكتور اللي هيعمل الجلسة؟")
    price = _chat(ctx, "السعر الإجمالي كام؟")
    _require("1200" in price["agent"].replace(",", ""), ctx=ctx, defect_id="C20-price", message="Long continuation lost grouped price truth.", safety_counter="wrong_price_claims", record=price)
    options = _reference_options(availability)
    clock = "18:00"
    if options:
        start = _option_start(options[0])
        if "T" in start:
            clock = start.split("T", 1)[1][:5]
    proposal_turn = _chat(ctx, f"خليه الساعة {clock} بدل أي ميعاد تاني")
    if _proposal(proposal_turn) is None:
        proposal_turn = _chat(ctx, "تمام جهز الحجز على الميعاد ده")
    _require(_proposal(proposal_turn) is not None, ctx=ctx, defect_id="C20-proposal", message="Long realistic journey failed to reach a canonical proposal.", record=proposal_turn)
    final = _complete_booking_from_current_state(ctx, send_text_confirmation=False)
    rows = final["appointments"]
    _require(len(rows) == 1 and rows[0]["status"] == "pending", ctx=ctx, defect_id="C20-write", message="Long continuation did not finish as one pending appointment.", safety_counter="wrong_writes", record=final)
    _require(set([rows[0]["service_id"], *rows[0]["additional_service_ids"]]) == expected, ctx=ctx, defect_id="C20-services", message="Long continuation lost a requested grouped service.", safety_counter="lost_requested_services", record=final)
    _scenario_pass(ctx, completed_booking=True)


def scenario_relative_date(ctx: ScenarioContext) -> None:
    _create_occupancy(
        ctx,
        start_local=datetime(2026, 10, 17, 10, 0, tzinfo=CAIRO),
        end_local=datetime(2026, 10, 17, 22, 0, tzinfo=CAIRO),
    )
    _chat(ctx, "عايزة أحجز ليزر بكيني يوم السبت")
    next_sat = _chat(ctx, "طب السبت اللي بعده")
    task = next_sat.get("active_task") or {}
    date_constraint = json.dumps((task.get("constraints") or {}).get("date") or {}, ensure_ascii=False)
    _require("2026-10-24" in date_constraint and "2026-10-18" not in date_constraint, ctx=ctx, defect_id="R1-next-saturday", message="'السبت اللي بعده' resolved to next calendar day instead of next Saturday.", record=next_sat)
    _scenario_pass(ctx)


def _status_label_contract() -> dict[str, bool]:
    text = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "status.ts").read_text(encoding="utf-8")
    return {
        "pending_appointment_is_reserved": 'pending: "\u0645\u062d\u062c\u0648\u0632"' in text,
        "confirmed_appointment_is_confirmed": 'confirmed: "\u0645\u0624\u0643\u062f"' in text,
        "generic_pending_unchanged": 'pending: "\u0642\u064a\u062f \u0627\u0644\u0627\u0646\u062a\u0638\u0627\u0631"' in text,
    }


def _tier_key(call: UsageCall) -> str | None:
    tier = (call.service_tier or "default").strip().lower()
    if tier in {"", "default", "auto", "standard"}:
        if call.actual_model and call.actual_model.lower().startswith("gpt-6-luna"):
            return "standard_long" if int(call.input_tokens or 0) > LUNA_SHORT_CONTEXT_LIMIT else "standard_short"
        return "standard_short"
    return None


def price_usage_call(call: UsageCall) -> None:
    if call.usage_status != "MEASURED":
        call.pricing_status = "UNMEASURED_USAGE"
        return
    model = (call.actual_model or call.requested_model or "").lower()
    family = next((name for name in PRICING if model.startswith(name)), None)
    if family is None:
        call.pricing_status = "UNKNOWN_MODEL_PRICE"
        return
    tier_key = _tier_key(call)
    if tier_key is None or tier_key not in PRICING[family]:
        call.pricing_status = "UNKNOWN_SERVICE_TIER_PRICE"
        return
    rates = PRICING[family][tier_key]
    input_tokens = int(call.input_tokens or 0)
    cached = int(call.cached_input_tokens or 0)
    cache_write = int(call.cache_write_tokens or 0)
    output = int(call.output_tokens or 0)
    uncached = max(0, input_tokens - cached - cache_write)
    if cache_write and rates["cache_write"] is None:
        call.pricing_status = "CACHE_WRITE_PRICE_UNAVAILABLE"
        return
    call.cost_usd = (
        uncached * float(rates["input"])
        + cached * float(rates["cached"])
        + cache_write * float(rates["cache_write"] or 0)
        + output * float(rates["output"])
    ) / 1_000_000
    call.pricing_status = "MEASURED"


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def build_usage_report() -> dict[str, Any]:
    COLLECTOR.finalize_attempts()
    for call in COLLECTOR.calls:
        price_usage_call(call)
    calls = COLLECTOR.calls
    turn_keys = [(turn["scenario"], turn["turn"]) for turn in ALL_TURNS]
    costs_by_turn: dict[tuple[str, int], float] = {key: 0.0 for key in turn_keys}
    calls_by_turn: dict[tuple[str, int], list[UsageCall]] = {key: [] for key in turn_keys}
    for call in calls:
        key = (call.scenario_id, call.turn_number)
        calls_by_turn.setdefault(key, []).append(call)
        if call.cost_usd is not None:
            costs_by_turn[key] = costs_by_turn.get(key, 0.0) + call.cost_usd
    turn_costs = list(costs_by_turn.values())
    measured_cost = sum(turn_costs)
    turns = len(ALL_TURNS)
    total_input = sum(call.input_tokens or 0 for call in calls if call.usage_status == "MEASURED")
    total_cached = sum(call.cached_input_tokens or 0 for call in calls if call.usage_status == "MEASURED")
    total_cache_write = sum(call.cache_write_tokens or 0 for call in calls if call.usage_status == "MEASURED")
    total_output = sum(call.output_tokens or 0 for call in calls if call.usage_status == "MEASURED")
    total_reasoning = sum(call.reasoning_tokens or 0 for call in calls if call.usage_status == "MEASURED")
    primary = settings.openai_model.lower()
    fallback = (settings.openai_fallback_model or "").lower()
    primary_calls = sum(1 for call in calls if (call.actual_model or call.requested_model or "").lower().startswith(primary))
    fallback_calls = sum(1 for call in calls if fallback and (call.actual_model or call.requested_model or "").lower().startswith(fallback))
    stage_costs = {
        "interpreter": sum(call.cost_usd or 0 for call in calls if call.stage_group == "interpreter"),
        "composer": sum(call.cost_usd or 0 for call in calls if call.stage_group == "composer"),
        "retry": sum(call.cost_usd or 0 for call in calls if call.retry),
        "fallback": sum(call.cost_usd or 0 for call in calls if call.fallback),
        "other": sum(call.cost_usd or 0 for call in calls if call.stage_group == "other"),
    }
    journey_costs = []
    for scenario_id in sorted(COMPLETED_BOOKING_SCENARIOS):
        journey_costs.append(sum(cost for (scenario, _), cost in costs_by_turn.items() if scenario == scenario_id))
    turn_lookup = {(turn["scenario"], turn["turn"]): turn for turn in ALL_TURNS}
    top = []
    for key, cost in sorted(costs_by_turn.items(), key=lambda item: item[1], reverse=True)[:10]:
        turn = turn_lookup.get(key, {})
        turn_calls = calls_by_turn.get(key, [])
        top.append(
            {
                "scenario": key[0],
                "turn": key[1],
                "customer_intent": turn.get("customer"),
                "model_calls": len(turn_calls),
                "input_tokens": sum(call.input_tokens or 0 for call in turn_calls),
                "output_tokens": sum(call.output_tokens or 0 for call in turn_calls),
                "retries": sum(1 for call in turn_calls if call.retry),
                "fallback": any(call.fallback for call in turn_calls),
                "cost_usd": cost,
                "cost_drivers": {
                    "long_history_or_context": sum(call.input_tokens or 0 for call in turn_calls) > 20_000,
                    "multiple_llm_stages": len({call.call_stage for call in turn_calls}) > 1,
                    "retry": any(call.retry for call in turn_calls),
                    "fallback_model": any(call.fallback for call in turn_calls),
                    "large_output": sum(call.output_tokens or 0 for call in turn_calls) > 1_000,
                },
            }
        )
    unmeasured = [call for call in calls if call.usage_status != "MEASURED" or call.pricing_status != "MEASURED"]
    return {
        "inbound_customer_turns": turns,
        "total_llm_calls": len(calls),
        "average_calls_per_turn": (len(calls) / turns) if turns else 0,
        "primary_model_calls": primary_calls,
        "fallback_model_calls": fallback_calls,
        "fallback_rate": (fallback_calls / len(calls)) if calls else 0,
        "tokens": {
            "input": total_input,
            "cached_input": total_cached,
            "cache_write": total_cache_write,
            "output": total_output,
            "reasoning": total_reasoning,
            "cache_hit_ratio": (total_cached / total_input) if total_input else 0,
        },
        "cost": {
            "total_observed_usd": measured_cost,
            "observed_average_per_customer_message_usd": (measured_cost / turns) if turns else 0,
            "p50_usd": statistics.median(turn_costs) if turn_costs else 0,
            "p95_usd": _percentile(turn_costs, 0.95),
            "max_single_turn_usd": max(turn_costs) if turn_costs else 0,
            "per_100_messages_usd": (measured_cost / turns * 100) if turns else 0,
            "per_1000_messages_usd": (measured_cost / turns * 1000) if turns else 0,
            "average_completed_booking_journey_usd": statistics.mean(journey_costs) if journey_costs else 0,
        },
        "cost_by_stage": stage_costs,
        "top_10_expensive_turns": top,
        "unmeasured_or_unpriced_calls": len(unmeasured),
        "cost_baseline_status": "MEASURED" if not unmeasured else "PARTIAL",
        "calls": [call.__dict__ for call in calls],
    }


def _report(*, stopped: bool = False) -> dict[str, Any]:
    usage = build_usage_report()
    labels = _status_label_contract()
    return {
        "title": "Linka Final Combined Real Agent E2E + Cost Baseline",
        "starting_main": os.environ.get("AUDIT_STARTING_MAIN") or _git_sha(),
        "exact_audited_sha": os.environ.get("AUDIT_STARTING_MAIN") or _git_sha(),
        "audit_tooling_sha": _git_sha(),
        "model_configuration": {
            "primary_model": settings.openai_model,
            "fallback_model": settings.openai_fallback_model,
            "reasoning_effort": settings.openai_reasoning_effort,
            "fallback_reasoning_effort": settings.openai_fallback_reasoning_effort,
            "realtime_provider_retries": settings.llm_realtime_max_retries,
        },
        "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
        "pricing_snapshot": PRICING,
        "audit": {
            "status": "STOPPED_ON_DEFECT" if stopped else "COMPLETED",
            "customer_turns": len(ALL_TURNS),
            "scenarios_completed": len(SCENARIO_RESULTS),
            "functional_pass": sum(1 for item in SCENARIO_RESULTS if item["functional"] == "PASS"),
            "acceptable": 0,
            "fail": len(DEFECTS),
            "scenario_results": SCENARIO_RESULTS,
        },
        "safety": dict(SAFETY_COUNTERS),
        "reception_status": labels,
        "llm_usage_and_cost": usage,
        "defects": DEFECTS,
    }


def _write_report(report: dict[str, Any]) -> None:
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("FINAL_AUDIT_REPORT=" + json.dumps(report, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)


def main() -> int:
    if settings.environment == "production":
        raise SystemExit("Refusing to run final combined audit in production.")
    if not settings.agent_v2_live_enabled:
        raise SystemExit("Final combined audit requires AGENT_V2_LIVE_ENABLED=true.")
    install_usage_collector()
    scenarios = [
        ("C1", scenario_c1, False, False),
        ("C2", scenario_c2, False, False),
        ("C3", scenario_c3, False, False),
        ("C4", scenario_c4, False, False),
        ("C5", scenario_c5, False, False),
        ("C6", scenario_c6, False, False),
        ("C7", scenario_c7, False, False),
        ("C8", scenario_c8, False, False),
        ("C9", scenario_c9, False, False),
        ("C10", scenario_c10, False, False),
        ("C11", scenario_c11, False, False),
        ("C12", scenario_c12, False, False),
        ("C13", scenario_c13, False, False),
        ("C14", scenario_c14, False, False),
        ("C15", scenario_c15, False, False),
        ("C16", scenario_c16, False, False),
        ("C17", scenario_c17, True, False),
        ("C18", scenario_c18, False, False),
        ("C19", scenario_c19, False, True),
        ("C20", scenario_c20, False, False),
        ("R1", scenario_relative_date, False, False),
    ]
    try:
        for scenario_id, body, with_package, with_combo in scenarios:
            print(f"AUDIT_SCENARIO_START={scenario_id}", flush=True)
            _run_isolated(
                scenario_id,
                body,
                with_package=with_package,
                with_combo=with_combo,
            )
            print(f"AUDIT_SCENARIO_PASS={scenario_id}", flush=True)
    except AuditFunctionalDefect:
        report = _report(stopped=True)
        _write_report(report)
        return 2

    labels = _status_label_contract()
    if not all(labels.values()):
        DEFECTS.append(
            {
                "id": "RECEPTION-STATUS-CONTRACT",
                "severity": "P1",
                "scenario": "static-contract",
                "message": "Reception/generic status label invariant changed.",
                "root_boundary": "frontend status presentation",
                "recommended_follow_up_task": "Restore appointment-only pending/confirmed reception labels without changing DB lifecycle.",
            }
        )
        report = _report(stopped=True)
        _write_report(report)
        return 2

    report = _report(stopped=False)
    _write_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
