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
from app.schemas.analytics_composable import AnalyticsComposePlan

_COMPOSABLE_GUIDE = """
You have three read-only analysis modes.

business:
Use AnalyticsBusinessPlan for aggregate business questions, trends, comparisons, funnels, retention, and performance/ranking by service, branch, doctor, or time. Prefer this mode for almost every aggregate question.
- metrics may combine up to 6 allowed measures in one request.
- group_by may contain up to two dimensions. Typical combinations: ["service"], ["branch"], ["doctor"], ["source"], ["month"], ["service","month"], ["branch","month"].
- lookback_days handles relative periods. For explicit calendar ranges use start_date/end_date in YYYY-MM-DD and leave lookback_days null. Leave all period fields null only for all-time.
- comparison="previous_period" means the immediately preceding period of exactly the same length. Do not combine it with day/week/month groupings; use a time grouping instead when the user wants a trend.
- Money metrics require one explicit currency. Arabic phrases meaning Egyptian pounds/جنيه may map to EGP. Never guess FX or combine currencies.
- Revenue scoped/grouped by service/branch/doctor/source uses explicit payment allocations only.
- Funnel questions such as "من الحجز للحضور والدفع" should request appointments, completed_appointments, paid_completed_appointments, completion_rate, paid_completion_rate and/or booking_to_paid_rate.
- Retention questions should use repeat_patients/repeat_rate or same_service_repeat_rate. same_service_repeat_rate requires grouping by service or exactly one service filter.
- new_patients may be grouped by day/week/month only and cannot use appointment entity filters.
- sort_metric must be one of metrics when ranking rows. Use limit 10 normally, up to 25 only when useful.

metric:
Use the legacy AnalyticsBIPlan only for patient_history_lookup. Aggregate/trend/performance questions belong in business mode.

patient audience:
Use an audience plan when the answer should be a list/group of patients. The user does NOT need to say cohort, segment, audience, or any technical term. Natural phrases such as "هاتلي", "جمعلي الناس", "مين اللي", "طلعلي العملاء", "الناس اللي", "customers who" all mean a patient-list request when the semantics require people.

Audience field meanings:
- lookback_days: matching appointments must be within this many days. Example: "عملوا ليزر آخر 6 شهور" -> 180.
- inactivity_days: the latest matching appointment must be at least this old. Example: "بقالهم 5 شهور معملوش ليزر" -> 150.
- service_ids/branch_ids/doctor_ids: canonical entity filters copied verbatim from the catalog. When a user's service wording is intentionally broad and clearly covers multiple catalog services in the same family (for example a generic laser request while the catalog has several laser areas/types), include every clearly matching service ID instead of choosing one arbitrary service. If the term could refer to genuinely different service families, do not guess.
- appointment_statuses: default to ["completed"] for phrases meaning "عملوا/زاروا/أخذوا جلسة". Use other statuses only when explicitly asked.
- min_matching_visits/max_matching_visits: visit-count conditions within the matching appointment set.
- has_future_appointment: set false only when the user explicitly wants people with no upcoming active booking; true only when explicitly requested.
- marketing_consent: use only when the user explicitly filters on marketing permission.
- patient_statuses: default ["active","inactive"]; include blocked only if explicitly requested for analysis.
- min_net_paid_minor/max_net_paid_minor: deterministic patient value condition in integer minor units. Currency is required. For EGP, 5000 pounds = 500000 minor units. Do not invent a currency, guess FX, or mix currencies.
- sort_by: use last_activity_desc normally, matching_visits_desc for repeat/frequency ranking, net_paid_desc for value ranking.

Actions are proposals only and never execute inside this planner:
- save_audience: phrases like "جمعهم", "احفظ الناس دي", "اعمل ليست للناس دي".
- follow_up_tasks: phrases like "اعمل follow up", "خلي الفريق يكلمهم", "اعمل مهام متابعة".
- whatsapp_campaign: phrases like "جهز حملة", "نبعتلهم واتساب", "جهز outreach". This only proposes the next step; template/channel selection and explicit confirmation happen later.
- none: analysis only.

When the new message refers to "الناس دي / دول / المجموعة دي / them" and previous_audience_plan exists, set mode=audience and reuse_previous_audience=true instead of trying to reconstruct the audience. If there is no previous audience, never pretend a referent exists.
""".strip()


