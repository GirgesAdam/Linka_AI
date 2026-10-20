from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage

from app.agents.v2.availability_reference_interpreter import interpret_availability_reference_turn
from app.agents.v2.semantic_context import SemanticContext, build_semantic_context
from app.agents.v2.semantic_state_view import (
    with_safe_availability_reference_context,
    with_safe_read_context,
)
from app.agents.v2.turn_contract import (
    Selection,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import interpret_customer_turn_v2
from app.services.agent_v2.planner import (
    PlannerContext,
    PlanStep,
    TurnPlan,
    plan_turn,
)
from app.services.agent_v2.reference_resolution import resolve_verified_availability_reference
from app.services.agent_v2.test_harness import V2FixtureEnvironment

TZ = "Africa/Cairo"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=ZoneInfo(TZ))
REPORT = Path("artifacts/pilot05-relative-availability-live-review.json")


@dataclass
class CaseResult:
    name: str
    passed: bool = False
    details: dict[str, object] = field(default_factory=dict)
    error: str | None = None


def _option(index: int, clock: str) -> dict[str, object]:
    return {
        "option_ref": f"opt_{index}",
        "index": index,
        "concrete": True,
        "start_time_24h": clock,
        "start_local": f"2026-09-12T{clock}:00+03:00",
        "doctor_name": "د. مريم",
        "laser_device_name": "Candela Gentle",
        "slot": {
            "service_id": "svc-underarm",
            "branch_id": "single-location",
            "doctor_id": "doc-maryam",
            "laser_device_key": "candela_gentle",
            "start_time_24h": clock,
            "start_local": f"2026-09-12T{clock}:00+03:00",
        },
    }


def _reference_context(
    *,
    selected_ref: str | None = None,
    time: dict[str, object] | None = None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "operation_type": "availability",
        "service_id": "svc-underarm",
        "doctor_id": "doc-maryam",
        "device_key": "candela_gentle",
        "date": {"mode": "exact", "start_date": "2026-09-12", "end_date": None},
        "availability_reference_options": [
            _option(1, "17:00"),
            _option(2, "18:00"),
            _option(3, "19:00"),
        ],
        "availability_option_count": 3,
    }
    if selected_ref is not None:
        context["last_selected_option_ref"] = selected_ref
        context["availability_reference_anchor_index"] = int(selected_ref.rsplit("_", 1)[1])
    if time is not None:
        context["time"] = time
    return context


def _semantic_context(
    recent: dict[str, object],
    reference: dict[str, object],
    *,
    followup_action: str | None,
) -> SemanticContext:
    context = build_semantic_context(V2FixtureEnvironment().catalog)
    context = with_safe_read_context(context, read_context=recent)
    context = with_safe_availability_reference_context(
        context,
        availability_context=reference,
    )
    if followup_action in {"new_search", "refresh_availability"}:
        model_input = dict(context.model_input)
        model_input["availability_followup_intent"] = {"action": followup_action}
        context = SemanticContext(
            model_input=model_input,
            reference_map=context.reference_map,
            server_metadata=context.server_metadata,
        )
    return context


def _plan_details(plan) -> list[dict[str, object]]:
    details: list[dict[str, object]] = []
    for step in plan.steps:
        details.append(
            {
                "operation_type": step.operation_type,
                "disposition": step.disposition,
                "state_action": step.state_action,
                "clarification_field": step.clarification_field,
                "reads": [
                    {"kind": read.kind, "parameters": dict(read.parameters)}
                    for read in step.reads
                ],
                "write_intent": (
                    step.write_intent.model_dump(mode="json")
                    if step.write_intent is not None
                    else None
                ),
            }
        )
    return details


