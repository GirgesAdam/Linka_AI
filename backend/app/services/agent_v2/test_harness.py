from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langchain_core.messages import BaseMessage

from app.agents.v2.responder import compose_v2_customer_reply
from app.agents.v2.semantic_context import SemanticContext, build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_task_context
from app.agents.v2.turn_contract import TiaTurnUnderstanding
from app.agents.v2.turn_interpreter import interpret_customer_turn_v2
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import build_handoff_outcome, build_step_outcome
from app.services.agent_v2.planner import (
    PlannerContext,
    PlanStep,
    TurnPlan,
    VerificationFacts,
    advance_step_after_verification,
    plan_turn,
)
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import ActiveTaskState

DEFAULT_CATALOG: dict[str, object] = {
    "services": [
        {
            "id": "svc-underarm",
            "name": "ليزر إبط",
            "category": "laser",
            "description": "جلسة إزالة شعر لمنطقة الإبط.",
            "requires_laser_device": True,
            "price_minor": 50_000,
            "currency": "EGP",
            "customer_duration_text": "حوالي 15 دقيقة",
            "laser_devices": [
                {"device_key": "candela_gentle", "device_name": "Candela Gentle"},
                {"device_key": "prime_lase", "device_name": "Prime Lase"},
            ],
        },
        {
            "id": "svc-bikini",
            "name": "ليزر بكيني",
            "category": "laser",
            "description": "جلسة إزالة شعر لمنطقة البكيني.",
            "requires_laser_device": True,
            "price_minor": 70_000,
            "currency": "EGP",
            "customer_duration_text": "حوالي 20 دقيقة",
            "laser_devices": [
                {"device_key": "candela_gentle", "device_name": "Candela Gentle"}
            ],
        },
        {
            "id": "svc-hydrafacial",
            "name": "هيدرافيشل",
            "category": "facial",
            "description": "جلسة تنظيف وترطيب للبشرة.",
            "requires_laser_device": False,
            "price_minor": 120_000,
            "currency": "EGP",
            "customer_duration_text": "حوالي 45 دقيقة",
        },
    ],
    "doctors": [
        {
            "id": "doc-maryam",
            "name": "د. مريم",
            "specialization": "Dermatology",
            "service_ids": ["svc-underarm", "svc-bikini", "svc-hydrafacial"],
        },
        {
            "id": "doc-sarah",
            "name": "د. سارة",
            "specialization": "Dermatology",
            "service_ids": ["svc-underarm", "svc-hydrafacial"],
        },
    ],
    "appointments": [
        {
            "appointment_id": "apt-underarm-sat",
            "service_id": "svc-underarm",
            "service_name": "ليزر إبط",
            "doctor_id": "doc-maryam",
            "doctor_name": "د. مريم",
            "status": "confirmed",
            "start_local": "2026-09-12T19:00:00+03:00",
            "laser_device_key": "candela_gentle",
            "laser_device_name": "Candela Gentle",
        },
        {
            "appointment_id": "apt-hydra-thu",
            "service_id": "svc-hydrafacial",
            "service_name": "هيدرافيشل",
            "doctor_id": "doc-sarah",
            "doctor_name": "د. سارة",
            "status": "pending",
            "start_local": "2026-09-17T18:00:00+03:00",
        },
    ],
    "packages": [
        {
            "id": "pkg-underarm-candela",
            "name": "باكيدج ليزر إبط - كانديلا",
            "service_id": "svc-underarm",
            "laser_device_key": "candela_gentle",
            "remaining_sessions": 4,
            "total_sessions": 6,
            "status": "active",
        }
    ],
}

DEFAULT_CLINIC_INFO = {
    "name": "Linka Test Clinic",
    "phone": "01000000000",
    "working_hours": "يوميًا من 10 صباحًا إلى 10 مساءً",
}

DEFAULT_PROFILE = {
    "first_name": "سارة",
    "last_name": "أحمد",
    "phone": "01012345678",
    "preferred_language": "ar",
    "status": "active",
}

DEFAULT_HISTORY = {
    "recent_visits": [
        {
            "service_name": "ليزر إبط",
            "doctor_name": "د. مريم",
            "date": "2026-08-29",
            "paid": "500.00 EGP",
        }
    ],
    "last_payment": "500.00 EGP",
}

