from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Self
from uuid import UUID
from zoneinfo import ZoneInfo

from app.agents.clinic_grounding import build_clinic_catalog
from app.integrations.clinic.base import AvailabilityRequest, ClinicCapability
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.agent_action import AgentAction
from app.models.appointment import Appointment
from app.models.conversation import Conversation
from app.models.handoff_request import HandoffRequest
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.pulse_billing import (
    AppointmentPulseSettlement,
    PatientPulsePack,
    PulseUsage,
)
from app.models.service import Service
from app.models.workspace import Workspace
from app.schemas.agent import AgentChatRequest
from app.services.agent_v2 import live_chat as live_chat_module
from app.services.agent_v2.live_chat import run_agent_chat
from app.services.patient_packages import list_patient_packages
from app.services.pulse_billing import list_patient_pulse_balances
from app.services.workspace_runtime_policy import workspace_runtime_policy
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    uncached_input_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    metadata_missing_calls: int = 0

    def add(self, usage: dict[str, Any] | None) -> None:
        self.calls += 1
        if not usage:
            self.metadata_missing_calls += 1
            return
        inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (inp + out))
        details = usage.get("input_token_details") or usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cache_read") or details.get("cached_tokens") or 0)
        cache_write = int(
            details.get("cache_creation") or details.get("cache_write_tokens") or 0
        )
        self.input_tokens += inp
        self.output_tokens += out
        self.cached_tokens += cached
        self.cache_write_tokens += cache_write
        self.uncached_input_tokens += max(0, inp - cached - cache_write)
        self.total_tokens += total


@dataclass
class TurnCapture:
    scenario_id: str
    turn_number: int
    user_message: str
    agent_response: str | None
    model: str | None
    latency_ms: int
    token_usage: dict[str, int]
    runtime_state: dict[str, Any]
    verified_reads: list[str]
    actions: list[dict[str, Any]]
    write_attempted: bool
    write_result: str | None
    handoff_state: dict[str, Any] | None
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    structured_trace: list[dict[str, Any]] = field(default_factory=list)
    context_metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class ScenarioResult:
    id: str
    category: str
    purpose: str
    turns: list[TurnCapture]
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    db_verification: dict[str, Any]
    evaluation: dict[str, str]
    issues: list[dict[str, str]]
    token_usage: dict[str, int]
    execution_error: str | None = None
    cost: dict[str, float] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump())
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return str(value)


def aggregate_tokens(turns: list[TurnCapture]) -> dict[str, int]:
    keys = (
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "uncached_input_tokens",
        "total_tokens",
        "calls",
        "metadata_missing_calls",
    )
    return {
        key: sum(int(turn.token_usage.get(key, 0)) for turn in turns)
        for key in keys
    }


def batch_token_summary(results: list[ScenarioResult]) -> dict[str, Any]:
    totals = [int(row.token_usage["total_tokens"]) for row in results]
    output = {
        "input_tokens": sum(int(row.token_usage["input_tokens"]) for row in results),
        "output_tokens": sum(int(row.token_usage["output_tokens"]) for row in results),
        "cached_tokens": sum(int(row.token_usage["cached_tokens"]) for row in results),
        "cache_write_tokens": sum(
            int(row.token_usage.get("cache_write_tokens", 0)) for row in results
        ),
        "uncached_input_tokens": sum(
            int(row.token_usage.get("uncached_input_tokens", 0)) for row in results
        ),
        "total_tokens": sum(totals),
        "average_tokens_per_conversation": round(statistics.mean(totals), 2) if totals else 0,
        "median_tokens_per_conversation": statistics.median(totals) if totals else 0,
    }
    if results:
        maximum = max(results, key=lambda row: row.token_usage["total_tokens"])
        minimum = min(results, key=lambda row: row.token_usage["total_tokens"])
        output.update(
            {
                "max_tokens": maximum.token_usage["total_tokens"],
                "max_scenario": maximum.id,
                "min_tokens": minimum.token_usage["total_tokens"],
                "min_scenario": minimum.id,
            }
        )
    return output