def _run_option_case(name: str, *, anchor: bool) -> CaseResult:
    result = CaseResult(name=name)
    try:
        context = _reference_context(selected_ref="opt_1" if anchor else None)
        interpretation = interpret_availability_reference_turn(
            latest_customer_text="اللي بعده",
            availability_context=context,
        )
        decision = interpretation.decision
        details: dict[str, object] = {
            "reference_decision": decision.model_dump(mode="json"),
            "model_name": interpretation.model_name,
        }
        if anchor:
            assert decision.action == "select_presented_option"
            assert decision.option_ref == "opt_2"
            resolved = resolve_verified_availability_reference(
                Selection(kind="ref", ref=decision.option_ref),
                context,
            )
            details["resolution"] = resolved
            assert resolved["status"] == "resolved"
            option = resolved["option"]
            assert isinstance(option, dict)
            slot = option["slot"]
            assert isinstance(slot, dict)
            assert slot["start_time_24h"] == "18:00"
        else:
            assert decision.action == "clarify"
            assert decision.option_ref is None
        result.passed = True
        result.details = details
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _run_search_case(
    name: str,
    *,
    message: str,
    recent_time: dict[str, object] | None,
    selected_ref: str | None,
    expected_action: str,
    expected_time_mode: str | None,
    expected_start_time: str | None = None,
    expect_clarify_plan: bool = False,
) -> CaseResult:
    result = CaseResult(name=name)
    details: dict[str, object] = {
        "customer": message,
        "failure_stage": "setup",
        "full_interpreter_called": False,
    }
    result.details = details
    try:
        reference = _reference_context(selected_ref=selected_ref, time=recent_time)
        recent = {
            key: value
            for key, value in reference.items()
            if key
            in {
                "operation_type",
                "service_id",
                "doctor_id",
                "device_key",
                "date",
                "time",
            }
        }
        details["current_reference_context"] = reference
        details["recent_verified_read"] = recent
        details["failure_stage"] = "reference_interpreter"
        interpretation = interpret_availability_reference_turn(
            latest_customer_text=message,
            availability_context=reference,
        )
        decision = interpretation.decision
        details["reference_decision"] = decision.model_dump(mode="json")
        details["reference_model"] = interpretation.model_name
        details["reference_structured_output_error"] = interpretation.structured_output_error
        assert decision.action == expected_action, (
            f"reference action expected={expected_action} actual={decision.action}"
        )

        context = _semantic_context(
            recent,
            reference,
            followup_action=decision.action,
        )
        if decision.action == "clarify":
            details["failure_stage"] = "clarify_override"
            understanding = TiaTurnUnderstanding(
                operations=[
                    TurnOperation(
                        type="availability",
                        entities=TurnEntities(),
                        execution_intent="informational",
                        continues_previous=True,
                    )
                ]
            )
            plan = TurnPlan(
                steps=[
                    PlanStep(
                        operation_index=0,
                        operation_type="availability",
                        disposition="clarify",
                        response_goal="clarification",
                        clarification_field="availability_reference",
                        facts={
                            "verified_availability_reference": True,
                            "availability_reference_reason": "semantic_clarification",
                        },
                    )
                ]
            )
        else:
            details["failure_stage"] = "full_interpreter"
            details["full_interpreter_called"] = True
            understanding = interpret_customer_turn_v2(
                history=[HumanMessage(content=message)],
                semantic_context=context,
                timezone_name=TZ,
                local_now=NOW,
            )
            details["turn"] = understanding.model_dump(mode="json")
            assert understanding.operations, "full interpreter returned no operations"
            details["failure_stage"] = "planner"
            plan = plan_turn(
                understanding,
                PlannerContext(
                    semantic_context=context,
                    active_task=None,
                    now=NOW,
                    recent_read_context=recent,
                    availability_reference_context=reference,
                ),
            )

        details.setdefault("turn", understanding.model_dump(mode="json"))
        details["plan"] = _plan_details(plan)
        details["failure_stage"] = "assertions"
        assert not any(
            step.write_intent is not None for step in plan.steps
        ), "relative availability exploration created a write intent"
        if expect_clarify_plan:
            assert details["full_interpreter_called"] is False, (
                "clarify reference should bypass the full interpreter"
            )
            assert all(step.disposition == "clarify" for step in plan.steps), (
                "clarify reference did not remain a clarify plan"
            )
            assert all(not step.reads for step in plan.steps), (
                "clarify reference unexpectedly issued reads"
            )
        else:
            operation = understanding.operations[0]
            assert operation.type == "availability", (
                f"operation expected=availability actual={operation.type}"
            )
            assert operation.continues_previous is True, (
                "availability search lost continuation semantics"
            )
            assert operation.entities.time is not None, (
                "availability search lost typed time constraint"
            )
            assert operation.entities.time.mode == expected_time_mode, (
                f"time mode expected={expected_time_mode} "
                f"actual={operation.entities.time.mode}"
            )
            if expected_start_time is not None:
                assert operation.entities.time.start_time == expected_start_time, (
                    f"time anchor expected={expected_start_time} "
                    f"actual={operation.entities.time.start_time}"
                )
            availability_read = next(
                (
                    read
                    for step in plan.steps
                    for read in step.reads
                    if read.kind == "availability"
                ),
                None,
            )
            assert availability_read is not None, "planner emitted no canonical availability read"
            params = availability_read.parameters
            assert params.get("service_id") == "svc-underarm", (
                f"service lost actual={params.get('service_id')}"
            )
            assert params.get("doctor_id") == "doc-maryam", (
                f"doctor lost actual={params.get('doctor_id')}"
            )
            assert params.get("device_key") == "candela_gentle", (
                f"device lost actual={params.get('device_key')}"
            )
            date = params.get("date")
            assert isinstance(date, dict), "date constraint missing from read"
            assert date.get("start_date") == "2026-09-12", (
                f"date lost actual={date}"
            )
            time = params.get("time")
            assert isinstance(time, dict), "time constraint missing from read"
            assert time.get("mode") == expected_time_mode, (
                f"read time mode expected={expected_time_mode} actual={time.get('mode')}"
            )
            if expected_start_time is not None:
                assert time.get("start_time") == expected_start_time, (
                    f"read time anchor expected={expected_start_time} "
                    f"actual={time.get('start_time')}"
                )
        details["failure_stage"] = "complete"
        result.passed = True
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    return result

