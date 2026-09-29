from __future__ import annotations

import json
import logging
from collections import defaultdict

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm_runtime import LLMProviderError, is_cross_model_failover_eligible
from app.agents.model_provider import (
    build_onboarding_fallback_model,
    build_onboarding_model,
)
from app.agents.structured_output import invoke_typed_structured_output
from app.schemas.onboarding_ai import (
    OnboardingBookingSettingsPlan,
    OnboardingBranchPlan,
    OnboardingDoctorPlan,
    OnboardingDoctorWorkingHours,
    OnboardingPlan,
    OnboardingServicePlan,
    OnboardingTurnDecision,
    OnboardingWorkingHour,
)
from app.schemas.onboarding_provider import OnboardingProviderDecision

logger = logging.getLogger(__name__)


def _provider_to_domain(
    decision: OnboardingProviderDecision,
) -> OnboardingTurnDecision:
    branch_hours: dict[str, list[OnboardingWorkingHour]] = defaultdict(list)
    for row in decision.branch_hours:
        for weekday in dict.fromkeys(row.weekdays):
            branch_hours[row.branch_key].append(
                OnboardingWorkingHour(
                    weekday=weekday,
                    start_time=row.start_time,
                    end_time=row.end_time,
                )
            )

    branches = [
        OnboardingBranchPlan(
            key=row.key,
            name=row.name,
            code=row.code,
            city=row.city,
            phone=row.phone,
            email=row.email,
            address_line1=row.address_line1,
            country_code=row.country_code,
            timezone=row.timezone,
            apply_working_hours=bool(branch_hours.get(row.key)),
            working_hours=branch_hours.get(row.key, []),
        )
        for row in decision.branches
    ]

    services = [
        OnboardingServicePlan(
            key=row.key,
            name=row.name,
            slug=row.slug,
            category=row.category,
            description=row.description,
            duration_minutes=row.duration_minutes,
            buffer_before_minutes=row.buffer_before_minutes,
            buffer_after_minutes=row.buffer_after_minutes,
            price_minor=row.price_minor,
            currency=row.currency,
            requires_medical_review=row.requires_medical_review,
        )
        for row in decision.services
    ]

    doctor_branch_rows: dict[str, list] = defaultdict(list)
    for row in decision.doctor_branches:
        doctor_branch_rows[row.doctor_key].append(row)

    doctor_service_rows: dict[str, list[str]] = defaultdict(list)
    for row in decision.doctor_services:
        doctor_service_rows[row.doctor_key].append(row.service_key)

    doctor_hour_rows: dict[
        tuple[str, str],
        list[OnboardingWorkingHour],
    ] = defaultdict(list)
    for row in decision.doctor_hours:
        for weekday in dict.fromkeys(row.weekdays):
            doctor_hour_rows[(row.doctor_key, row.branch_key)].append(
                OnboardingWorkingHour(
                    weekday=weekday,
                    start_time=row.start_time,
                    end_time=row.end_time,
                )
            )

    doctors: list[OnboardingDoctorPlan] = []
    for row in decision.doctors:
        assignments = doctor_branch_rows.get(row.key, [])
        branch_keys = list(dict.fromkeys(item.branch_key for item in assignments))
        primary_keys = [item.branch_key for item in assignments if item.is_primary]
        primary_branch_key = primary_keys[0] if primary_keys else None

        schedule_groups = [
            OnboardingDoctorWorkingHours(
                branch_key=branch_key,
                intervals=doctor_hour_rows[(row.key, branch_key)],
            )
            for branch_key in branch_keys
            if doctor_hour_rows.get((row.key, branch_key))
        ]

        doctors.append(
            OnboardingDoctorPlan(
                key=row.key,
                first_name=row.first_name,
                last_name=row.last_name,
                specialization=row.specialization,
                license_number=row.license_number,
                phone=row.phone,
                email=row.email,
                branch_keys=branch_keys,
                primary_branch_key=primary_branch_key,
                service_keys=list(dict.fromkeys(doctor_service_rows.get(row.key, []))),
                apply_working_hours=bool(schedule_groups),
                working_hours=schedule_groups,
            )
        )

    provider_settings = decision.booking_settings
    settings = OnboardingBookingSettingsPlan(
        apply=provider_settings.apply,
        slot_interval_minutes=provider_settings.slot_interval_minutes,
        minimum_notice_minutes=provider_settings.minimum_notice_minutes,
        booking_horizon_days=provider_settings.booking_horizon_days,
        cancellation_notice_minutes=provider_settings.cancellation_notice_minutes,
        allow_same_day_booking=provider_settings.allow_same_day_booking,
        require_confirmation=provider_settings.require_confirmation,
        default_currency=provider_settings.default_currency,
    )

    return OnboardingTurnDecision(
        action=decision.action,
        capabilities=decision.capabilities,
        plan=OnboardingPlan(
            branches=branches,
            services=services,
            doctors=doctors,
            booking_settings=settings,
        ),
        missing_information=decision.missing_information,
        assistant_message=decision.assistant_message,
        confidence=decision.confidence,
    )