DEFAULT_PACKAGE_OFFERS = [
    {
        "id": "offer-underarm-6-candela",
        "name": "6 جلسات ليزر إبط - كانديلا",
        "service_id": "svc-underarm",
        "device_key": "candela_gentle",
        "sessions_count": 6,
        "price_minor": 250_000,
        "currency": "EGP",
    }
]

DEFAULT_REFUND_QUOTES = [
    {
        "package_id": "pkg-underarm-candela",
        "package_name": "باكيدج ليزر إبط - كانديلا",
        "refundable_minor": 180_000,
        "currency": "EGP",
        "sessions_consumed": 2,
        "standalone_session_price_minor": 50_000,
    }
]

DEFAULT_SLOTS = [
    {
        "branch_id": "single-location",
        "service_id": "svc-underarm",
        "doctor_id": "doc-maryam",
        "doctor_name": "د. مريم",
        "laser_device_key": "candela_gentle",
        "laser_device_name": "Candela Gentle",
        "start_local": "2026-09-12T18:00:00+03:00",
        "end_local": "2026-09-12T18:30:00+03:00",
        "start_at": "2026-09-12T18:00:00+03:00",
        "price_minor": 50_000,
        "currency": "EGP",
    },
    {
        "branch_id": "single-location",
        "service_id": "svc-underarm",
        "doctor_id": "doc-maryam",
        "doctor_name": "د. مريم",
        "laser_device_key": "candela_gentle",
        "laser_device_name": "Candela Gentle",
        "start_local": "2026-09-12T19:00:00+03:00",
        "end_local": "2026-09-12T19:30:00+03:00",
        "start_at": "2026-09-12T19:00:00+03:00",
        "price_minor": 50_000,
        "currency": "EGP",
    },
    {
        "branch_id": "single-location",
        "service_id": "svc-underarm",
        "doctor_id": "doc-sarah",
        "doctor_name": "د. سارة",
        "laser_device_key": "prime_lase",
        "laser_device_name": "Prime Lase",
        "start_local": "2026-09-12T19:00:00+03:00",
        "end_local": "2026-09-12T19:30:00+03:00",
        "start_at": "2026-09-12T19:00:00+03:00",
        "price_minor": 50_000,
        "currency": "EGP",
    },
    {
        "branch_id": "single-location",
        "service_id": "svc-underarm",
        "doctor_id": "doc-maryam",
        "doctor_name": "د. مريم",
        "laser_device_key": "candela_gentle",
        "laser_device_name": "Candela Gentle",
        "start_local": "2026-09-12T20:00:00+03:00",
        "end_local": "2026-09-12T20:30:00+03:00",
        "start_at": "2026-09-12T20:00:00+03:00",
        "price_minor": 50_000,
        "currency": "EGP",
    },
    {
        "branch_id": "single-location",
        "service_id": "svc-bikini",
        "doctor_id": "doc-maryam",
        "doctor_name": "د. مريم",
        "laser_device_key": "candela_gentle",
        "laser_device_name": "Candela Gentle",
        "start_local": "2026-09-12T20:00:00+03:00",
        "end_local": "2026-09-12T20:30:00+03:00",
        "start_at": "2026-09-12T20:00:00+03:00",
        "price_minor": 70_000,
        "currency": "EGP",
    },
]


@dataclass(frozen=True)
class V2FixtureEnvironment:
    catalog: dict[str, object] = field(default_factory=lambda: dict(DEFAULT_CATALOG))
    clinic_info: dict[str, object] = field(default_factory=lambda: dict(DEFAULT_CLINIC_INFO))
    profile: dict[str, object] = field(default_factory=lambda: dict(DEFAULT_PROFILE))
    history: dict[str, object] = field(default_factory=lambda: dict(DEFAULT_HISTORY))
    slots: list[dict[str, object]] = field(
        default_factory=lambda: [dict(row) for row in DEFAULT_SLOTS]
    )
    package_offers: list[dict[str, object]] = field(
        default_factory=lambda: [dict(row) for row in DEFAULT_PACKAGE_OFFERS]
    )
    refund_quotes: list[dict[str, object]] = field(
        default_factory=lambda: [dict(row) for row in DEFAULT_REFUND_QUOTES]
    )


@dataclass(frozen=True)
class V2HarnessStepTrace:
    operation_index: int
    operation_type: str
    disposition_before: str
    disposition_after: str
    read_kinds: tuple[str, ...]
    simulated_write: str | None
    outcome: TurnOutcome