def assert_demo_only(workspace: Workspace) -> None:
    policy = workspace_runtime_policy(workspace)
    if not workspace.is_active or not policy.is_demo:
        raise RuntimeError(
            "Refusing evaluation: workspace is not an active Demo workspace."
        )
    if any(
        (
            policy.allow_external_dispatch,
            policy.allow_external_configuration,
            policy.allow_external_ingress,
            policy.allow_external_sync,
        )
    ):
        raise RuntimeError(
            "Refusing evaluation: Demo external side effects are not fully blocked."
        )


class RuntimeProbe:
    """Eval-only instrumentation for model usage, prompt attribution, and V2 traces."""

    def __init__(self) -> None:
        self.usage = TokenUsage()
        self.turns: list[Any] = []
        self.llm_calls: list[dict[str, Any]] = []
        self.interpreter_contexts: list[dict[str, Any]] = []
        self._current_attribution: dict[str, Any] | None = None
        self._attribution_attempt = 0
        self._original_generate = None
        self._original_orchestrate = None
        self._original_build_interpreter_messages = None
        self._original_build_responder_messages = None

    def __enter__(self) -> Self:
        from app.agents.v2 import responder as responder_module
        from app.agents.v2 import turn_interpreter as turn_interpreter_module
        from app.core.config import settings
        from langchain_openai import ChatOpenAI

        from tools.agent_eval.token_attribution import (
            attach_actual_usage,
            interpreter_attribution,
            responder_attribution,
        )

        self._original_generate = ChatOpenAI._generate
        self._original_orchestrate = live_chat_module.orchestrate_v2_turn
        self._original_build_interpreter_messages = (
            turn_interpreter_module._build_interpreter_messages
        )
        self._original_build_responder_messages = (
            responder_module._build_responder_messages
        )
        probe = self

        def build_interpreter_messages(*args, **kwargs):
            messages = probe._original_build_interpreter_messages(*args, **kwargs)
            semantic_context = kwargs.get("semantic_context")
            model_input = (
                dict(getattr(semantic_context, "model_input", {}) or {})
                if semantic_context is not None
                else {}
            )
            probe._current_attribution = interpreter_attribution(
                messages=messages,
                model_input=model_input,
            )
            attribution = dict(probe._current_attribution or {})
            probe.interpreter_contexts.append(
                {
                    "native_history_messages_exposed": max(0, len(messages) - 3),
                    "history_text_tokens_estimated": int(
                        attribution.get("conversation_history_tokens_estimated") or 0
                    ),
                    "semantic_context_tokens_estimated": int(
                        attribution.get("semantic_context_tokens_estimated") or 0
                    ),
                    "active_task_tokens_estimated": int(
                        attribution.get("active_task_tokens_estimated") or 0
                    ),
                    "recent_verified_read_tokens_estimated": int(
                        attribution.get("recent_verified_read_tokens_estimated") or 0
                    ),
                    "recent_verified_action_tokens_estimated": int(
                        attribution.get("recent_verified_action_tokens_estimated") or 0
                    ),
                }
            )
            probe._attribution_attempt = 0
            return messages

        def build_responder_messages(*args, **kwargs):
            messages = probe._original_build_responder_messages(*args, **kwargs)
            probe._current_attribution = responder_attribution(messages=messages)
            probe._attribution_attempt = 0
            return messages

        def generate(model_self, *args, **kwargs):
            attribution = dict(
                probe._current_attribution
                or {
                    "operation": "unknown",
                    "system_tokens_estimated": 0,
                    "semantic_context_tokens_estimated": 0,
                    "semantic_catalog_tokens_estimated": 0,
                    "conversation_history_tokens_estimated": 0,
                    "latest_user_tokens_estimated": 0,
                    "active_task_tokens_estimated": 0,
                    "pending_choice_tokens_estimated": 0,
                    "recent_verified_read_tokens_estimated": 0,
                    "recent_verified_action_tokens_estimated": 0,
                    "grounded_outcome_tokens_estimated": 0,
                    "structured_schema_tokens_estimated": 0,
                    "message_tokens_estimated": 0,
                    "message_plus_schema_tokens_estimated": 0,
                }
            )
            probe._attribution_attempt += 1
            attempt_index = probe._attribution_attempt
            model_name = str(
                getattr(model_self, "model_name", None)
                or getattr(model_self, "model", None)
                or "unknown"
            )
            fallback_used = bool(
                settings.openai_fallback_model
                and settings.openai_fallback_model != settings.openai_model
                and model_name == settings.openai_fallback_model
            )
            started = perf_counter()
            try:
                result = probe._original_generate(model_self, *args, **kwargs)
            except BaseException as exc:
                latency_ms = int((perf_counter() - started) * 1000)
                probe.llm_calls.append(
                    attach_actual_usage(
                        attribution,
                        input_tokens=0,
                        output_tokens=0,
                        cached_tokens=0,
                        cache_write_tokens=0,
                        total_tokens=0,
                        model=model_name,
                        latency_ms=latency_ms,
                        attempt_index=attempt_index,
                        fallback_used=fallback_used,
                        error=type(exc).__name__,
                    )
                )
                raise

            latency_ms = int((perf_counter() - started) * 1000)
            call_usage = TokenUsage()
            generations = getattr(result, "generations", []) or []
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if usage is None:
                    metadata = getattr(message, "response_metadata", {}) or {}
                    usage = metadata.get("token_usage") or metadata.get("usage")
                usage_dict = dict(usage) if usage else None
                probe.usage.add(usage_dict)
                call_usage.add(usage_dict)

            if not generations:
                probe.usage.add(None)
                call_usage.add(None)

            probe.llm_calls.append(
                attach_actual_usage(
                    attribution,
                    input_tokens=call_usage.input_tokens,
                    output_tokens=call_usage.output_tokens,
                    cached_tokens=call_usage.cached_tokens,
                    cache_write_tokens=call_usage.cache_write_tokens,
                    total_tokens=call_usage.total_tokens,
                    model=model_name,
                    latency_ms=latency_ms,
                    attempt_index=attempt_index,
                    fallback_used=fallback_used,
                )
            )
            return result

        def orchestrate(*args, **kwargs):
            turn = probe._original_orchestrate(*args, **kwargs)
            probe.turns.append(turn)
            return turn

        ChatOpenAI._generate = generate
        live_chat_module.orchestrate_v2_turn = orchestrate
        turn_interpreter_module._build_interpreter_messages = build_interpreter_messages
        responder_module._build_responder_messages = build_responder_messages
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        from app.agents.v2 import responder as responder_module
        from app.agents.v2 import turn_interpreter as turn_interpreter_module
        from langchain_openai import ChatOpenAI

        ChatOpenAI._generate = self._original_generate
        live_chat_module.orchestrate_v2_turn = self._original_orchestrate
        turn_interpreter_module._build_interpreter_messages = (
            self._original_build_interpreter_messages
        )
        responder_module._build_responder_messages = (
            self._original_build_responder_messages
        )