def plan_composable_analytics(
    *,
    message: str,
    entity_catalog: dict[str, list[dict[str, str]]],
    timezone_name: str,
    local_now: datetime,
    previous_question: str | None = None,
    previous_audience_plan: dict | None = None,
    previous_business_plan: dict | None = None,
) -> tuple[AnalyticsComposePlan, str | None]:
    system = SystemMessage(
        content=(
            "You are Linka's semantic analytics and action-intent planner for clinic staff. "
            "Return only the typed plan. You never execute writes, never write SQL, never inspect raw tables, "
            "and never invent patient identities or financial attribution. The backend performs all reads and any later confirmed writes.\n\n"
            "All output fields must be present. Use null for unused nullable fields and [] for unused collections. "
            "For business mode, use AnalyticsBusinessPlan. Use metric mode only for patient_history_lookup. For audience mode, compose only the bounded patient-audience fields provided by the schema.\n\n"
            "Entity grounding is strict: canonical service/branch/doctor IDs must be copied verbatim from the supplied catalog. "
            "If an entity is genuinely ambiguous, do not guess.\n\n"
            "The planner sees no patient rows or payment rows. Previous audience/business plans, when supplied, contain only typed filters/metrics and canonical IDs. Use a previous business plan as context for follow-up modifiers like changing grouping, period, filters, metrics, or comparison; never pretend a previous plan exists when it is null. Reuse previous audience only through reuse_previous_audience.\n\n"
            + _COMPOSABLE_GUIDE
            + f"\n\nClinic timezone: {timezone_name}\nClinic local now: {local_now.isoformat()}"
        )
    )
    context = {
        "entity_catalog": entity_catalog,
        "previous_question": previous_question,
        "previous_audience_plan": previous_audience_plan,
        "previous_business_plan": previous_business_plan,
        "staff_message": message,
    }
    human = HumanMessage(content=json.dumps(context, ensure_ascii=False, separators=(",", ":")))

    primary_name = settings.gemini_realtime_interpreter_model
    fallback_name = settings.gemini_realtime_interpreter_fallback_model
    emergency_name = settings.gemini_realtime_interpreter_emergency_model
    primary_model = build_realtime_interpreter_model()

    def invoke_primary() -> AnalyticsComposePlan:
        return invoke_typed_structured_output(
            model=primary_model,
            schema=AnalyticsComposePlan,
            messages=[system, human],
        )

    def invoke_fallback() -> AnalyticsComposePlan:
        fallback_model = build_realtime_interpreter_fallback_model()
        if fallback_model is None:
            raise RuntimeError("Analytics orchestrator fallback model is not configured.")
        return invoke_typed_structured_output(
            model=fallback_model,
            schema=AnalyticsComposePlan,
            messages=[system, human],
        )

    def invoke_emergency() -> AnalyticsComposePlan:
        emergency_model = build_realtime_interpreter_emergency_model()
        if emergency_model is None:
            raise RuntimeError("Analytics orchestrator emergency model is not configured.")
        return invoke_typed_structured_output(
            model=emergency_model,
            schema=AnalyticsComposePlan,
            messages=[system, human],
        )

    model_calls = [(primary_name, invoke_primary)]
    if fallback_name and fallback_name != primary_name:
        model_calls.append((fallback_name, invoke_fallback))
    if emergency_name and emergency_name not in {primary_name, fallback_name}:
        model_calls.append((emergency_name, invoke_emergency))

    invocation = invoke_with_model_chain(
        model_calls=model_calls,
        operation="staff-composable-analytics-planner",
        circuit_breaker_cooldown_seconds=settings.llm_realtime_circuit_breaker_cooldown_seconds,
    )
    return invocation.value, invocation.model_name