@dataclass(frozen=True)
class V2HarnessResult:
    understanding: TiaTurnUnderstanding
    plan: TurnPlan
    traces: tuple[V2HarnessStepTrace, ...]
    outcomes: tuple[TurnOutcome, ...]
    reply: str
    responder_model: str


def _rows(catalog: dict[str, object], key: str) -> list[dict[str, object]]:
    value = catalog.get(key)
    return [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _matches_date(local_value: object, constraint: object) -> bool:
    if not isinstance(constraint, dict):
        return True
    date_value = str(local_value or "")[:10]
    mode = constraint.get("mode")
    start = str(constraint.get("start_date") or "")
    end = str(constraint.get("end_date") or "")
    if mode == "exact":
        return not start or date_value == start
    if mode == "range":
        return (not start or date_value >= start) and (not end or date_value <= end)
    if mode == "from_date":
        return not start or date_value >= start
    return True


def _matches_time(local_value: object, constraint: object) -> bool:
    if not isinstance(constraint, dict):
        return True
    clock = str(local_value or "")[11:16]
    mode = constraint.get("mode")
    start = str(constraint.get("start_time") or "")[:5]
    end = str(constraint.get("end_time") or "")[:5]
    if mode == "exact":
        return not start or clock == start
    if mode == "after":
        return not start or clock >= start
    if mode == "before":
        return not start or clock <= start
    if mode == "range":
        return (not start or clock >= start) and (not end or clock <= end)
    return True


def _service_payload(env: V2FixtureEnvironment, service_id: object) -> dict[str, object]:
    row = next((item for item in _rows(env.catalog, "services") if item.get("id") == service_id), None)
    if row is None:
        return {"services": []}
    return {"service": row}


def _doctor_payload(env: V2FixtureEnvironment, params: dict[str, object]) -> dict[str, object]:
    rows = _rows(env.catalog, "doctors")
    doctor_id = params.get("doctor_id")
    service_id = params.get("service_id")
    if doctor_id:
        rows = [row for row in rows if row.get("id") == doctor_id]
    if service_id:
        rows = [row for row in rows if service_id in list(row.get("service_ids") or [])]
    return {"doctors": rows}


def _availability_bundle(
    env: V2FixtureEnvironment,
    params: dict[str, object],
) -> tuple[ReadResult, VerificationFacts]:
    rows = [dict(row) for row in env.slots]
    service_id = params.get("service_id")
    doctor_id = params.get("doctor_id")
    device_key = params.get("device_key")
    if service_id:
        rows = [row for row in rows if row.get("service_id") == service_id]
    if doctor_id:
        rows = [row for row in rows if row.get("doctor_id") == doctor_id]
    if device_key:
        rows = [row for row in rows if row.get("laser_device_key") == device_key]
    rows = [row for row in rows if _matches_date(row.get("start_local"), params.get("date"))]
    rows = [row for row in rows if _matches_time(row.get("start_local"), params.get("time"))]

    time_constraint = params.get("time")
    exact = isinstance(time_constraint, dict) and time_constraint.get("mode") == "exact"
    verified: dict[str, object] = {}
    if exact and len(rows) == 1:
        row = rows[0]
        verified = {
            "branch_id": row.get("branch_id"),
            "service_id": row.get("service_id"),
            "doctor_id": row.get("doctor_id"),
            "device_key": row.get("laser_device_key"),
            "start_at": row.get("start_at"),
        }
    service = next(
        (item for item in _rows(env.catalog, "services") if item.get("id") == service_id),
        {},
    )
    return (
        ReadResult(
            kind="availability",
            ok=True,
            payload={
                "service_id": service_id,
                "service_name": service.get("name"),
                "checked_dates": sorted({str(row.get("start_local"))[:10] for row in rows}),
                "slots": rows,
                "matching_slot_count": len(rows),
                "search_truncated": False,
            },
        ),
        VerificationFacts(
            exact_slot_match_count=len(rows) if exact else None,
            verified_parameters=verified,
        ),
    )


def _appointment_bundle(
    env: V2FixtureEnvironment,
    params: dict[str, object],
) -> tuple[ReadResult, VerificationFacts]:
    rows = _rows(env.catalog, "appointments")
    if params.get("appointment_id"):
        rows = [row for row in rows if row.get("appointment_id") == params["appointment_id"]]
    if params.get("service_id"):
        rows = [row for row in rows if row.get("service_id") == params["service_id"]]
    if params.get("doctor_id"):
        rows = [row for row in rows if row.get("doctor_id") == params["doctor_id"]]
    rows = [row for row in rows if _matches_date(row.get("start_local"), params.get("date"))]
    verified: dict[str, object] = {}
    if len(rows) == 1:
        row = rows[0]
        verified = {
            "appointment_id": row.get("appointment_id"),
            "service_id": row.get("service_id"),
            "doctor_id": row.get("doctor_id"),
        }
    return (
        ReadResult(kind="appointments", ok=True, payload={"appointments": rows}),
        VerificationFacts(
            appointment_match_count=len(rows),
            verified_parameters=verified,
        ),
    )


def _package_rows(env: V2FixtureEnvironment, params: dict[str, object]) -> list[dict[str, object]]:
    rows = _rows(env.catalog, "packages")
    if params.get("package_id"):
        rows = [row for row in rows if row.get("id") == params["package_id"]]
    if params.get("service_id"):
        rows = [row for row in rows if row.get("service_id") == params["service_id"]]
    if params.get("device_key"):
        rows = [row for row in rows if row.get("laser_device_key") == params["device_key"]]
    return rows


def _offer_bundle(
    env: V2FixtureEnvironment,
    params: dict[str, object],
) -> tuple[ReadResult, VerificationFacts]:
    rows = [dict(row) for row in env.package_offers]
    if params.get("service_id"):
        rows = [row for row in rows if row.get("service_id") == params["service_id"]]
    if params.get("device_key"):
        rows = [row for row in rows if row.get("device_key") == params["device_key"]]
    if params.get("package_sessions") is not None:
        rows = [row for row in rows if row.get("sessions_count") == params["package_sessions"]]
    verified: dict[str, object] = {}
    if len(rows) == 1:
        row = rows[0]
        verified = {
            "package_offer_id": row.get("id"),
            "service_id": row.get("service_id"),
            "device_key": row.get("device_key"),
            "package_sessions": row.get("sessions_count"),
            "price_minor": row.get("price_minor"),
            "currency": row.get("currency"),
        }
    return (
        ReadResult(kind="package_offers", ok=True, payload={"offers": rows}),
        VerificationFacts(
            package_offer_match_count=len(rows),
            verified_parameters=verified,
        ),
    )


def execute_fixture_reads(step: PlanStep, env: V2FixtureEnvironment) -> ReadExecutionBundle:
    results: list[ReadResult] = []
    verification = VerificationFacts()
    appointment_verification = VerificationFacts()

    for request in step.reads:
        params = dict(request.parameters)
        if request.kind == "service_catalog":
            results.append(
                ReadResult(
                    kind=request.kind,
                    ok=True,
                    payload=_service_payload(env, params.get("service_id")),
                )
            )
        elif request.kind == "clinic_info":
            results.append(ReadResult(kind=request.kind, ok=True, payload=dict(env.clinic_info)))
        elif request.kind == "doctors":
            results.append(
                ReadResult(kind=request.kind, ok=True, payload=_doctor_payload(env, params))
            )
        elif request.kind == "appointments":
            result, appointment_verification = _appointment_bundle(env, params)
            results.append(result)
            verification = appointment_verification
        elif request.kind == "availability":
            inherited = dict(params)
            if step.operation_type == "reschedule" and appointment_verification.verified_parameters:
                inherited = {**params, **appointment_verification.verified_parameters}
            result, slot_verification = _availability_bundle(env, inherited)
            results.append(result)
            verification = VerificationFacts(
                appointment_match_count=appointment_verification.appointment_match_count,
                exact_slot_match_count=slot_verification.exact_slot_match_count,
                verified_parameters={
                    **appointment_verification.verified_parameters,
                    **slot_verification.verified_parameters,
                },
            )
        elif request.kind == "customer_profile":
            raw_details = params.get("requested_patient_details")
            details = (
                [str(item) for item in raw_details]
                if isinstance(raw_details, list) and raw_details
                else ["name", "phone", "preferred_language"]
            )
            profile: dict[str, object] = {}
            if "name" in details:
                profile["first_name"] = env.profile.get("first_name")
                profile["last_name"] = env.profile.get("last_name")
            if "phone" in details:
                profile["phone"] = env.profile.get("phone")
            if "preferred_language" in details:
                profile["preferred_language"] = env.profile.get("preferred_language")
            results.append(
                ReadResult(
                    kind=request.kind,
                    ok=True,
                    payload={
                        "patient": profile,
                        "requested_details": details,
                    },
                )
            )
        elif request.kind == "customer_history":
            results.append(
                ReadResult(kind=request.kind, ok=True, payload={"history": dict(env.history)})
            )
        elif request.kind == "customer_packages":
            results.append(
                ReadResult(
                    kind=request.kind,
                    ok=True,
                    payload={"packages": _package_rows(env, params)},
                )
            )
        elif request.kind == "package_offers":
            result, verification = _offer_bundle(env, params)
            results.append(result)
        elif request.kind == "package_refund_quote":
            rows = [dict(row) for row in env.refund_quotes]
            if params.get("package_id"):
                rows = [row for row in rows if row.get("package_id") == params["package_id"]]
            results.append(ReadResult(kind=request.kind, ok=True, payload={"quotes": rows}))
        else:
            raise ValueError(f"Unsupported V2 fixture read kind: {request.kind}")

    return ReadExecutionBundle(results=results, verification=verification)


def _task_dict(active_task: ActiveTaskState | None) -> dict[str, Any] | None:
    return active_task.model_dump(mode="json") if active_task is not None else None


def run_v2_fixture_turn(
    *,
    history: list[BaseMessage],
    local_now: datetime,
    timezone_name: str = "Africa/Cairo",
    clinic_name: str = "Linka Test Clinic",
    env: V2FixtureEnvironment | None = None,
    active_task: ActiveTaskState | None = None,
    simulate_writes: bool = True,
) -> V2HarnessResult:
    """Run only Agent Core V2 against deterministic test-clinic fixtures.

    No V1 code, database session, clinic adapter, production state, or customer delivery is used.
    Authorized writes can be simulated only so the V2 responder can be evaluated end to end.
    """
    fixture = env or V2FixtureEnvironment()
    semantic_context: SemanticContext = build_semantic_context(fixture.catalog)
    semantic_context = with_safe_task_context(
        semantic_context,
        active_task=_task_dict(active_task),
    )
    understanding = interpret_customer_turn_v2(
        history=history,
        semantic_context=semantic_context,
        timezone_name=timezone_name,
        local_now=local_now,
    )
    plan = plan_turn(
        understanding,
        PlannerContext(
            semantic_context=semantic_context,
            active_task=active_task,
            now=local_now,
        ),
    )

    if plan.handoff_category is not None:
        outcome = build_handoff_outcome(plan)
        reply, model = compose_v2_customer_reply(
            clinic_name=clinic_name,
            timezone_name=timezone_name,
            local_now=local_now,
            history=history,
            outcomes=[outcome],
        )
        return V2HarnessResult(
            understanding=understanding,
            plan=plan,
            traces=(),
            outcomes=(outcome,),
            reply=reply,
            responder_model=model,
        )

    traces: list[V2HarnessStepTrace] = []
    outcomes: list[TurnOutcome] = []
    for step in plan.steps:
        bundle = execute_fixture_reads(step, fixture) if step.reads else ReadExecutionBundle()
        advanced = (
            advance_step_after_verification(step, bundle.verification)
            if step.write_intent is not None and step.reads
            else step
        )
        simulated_write: str | None = None
        action_result: dict[str, object] | None = None
        if advanced.disposition == "write_ready":
            if not simulate_writes:
                raise RuntimeError(
                    f"V2 test reached write-ready operation {advanced.operation_type}; "
                    "enable simulation or test the write executor separately."
                )
            simulated_write = advanced.write_intent.kind if advanced.write_intent is not None else None
            action_result = {"ok": True}

        outcome = build_step_outcome(
            advanced,
            turn=understanding,
            semantic_context=semantic_context,
            reads=bundle if bundle.results else None,
            action_result=action_result,
            active_task_summary=_task_dict(active_task),
        )
        traces.append(
            V2HarnessStepTrace(
                operation_index=advanced.operation_index,
                operation_type=advanced.operation_type,
                disposition_before=step.disposition,
                disposition_after=advanced.disposition,
                read_kinds=tuple(result.kind for result in bundle.results),
                simulated_write=simulated_write,
                outcome=outcome,
            )
        )
        outcomes.append(outcome)

    reply, model = compose_v2_customer_reply(
        clinic_name=clinic_name,
        timezone_name=timezone_name,
        local_now=local_now,
        history=history,
        outcomes=outcomes,
    )
    return V2HarnessResult(
        understanding=understanding,
        plan=plan,
        traces=tuple(traces),
        outcomes=tuple(outcomes),
        reply=reply,
        responder_model=model,
    )