def active_branch_id(catalog: dict[str, Any]) -> str:
    branches = [
        row
        for row in catalog.get("branches", [])
        if isinstance(row, dict) and row.get("id")
    ]
    if not branches:
        raise RuntimeError("Demo catalog has no active branch.")
    return str(branches[0]["id"])


def booking_context(
    db: Session,
    workspace: Workspace,
    *,
    service_slug: str | None = None,
    exclude_service_id: str | None = None,
):
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)
    services = [
        row
        for row in catalog.get("services", [])
        if isinstance(row, dict) and row.get("id")
    ]
    doctors = [
        row
        for row in catalog.get("doctors", [])
        if isinstance(row, dict) and row.get("id")
    ]
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    adapter.require_capability(ClinicCapability.AVAILABILITY_READ)
    today = datetime.now(UTC).date()
    if service_slug:
        service_row = db.scalar(
            select(Service).where(
                Service.workspace_id == workspace.id,
                Service.slug == service_slug,
                Service.is_active.is_(True),
            )
        )
        if service_row is None:
            raise RuntimeError(f"Required demo service missing: {service_slug}")
        services = [
            row
            for row in services
            if str(row.get("id")) == str(service_row.id)
        ]

    for service in services:
        service_id = str(service["id"])
        if exclude_service_id and service_id == str(exclude_service_id):
            continue
        for doctor in doctors:
            if service_id not in {
                str(value) for value in (doctor.get("service_ids") or [])
            }:
                continue
            scheduled = {
                str(value)
                for value in (
                    doctor.get("scheduled_branch_ids")
                    or doctor.get("branch_ids")
                    or []
                )
                if value
            }
            if scheduled and branch_id not in scheduled:
                continue
            for offset in range(1, 36):
                day = today + timedelta(days=offset)
                available = adapter.get_availability(
                    AvailabilityRequest(
                        branch_id=branch_id,
                        service_id=service_id,
                        booking_date=day,
                        doctor_id=str(doctor["id"]),
                    )
                )
                if available.slots:
                    return catalog, service, doctor, branch_id, day, available
    raise RuntimeError(
        f"No bookable context found for service_slug={service_slug!r}"
    )