def main() -> int:
    exact_1800 = {
        "mode": "exact",
        "start_time": "18:00",
        "end_time": None,
        "start_time_ambiguity": "none",
        "end_time_ambiguity": "none",
    }
    range_17_21 = {
        "mode": "range",
        "start_time": "17:00",
        "end_time": "21:00",
        "start_time_ambiguity": "none",
        "end_time_ambiguity": "none",
    }
    cases = [
        _run_search_case(
            "C_nearest",
            message="اختارلي أقرب ميعاد متاح",
            recent_time=None,
            selected_ref=None,
            expected_action="new_search",
            expected_time_mode="nearest",
        ),
        _run_search_case(
            "D_earlier",
            message="عايز حاجة أبدر من الوقت ده",
            recent_time=exact_1800,
            selected_ref="opt_2",
            expected_action="new_search",
            expected_time_mode="before",
            expected_start_time="18:00",
        ),
        _run_search_case(
            "E_later",
            message="عايز حاجة بعد الوقت ده",
            recent_time=exact_1800,
            selected_ref="opt_2",
            expected_action="new_search",
            expected_time_mode="after",
            expected_start_time="18:00",
        ),
        _run_search_case(
            "F_relative_without_exact_anchor",
            message="عايز حاجة أبدر",
            recent_time=range_17_21,
            selected_ref=None,
            expected_action="clarify",
            expected_time_mode=None,
            expect_clarify_plan=True,
        ),
    ]
    payload = {
        "baseline_only": True,
        "production_modified": False,
        "cases": [asdict(case) for case in cases],
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for case in cases:
        print(json.dumps(asdict(case), ensure_ascii=False, separators=(",", ":")), flush=True)
    print(f"Report: {REPORT}", flush=True)
    return 0 if all(case.passed for case in cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
