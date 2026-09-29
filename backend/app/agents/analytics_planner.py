from __future__ import annotations

import json
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm_runtime import invoke_with_model_chain
from app.agents.model_provider import (
    build_realtime_interpreter_emergency_model,
    build_realtime_interpreter_fallback_model,
    build_realtime_interpreter_model,
)
from app.agents.structured_output import invoke_typed_structured_output
from app.core.config import settings
from app.schemas.analytics_bi import AnalyticsBIPlan

_OPERATION_GUIDE = """
Allowed operation meanings:
- clinic_summary: general appointment/patient/payment snapshot.
- revenue_trend: payment/refund net trend; may use service/branch/doctor filters only through explicit payment allocations.
- appointment_outcomes: attendance, no-show, cancellation outcomes.
- service_performance: appointment/completion volume by service.
- service_retention: same-service repeat rate; patient has 2+ completed visits for the same service.
- doctor_performance: appointment/completion outcomes by doctor.
- branch_performance: appointment/completion outcomes by branch.
- top_repeat_patients: patients with the most completed visits.
- top_value_patients: patients ranked by recorded payments minus refunds.
- lapsed_patients: patients whose last completed visit is older than inactivity_days and have no active future appointment.
- new_patients_trend: count new patients using source_created_at when available.
- patient_history_lookup: read one patient's canonical clinic history. Use patient_phone when present; otherwise extract the exact patient_name from the question.
""".strip()


def plan_analytics_question(
    *,
    question: str,
    entity_catalog: dict[str, list[dict[str, str]]],
    timezone_name: str,
    local_now: datetime,
) -> tuple[AnalyticsBIPlan, str | None]:
    system = SystemMessage(
        content=(
            "You are Linka's semantic analytics planner for clinic staff. Return only the structured plan. "
            "You do not answer the question, do not write SQL, do not request raw tables, and do not invent metrics. "
            "The backend will execute one deterministic analytics operation against Linka's canonical clinic data.\n\n"
            "Every field must be present. Use null for unused nullable scalars (including patient_name/patient_phone) and [] for unused entity filters. "
            "Use limit between 1 and 25; default to 10 unless the question clearly asks for fewer/more. "
            "lookback_days is null for all-time/history-wide questions. Resolve relative periods against the supplied "
            "clinic clock. Use inactivity_days only for lapsed_patients; default to 180 when the user says roughly six "
            "months. Currency must be a three-letter code only when explicitly requested or clearly established.\n\n"
            "For patient_history_lookup, patient_phone/patient_name are read-only lookup hints copied only from the staff question. Phone is the strongest lookup signal. Name is allowed only for exact unique read lookup; the backend will reject ambiguous names and never merge identities by name. Do not use patient fields for any aggregate operation.\n\n"
            "Entity grounding is strict. service_ids/branch_ids/doctor_ids may contain only canonical IDs copied verbatim "
            "from the supplied catalog. Never use patient names as identity filters. If the question asks for a specific "
            "service/branch/doctor, ground it semantically to the catalog; if genuinely ambiguous, leave the filter empty "
            "rather than guessing and explain the ambiguity briefly in reason.\n\n"
            "For financial questions with service/branch/doctor filters, the executor uses explicit payment allocations "
            "only and never guesses attribution. Do not reinterpret revenue as appointment list-price.\n\n"
            + _OPERATION_GUIDE
            + f"\n\nClinic timezone: {timezone_name}\nClinic local now: {local_now.isoformat()}"
        )
    )
    human = HumanMessage(
        content=(
            "Canonical analytics entity catalog:\n"
            + json.dumps(entity_catalog, ensure_ascii=False, separators=(",", ":"))
            + "\n\nStaff question:\n"
            + question
        )
    )

    primary_name = settings.gemini_realtime_interpreter_model
    fallback_name = settings.gemini_realtime_interpreter_fallback_model
    emergency_name = settings.gemini_realtime_interpreter_emergency_model
    primary_model = build_realtime_interpreter_model()

    def invoke_primary() -> AnalyticsBIPlan:
        return invoke_typed_structured_output(
            model=primary_model,
            schema=AnalyticsBIPlan,
            messages=[system, human],
        )

    def invoke_fallback() -> AnalyticsBIPlan:
        fallback_model = build_realtime_interpreter_fallback_model()
        if fallback_model is None:
            raise RuntimeError("Analytics planner fallback model is not configured.")
        return invoke_typed_structured_output(
            model=fallback_model,
            schema=AnalyticsBIPlan,
            messages=[system, human],
        )

    def invoke_emergency() -> AnalyticsBIPlan:
        emergency_model = build_realtime_interpreter_emergency_model()
        if emergency_model is None:
            raise RuntimeError("Analytics planner emergency model is not configured.")
        return invoke_typed_structured_output(
            model=emergency_model,
            schema=AnalyticsBIPlan,
            messages=[system, human],
        )

    model_calls = [(primary_name, invoke_primary)]
    if fallback_name and fallback_name != primary_name:
        model_calls.append((fallback_name, invoke_fallback))
    if emergency_name and emergency_name not in {primary_name, fallback_name}:
        model_calls.append((emergency_name, invoke_emergency))

    invocation = invoke_with_model_chain(
        model_calls=model_calls,
        operation="staff-analytics-planner",
        circuit_breaker_cooldown_seconds=settings.llm_realtime_circuit_breaker_cooldown_seconds,
    )
    return invocation.value, invocation.model_name