def find_patient(db: Session, workspace: Workspace) -> Patient:
    patient = db.scalar(
        select(Patient)
        .where(
            Patient.workspace_id == workspace.id,
            Patient.status != "blocked",
        )
        .order_by(Patient.created_at.asc())
        .limit(1)
    )
    if patient is None:
        raise RuntimeError("Demo has no active patient.")
    return patient


def package_patient(
    db: Session,
    workspace: Workspace,
    *,
    exclude_service_id: UUID | None = None,
) -> tuple[Patient, PatientPackage]:
    packages = list(
        db.scalars(
            select(PatientPackage)
            .where(
                PatientPackage.workspace_id == workspace.id,
                PatientPackage.status == "active",
            )
            .order_by(PatientPackage.purchased_at.desc())
            .limit(100)
        )
    )
    for package in packages:
        if exclude_service_id and package.service_id == exclude_service_id:
            continue
        usable = list_patient_packages(
            db,
            workspace_id=workspace.id,
            patient_id=package.patient_id,
            service_id=package.service_id,
            usable_only=True,
        )
        if not usable:
            continue
        patient = db.scalar(
            select(Patient).where(
                Patient.workspace_id == workspace.id,
                Patient.id == package.patient_id,
                Patient.status != "blocked",
            )
        )
        if patient:
            return patient, package
    raise RuntimeError("Demo has no usable package patient for this scenario.")


def seed_appointment(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    context=None,
) -> Appointment:
    if context is None:
        context = booking_context(db, workspace)
    _, service, doctor, branch_id, _, available = context
    slot = available.slots[0]
    row = Appointment(
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=UUID(branch_id),
        doctor_id=UUID(str(doctor["id"])),
        service_id=UUID(str(service["id"])),
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


def appointment_snapshot(
    db: Session,
    workspace: Workspace,
    patient: Patient,
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(Appointment)
            .where(
                Appointment.workspace_id == workspace.id,
                Appointment.patient_id == patient.id,
            )
            .order_by(Appointment.start_at, Appointment.created_at)
        )
    )
    return [
        {
            "id": str(row.id),
            "status": row.status,
            "service_id": str(row.service_id),
            "doctor_id": str(row.doctor_id),
            "branch_id": str(row.branch_id),
            "start_at": row.start_at.isoformat(),
            "billing_context": row.billing_context,
            "payment_status": row.payment_status,
            "amount_paid_minor": row.amount_paid_minor,
            "laser_device_key": row.laser_device_key,
            "laser_device_name": row.laser_device_name,
            "patient_package_id": (
                str(row.patient_package_id) if row.patient_package_id else None
            ),
            "rescheduled_from_appointment_id": (
                str(row.rescheduled_from_appointment_id)
                if row.rescheduled_from_appointment_id
                else None
            ),
        }
        for row in rows
    ]