def _invoke_onboarding_provider_decision(
    *,
    messages: list,
) -> OnboardingProviderDecision:
    """
    Keep Gemini 3.7 Flash as the primary onboarding model.

    ChatGoogleGenerativeAI already performs its configured client retries. Only
    after those retries are exhausted do we fail over, and only for provider
    5xx/capacity errors. Client/schema errors (4xx) and quota errors (429) are
    surfaced instead of being hidden by a model switch.
    """
    try:
        return invoke_typed_structured_output(
            model=build_onboarding_model(),
            schema=OnboardingProviderDecision,
            messages=messages,
        )
    except LLMProviderError as exc:
        if not is_cross_model_failover_eligible(exc):
            raise

        fallback = build_onboarding_fallback_model()
        if fallback is None:
            raise

        logger.warning(
            "Gemini onboarding primary unavailable after retries "
            "(status=%s); using configured fallback model.",
            exc.status_code,
        )
        return invoke_typed_structured_output(
            model=fallback,
            schema=OnboardingProviderDecision,
            messages=messages,
        )


def plan_onboarding_turn(
    *,
    message: str,
    current_setup: dict,
    stored_plan: dict,
    recent_history: list[dict],
) -> OnboardingTurnDecision:
    system = SystemMessage(
        content=(
            "You are Linka's AI-assisted clinic onboarding planner. "
            "You plan configuration; you never execute writes and never expose "
            "internal database IDs. Return only the required structured output.\n\n"
            "Understand Arabic or English semantically. Do not use keyword "
            "routing. Extract only configuration supported by these capabilities: "
            "branches, services, doctors, schedules and booking settings.\n\n"
            "Provider output is deliberately relational and flat:\n"
            "- branches/services/doctors contain entities.\n"
            "- branch_hours contains compact weekly schedule rules.\n"
            "- doctor_branches links doctor_key to branch_key.\n"
            "- doctor_services links doctor_key to service_key.\n"
            "- doctor_hours contains compact doctor/branch weekly rules.\n"
            "Every referenced key must exist in entities returned in this same "
            "response, including existing entities that need linking/updating.\n\n"
            "Time rules: weekdays use Monday=0 through Sunday=6. For 'every "
            "day', emit ONE schedule row with weekdays [0,1,2,3,4,5,6], not "
            "seven repeated rows. Reuse a single row for any days sharing the "
            "same start/end time. Use HH:MM:SS time strings. When an admin clearly "
            "states clinic hours and clearly says doctors are available in those "
            "branches for the same schedule, mirror those hours into doctor_hours "
            "unless a different doctor schedule is stated. Otherwise ask for the "
            "missing doctor schedule rather than inventing it.\n\n"
            "Money rules: price_minor uses minor units; 1500 EGP = 150000. "
            "For an Egyptian clinic use EGP, EG and Africa/Cairo unless the admin "
            "provides different values.\n\n"
            "Use simple lowercase ASCII slug/code identifiers with hyphens. "
            "Entity keys are local plan references and should also be short ASCII "
            "hyphen identifiers.\n\n"
            "Confirmation rules:\n"
            "- propose/revise while forming a plan.\n"
            "- clarify only for genuinely required missing information.\n"
            "- confirm ONLY when the newest admin message explicitly approves the "
            "already stored plan. Never infer confirmation from silence.\n"
            "- cancel when the newest message clearly cancels the plan.\n"
            "- do not propose destructive deletions in this version.\n\n"
            "The application performs strict Pydantic, relationship and database "
            "validation after your structured response."
        )
    )
    user = HumanMessage(
        content=(
            "CURRENT_SETUP_JSON:\n"
            f"{json.dumps(current_setup, ensure_ascii=False)}\n\n"
            "STORED_PLAN_JSON:\n"
            f"{json.dumps(stored_plan, ensure_ascii=False)}\n\n"
            "RECENT_HISTORY_JSON:\n"
            f"{json.dumps(recent_history, ensure_ascii=False)}\n\n"
            "NEW_ADMIN_MESSAGE:\n"
            f"{message}"
        )
    )

    provider_decision = _invoke_onboarding_provider_decision(
        messages=[system, user],
    )
    return _provider_to_domain(provider_decision)


def empty_onboarding_plan() -> OnboardingPlan:
    return OnboardingPlan()
