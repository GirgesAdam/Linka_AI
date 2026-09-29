from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage

from app.agents.v2.responder import compose_v2_customer_reply
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_interpreter import interpret_customer_turn_v2
from app.services.agent_v2.outcome_builder import build_step_outcome, customer_visible_outcome
from app.services.agent_v2.planner import PlannerContext, plan_turn
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult

TZ = "Africa/Cairo"
NOW = datetime(2026, 9, 11, 20, 0, tzinfo=ZoneInfo(TZ))

KNOWLEDGE_TEXT = """العيادة متخصصة في الليزر والعناية بالبشرة. نقدم إزالة الشعر بالليزر لمناطق مختلفة، واستشارات جلدية وليزر، وجلسات عناية بالبشرة مثل الهيدرافيشل حسب احتياج الحالة.

في إزالة الشعر نستخدم Prime Lase وCandela Gentle. مفيش جهاز أفضل لكل الناس؛ الاختيار يعتمد على نوع البشرة والشعر والمنطقة، والمختص يحدد الأنسب للحالة. Prime Lase مناسب لخطط علاج مختلفة بإعداداته المتاحة، وCandela Gentle من الأجهزة المستخدمة بكفاءة في إزالة الشعر حسب تقييم المختص.

الهيدرافيشل جلسة عناية بالبشرة هدفها التنظيف والترطيب وتحسين مظهر البشرة، والخطة المناسبة تعتمد على احتياج البشرة.

قبل جلسة إزالة الشعر يفضّل الحلاقة بالموس وعدم استخدام الشمع أو الحلاوة قبل الجلسة. أي تعليمات خاصة بالحالة يحددها المختص."""

CATALOG = {
    "services": [
        {
            "id": "svc-underarm",
            "name": "ليزر إزالة الشعر - إبط",
            "category": "Laser Hair Removal",
            "requires_laser_device": True,
            "price_minor": 50000,
            "currency": "EGP",
            "duration_minutes": 15,
            "description": "legacy description must never reach V2",
            "laser_devices": [
                {"device_key": "prime_lase", "device_name": "Prime Lase"},
                {"device_key": "candela_gentle", "device_name": "Candela Gentle"},
            ],
        },
        {
            "id": "svc-hydrafacial",
            "name": "هيدرافيشل",
            "category": "Facial",
            "requires_laser_device": False,
            "price_minor": 120000,
            "currency": "EGP",
            "duration_minutes": 45,
            "description": "another legacy description",
        },
    ],
    "doctors": [],
    "appointments": [],
    "packages": [],
    "branches": [
        {
            "id": "branch-main",
            "name": "العيادة",
            "address_line1": "القاهرة",
            "city": "Cairo",
            "timezone": TZ,
            "working_hours": [
                {"weekday": day, "start": "10:00", "end": "22:00"}
                for day in range(7)
            ],
        }
    ],
}


def _service_by_id(service_id: object) -> dict[str, object]:
    for row in CATALOG["services"]:
        if str(row["id"]) == str(service_id):
            service = dict(row)
            service.pop("description", None)
            return service
    raise AssertionError(f"Unknown service id: {service_id}")


def _reads_for_step(step) -> ReadExecutionBundle:
    results: list[ReadResult] = []
    for request in step.reads:
        if request.kind == "service_catalog":
            service = _service_by_id(request.parameters.get("service_id"))
            if step.operation_type == "service_info":
                service["description"] = KNOWLEDGE_TEXT
            results.append(ReadResult(kind="service_catalog", ok=True, payload={"service": service}))
        elif request.kind == "clinic_info":
            results.append(
                ReadResult(
                    kind="clinic_info",
                    ok=True,
                    payload={
                        "clinic_name": "Linka Clinic",
                        "timezone": TZ,
                        "locations": [{"name": "العيادة", "city": "Cairo"}],
                        "knowledge": KNOWLEDGE_TEXT,
                    },
                )
            )
        else:
            raise AssertionError(f"KB smoke does not support read kind {request.kind}")
    return ReadExecutionBundle(results=results)


def _contains_exact(value: object, target: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_exact(item, target) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact(item, target) for item in value)
    return value == target


def _run_case(message: str, *, expected_operation: str, knowledge_expected: bool) -> None:
    semantic_context = build_semantic_context(CATALOG)
    history = [HumanMessage(content=message)]
    turn = interpret_customer_turn_v2(
        history=history,
        semantic_context=semantic_context,
        timezone_name=TZ,
        local_now=NOW,
    )
    if not turn.operations:
        raise AssertionError(f"No operation produced for: {message}")
    if turn.operations[0].type != expected_operation:
        raise AssertionError(
            f"Expected {expected_operation} for {message!r}; got {turn.operations[0].type}"
        )

    plan = plan_turn(
        turn,
        PlannerContext(semantic_context=semantic_context, active_task=None, now=NOW),
    )
    if not plan.steps:
        raise AssertionError(f"No plan step produced for: {message}")

    outcomes = []
    for step in plan.steps:
        reads = _reads_for_step(step) if step.reads else None
        outcomes.append(
            build_step_outcome(
                step,
                turn=turn,
                semantic_context=semantic_context,
                reads=reads,
            )
        )

    visible = [customer_visible_outcome(item) for item in outcomes]
    encoded = json.dumps(visible, ensure_ascii=False, default=str)
    has_knowledge = _contains_exact(visible, KNOWLEDGE_TEXT)
    if has_knowledge != knowledge_expected:
        raise AssertionError(
            f"knowledge_expected={knowledge_expected} but outcome had knowledge={has_knowledge}: {encoded}"
        )
    if "legacy description" in encoded:
        raise AssertionError("Legacy Service.description leaked into a customer-visible V2 outcome.")

    reply, model = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name=TZ,
        local_now=NOW,
        history=history,
        outcomes=outcomes,
    )
    if not reply.strip():
        raise AssertionError("V2 responder returned an empty reply.")

    print("=" * 88)
    print("CUSTOMER:", message)
    print("OPERATION:", turn.operations[0].type)
    print("VISIBLE_OUTCOME:", encoded)
    print("Linka V2:", reply)
    print("MODEL:", model)


def main() -> None:
    _run_case(
        "إيه الفرق بين كانديلا وبرايم ليز؟",
        expected_operation="clinic_info",
        knowledge_expected=True,
    )
    _run_case(
        "الهيدرافيشل بيعمل إيه؟",
        expected_operation="service_info",
        knowledge_expected=True,
    )
    _run_case(
        "ليزر الإبط بكام؟",
        expected_operation="pricing",
        knowledge_expected=False,
    )
    print("V2 KB SMOKE PASSED")


if __name__ == "__main__":
    main()