def package_snapshot(
    db: Session,
    workspace: Workspace,
    patient: Patient,
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(PatientPackage).where(
                PatientPackage.workspace_id == workspace.id,
                PatientPackage.patient_id == patient.id,
            )
        )
    )
    output: list[dict[str, Any]] = []
    for row in rows:
        used = int(
            db.scalar(
                select(func.coalesce(func.sum(PackageUsage.sessions_used), 0)).where(
                    PackageUsage.workspace_id == workspace.id,
                    PackageUsage.patient_package_id == row.id,
                    PackageUsage.status.in_(("reserved", "consumed")),
                )
            )
            or 0
        )
        remaining = (
            None
            if row.opening_sessions_remaining is None
            else max(0, int(row.opening_sessions_remaining) - used)
        )
        output.append(
            {
                "id": str(row.id),
                "service_id": str(row.service_id),
                "name": row.name,
                "status": row.status,
                "remaining": remaining,
            }
        )
    return output


def financial_snapshot(
    db: Session,
    workspace: Workspace,
    patient: Patient,
) -> dict[str, Any]:
    pulse_packs = list(
        db.scalars(
            select(PatientPulsePack)
            .where(
                PatientPulsePack.workspace_id == workspace.id,
                PatientPulsePack.patient_id == patient.id,
            )
            .order_by(PatientPulsePack.purchased_at, PatientPulsePack.id)
        )
    )
    payments = list(
        db.scalars(
            select(PaymentTransaction)
            .where(
                PaymentTransaction.workspace_id == workspace.id,
                PaymentTransaction.patient_id == patient.id,
            )
            .order_by(PaymentTransaction.created_at, PaymentTransaction.id)
        )
    )
    pulse_pack_ids = [row.id for row in pulse_packs]
    usages = (
        list(
            db.scalars(
                select(PulseUsage)
                .where(
                    PulseUsage.workspace_id == workspace.id,
                    PulseUsage.patient_pulse_pack_id.in_(pulse_pack_ids),
                )
                .order_by(PulseUsage.created_at, PulseUsage.id)
            )
        )
        if pulse_pack_ids
        else []
    )
    appointment_ids = list(
        db.scalars(
            select(Appointment.id).where(
                Appointment.workspace_id == workspace.id,
                Appointment.patient_id == patient.id,
            )
        )
    )
    settlements = (
        list(
            db.scalars(
                select(AppointmentPulseSettlement)
                .where(
                    AppointmentPulseSettlement.workspace_id == workspace.id,
                    AppointmentPulseSettlement.appointment_id.in_(appointment_ids),
                )
                .order_by(AppointmentPulseSettlement.created_at, AppointmentPulseSettlement.id)
            )
        )
        if appointment_ids
        else []
    )
    return {
        "pulse_packs": [
            {
                "id": str(row.id),
                "device_key": row.device_key,
                "pulses_purchased": row.pulses_purchased,
                "sale_price_minor": row.sale_price_minor,
                "status": row.status,
                "purchase_transaction_id": (
                    str(row.purchase_transaction_id) if row.purchase_transaction_id else None
                ),
            }
            for row in pulse_packs
        ],
        "pulse_balances": [
            row.model_dump(mode="json")
            for row in list_patient_pulse_balances(
                db,
                workspace_id=workspace.id,
                patient_id=patient.id,
            )
        ],
        "payments": [
            {
                "id": str(row.id),
                "transaction_type": row.transaction_type,
                "amount_minor": row.amount_minor,
                "currency": row.currency,
                "patient_pulse_pack_id": (
                    str(row.patient_pulse_pack_id) if row.patient_pulse_pack_id else None
                ),
                "patient_package_id": (
                    str(row.patient_package_id) if row.patient_package_id else None
                ),
            }
            for row in payments
        ],
        "pulse_usages": [
            {
                "id": str(row.id),
                "patient_pulse_pack_id": str(row.patient_pulse_pack_id),
                "appointment_id": str(row.appointment_id),
                "pulses_used": row.pulses_used,
                "status": row.status,
            }
            for row in usages
        ],
        "pulse_settlements": [
            {
                "id": str(row.id),
                "appointment_id": str(row.appointment_id),
                "pulses_used": row.pulses_used,
                "pulses_from_balance": row.pulses_from_balance,
                "deficit_pulses": row.deficit_pulses,
                "resolution": row.resolution,
                "overage_charge_minor": row.overage_charge_minor,
            }
            for row in settlements
        ],
    }


def state_snapshot(
    db: Session,
    workspace: Workspace,
    patient: Patient,
) -> dict[str, Any]:
    return {
        "appointments": appointment_snapshot(db, workspace, patient),
        "packages": package_snapshot(db, workspace, patient),
        **financial_snapshot(db, workspace, patient),
    }


def acquire_eval_advisory_lock(db: Session, *, namespace: str = "tia-agent-eval") -> None:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:namespace))"),
        {"namespace": namespace},
    )


def action_rows(
    db: Session,
    workspace: Workspace,
    conversation_id: UUID | None,
) -> list[dict[str, Any]]:
    if conversation_id is None:
        return []
    rows = list(
        db.scalars(
            select(AgentAction)
            .where(
                AgentAction.workspace_id == workspace.id,
                AgentAction.conversation_id == conversation_id,
            )
            .order_by(AgentAction.created_at)
        )
    )
    return [
        {
            "tool_name": row.tool_name,
            "action_type": row.action_type,
            "status": row.status,
            "appointment_id": str(row.appointment_id) if row.appointment_id else None,
            "error": row.error_message,
        }
        for row in rows
    ]


def handoff_state(
    db: Session,
    workspace: Workspace,
    conversation_id: UUID | None,
) -> dict[str, Any] | None:
    if conversation_id is None:
        return None
    row = db.scalar(
        select(HandoffRequest)
        .where(
            HandoffRequest.workspace_id == workspace.id,
            HandoffRequest.conversation_id == conversation_id,
            HandoffRequest.status.in_(("pending", "claimed")),
        )
        .order_by(HandoffRequest.created_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {
        "status": row.status,
        "category": row.category,
        "priority": row.priority,
        "source": row.source,
    }


def runtime_summary(
    probe: RuntimeProbe,
) -> tuple[list[str], bool, str | None]:
    reads: list[str] = []
    write_attempted = False
    write_result = None
    for turn in probe.turns:
        for trace in getattr(turn, "traces", ()):
            reads.extend(
                str(value)
                for value in getattr(trace, "read_kinds", ())
                if value
            )
            outcome = getattr(trace, "outcome", None)
            if outcome is not None and getattr(outcome, "action_result", None):
                write_attempted = True
                write_result = str(getattr(outcome, "status", "unknown"))
    return list(dict.fromkeys(reads)), write_attempted, write_result


def send_turn(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    scenario_id: str,
    number: int,
    message: str,
    conversation_id: UUID | None,
):
    before_actions = action_rows(db, workspace, conversation_id)
    with RuntimeProbe() as probe:
        started = perf_counter()
        response = run_agent_chat(
            db=db,
            workspace=workspace,
            payload=AgentChatRequest(
                patient_id=patient.id,
                conversation_id=conversation_id,
                channel="whatsapp",
                message=message,
            ),
        )
        latency_ms = int((perf_counter() - started) * 1000)

    reads, write_attempted, write_result = runtime_summary(probe)
    actions = action_rows(db, workspace, response.conversation_id)
    fresh_actions = (
        actions[len(before_actions):]
        if conversation_id == response.conversation_id
        else actions
    )
    conversation = db.get(Conversation, response.conversation_id)
    capture = TurnCapture(
        scenario_id=scenario_id,
        turn_number=number,
        user_message=message,
        agent_response=response.reply,
        model=response.model,
        latency_ms=latency_ms,
        token_usage=asdict(probe.usage),
        runtime_state={
            "conversation_id": str(response.conversation_id),
            "owner_type": getattr(conversation, "owner_type", None),
            "agent_paused": response.agent_paused,
        },
        verified_reads=reads,
        actions=fresh_actions,
        write_attempted=write_attempted
        or any(
            action["action_type"] not in {"read", "lookup"}
            for action in fresh_actions
        ),
        write_result=write_result,
        handoff_state=handoff_state(
            db,
            workspace,
            response.conversation_id,
        ),
        llm_calls=list(probe.llm_calls),
        structured_trace=[
            {
                "understanding": jsonable(turn.understanding),
                "plan": jsonable(turn.plan),
                "outcomes": jsonable(turn.outcomes),
                "active_task": jsonable(turn.active_task),
                "persisted_task": jsonable(turn.persisted_task),
            }
            for turn in probe.turns
        ],
        context_metrics=(
            dict(probe.interpreter_contexts[-1])
            if probe.interpreter_contexts
            else {}
        ),
    )
    return response, capture


def money(minor: int) -> str:
    return f"{minor / 100:g}"


def local_slot(available, slot) -> tuple[str, str]:
    local = slot.start_at.astimezone(ZoneInfo(available.timezone))
    return local.date().isoformat(), local.strftime("%H:%M")


def service_by_slug(
    db: Session,
    workspace: Workspace,
    slug: str,
) -> Service:
    row = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.slug == slug,
            Service.is_active.is_(True),
        )
    )
    if row is None:
        raise RuntimeError(f"Required demo service missing: {slug}")
    return row


def classify_issue(
    ok: bool,
    *,
    severity: str,
    title: str,
    detail: str,
) -> list[dict[str, str]]:
    return [] if ok else [{"severity": severity, "title": title, "detail": detail}]


def default_evaluation(
    *,
    db_ok: bool = True,
    action_ok: bool | None = None,
    grounding_ok: bool = True,
    continuity_ok: bool = True,
    handoff_ok: bool | None = None,
) -> dict[str, str]:
    return {
        "understanding": (
            "CORRECT" if db_ok and grounding_ok else "PARTIAL"
        ),
        "conversation_quality": "ACCEPTABLE",
        "grounding": "CORRECT" if grounding_ok else "ISSUE",
        "continuity": "CORRECT" if continuity_ok else "ISSUE",
        "action_correctness": (
            "N/A"
            if action_ok is None
            else ("CORRECT" if action_ok else "ISSUE")
        ),
        "db_final_state": (
            "N/A"
            if action_ok is None
            else ("CORRECT" if db_ok else "ISSUE")
        ),
        "handoff_behavior": (
            "N/A"
            if handoff_ok is None
            else ("CORRECT" if handoff_ok else "ISSUE")
        ),
        "token_efficiency": "NORMAL",
    }


def write_reports(
    payload: dict[str, Any],
    json_path: Path,
    md_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# Linka Agent Evaluation — Batch 01",
        "",
        f"- Git SHA: `{payload['run_metadata']['git_sha']}`",
        f"- Workspace: `{payload['run_metadata']['workspace']}`",
        f"- Scenarios: {len(payload['scenario_results'])}",
        "",
    ]
    for row in payload["scenario_results"]:
        lines.extend(
            [
                f"## {row['id']}",
                "",
                f"**Category:** {row['category']}  ",
                f"**Purpose:** {row['purpose']}",
                "",
                "### Conversation transcript",
                "",
            ]
        )
        for turn in row["turns"]:
            lines.append(
                f"**Customer {turn['turn_number']}:** {turn['user_message']}"
            )
            lines.append(f"**Linka:** {turn['agent_response']}")
            lines.append(
                "Tokens: "
                f"{turn['token_usage']['total_tokens']} | "
                f"latency: {turn['latency_ms']} ms | "
                f"reads: {', '.join(turn['verified_reads']) or 'none'}"
            )
            lines.append("")
        lines.extend(
            [
                "### Verification",
                "",
                "```json",
                json.dumps(
                    row["db_verification"],
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "",
                "### Evaluation",
                "",
            ]
        )
        for key, value in row["evaluation"].items():
            lines.append(f"- {key}: **{value}**")
        if row["issues"]:
            lines.append("- Issues:")
            for issue in row["issues"]:
                lines.append(
                    f"  - {issue['severity']}: {issue['title']} — "
                    f"{issue['detail']}"
                )
        else:
            lines.append(
                "- Issues: none detected by deterministic checks; "
                "transcript still requires human quality review."
            )
        lines.append("")

    lines.extend(
        [
            "## Batch token summary",
            "",
            "```json",
            json.dumps(
                payload["batch_summary"]["tokens"],
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
