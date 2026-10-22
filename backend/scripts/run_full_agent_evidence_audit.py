
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import textwrap
import traceback
import types
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve()
BACKEND_DIR = SCRIPT_PATH.parents[1]
REPO_ROOT = SCRIPT_PATH.parents[2]
REPORT = BACKEND_DIR / "artifacts" / "full-agent-evidence-audit.json"
HUMAN_RUNNER = BACKEND_DIR / "scripts" / "run_human_conversation_quality_batch1_review.py"
GROUPED_RUNNER = BACKEND_DIR / "scripts" / "run_grouped_multiservice_intent_live_review.py"
V2_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "v2-grouped-visit-live-review.yml"

HUMAN_FUNCTIONS = {
    "RA-H1": "ra_h1_marker_leak",
    "RA-H2": "ra_h2_multiservice_total",
    "RA-H3": "ra_h3_long_continuation",
    "RA-H4": "ra_h4_one_device",
    "RA-H5": "ra_h5_two_devices",
    "RA-H6": "ra_h6_service_correction",
    "RA-H7": "ra_h7_booking_completion",
    "RA-H8": "ra_h8_attendance_confirmation",
}
HUMAN_TOPICS = {
    "RA-H1": "device-specific booking and internal-marker containment",
    "RA-H2": "grouped multi-service price continuation",
    "RA-H3": "long grouped booking continuation across side questions",
    "RA-H4": "single-device service pricing",
    "RA-H5": "multi-device selection and pricing",
    "RA-H6": "grouped service correction",
    "RA-H7": "booking completion after explicit confirmation",
    "RA-H8": "attendance confirmation",
}
GROUPED_TOPICS = {
    "RA-G1": "grouped availability baseline",
    "RA-G2": "grouped price continuation",
    "RA-G3": "grouped long continuation",
    "RA-G4": "grouped service narrowing continuation",
    "RA-G5": "grouped proposal progression",
    "RA-G6-VISIT-TIME-SELECTION": "grouped visit-time selection",
    "RA-G7-CATEGORY-SPLIT": "cross-category grouped visit planning",
}
V2_TOPICS = {
    "mixed_authority_recovery": "Mixed package authority recovery",
    "mixed_authority_confirm_b": "Mixed package later explicit purchase",
}

_AUDIT_CHECKS: list[dict[str, Any]] = []


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    scenario_name: str
    source_suite: str
    business_topic: str
    number_of_turns_planned: int
    environment_context_variant: str
    transaction_rollback_mode: str
    runner_kind: str
    entrypoint: str
    compatibility_context: str | None = None


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _literal_sequence_length(node: ast.AST) -> int | None:
    return len(node.elts) if isinstance(node, (ast.List, ast.Tuple)) else None


def _human_turn_counts() -> dict[str, int]:
    tree = ast.parse(HUMAN_RUNNER.read_text(encoding="utf-8"), filename=str(HUMAN_RUNNER))
    by_function: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in ast.walk(node):
            if isinstance(item, ast.Call) and _call_name(item.func) == "_run" and len(item.args) >= 2:
                count = _literal_sequence_length(item.args[1])
                if count is not None:
                    by_function[node.name] = count
                    break
    return {scenario_id: by_function[function] for scenario_id, function in HUMAN_FUNCTIONS.items()}


def _grouped_turn_counts() -> dict[str, int]:
    tree = ast.parse(GROUPED_RUNNER.read_text(encoding="utf-8"), filename=str(GROUPED_RUNNER))
    counts: dict[str, int] = {}
    for item in ast.walk(tree):
        if not isinstance(item, ast.Call) or _call_name(item.func) != "run_scenario" or len(item.args) < 2:
            continue
        first = item.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str) or not first.value.startswith("RA-G"):
            continue
        count = _literal_sequence_length(item.args[1])
        if count is not None:
            counts[first.value] = count
    return counts


def _extract_v2_python() -> str:
    text = V2_WORKFLOW.read_text(encoding="utf-8")
    step_pos = text.find("- name: Run full mixed-authority recovery scenario")
    py_pos = text.find("python - <<'PY'", step_pos)
    body_start = text.find("\n", py_pos) + 1
    body_end = text.find("\n          PY", body_start)
    if min(step_pos, py_pos, body_start, body_end) < 0:
        raise RuntimeError("V2 grouped-visit inline Python block not found")
    return textwrap.dedent(text[body_start:body_end])


def _v2_turn_counts() -> dict[str, int]:
    tree = ast.parse(_extract_v2_python(), filename=str(V2_WORKFLOW))
    names = {"prepare": "mixed_authority_recovery", "prepare_confirm_b": "mixed_authority_confirm_b"}
    counts: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in names:
            continue
        for item in ast.walk(node):
            if not isinstance(item, ast.Call) or _call_name(item.func) != "PreparedScenario":
                continue
            for keyword in item.keywords:
                if keyword.arg == "messages":
                    count = _literal_sequence_length(keyword.value)
                    if count is not None:
                        counts[names[node.name]] = count
    return counts


def scenario_inventory() -> list[ScenarioSpec]:
    human_counts = _human_turn_counts()
    grouped_counts = _grouped_turn_counts()
    v2_counts = _v2_turn_counts()
    specs: list[ScenarioSpec] = []
    for scenario_id, function in HUMAN_FUNCTIONS.items():
        specs.append(ScenarioSpec(
            scenario_id, scenario_id, "Human Conversation Quality Batch 1 Live Review",
            HUMAN_TOPICS[scenario_id], human_counts[scenario_id],
            "stateful fixture with real model and simulated writes",
            "fixture simulation; no persistent DB writes", "human", function,
        ))
    for scenario_id in GROUPED_TOPICS:
        compatibility = None
        if scenario_id in {"RA-G2", "RA-G3", "RA-G4"}:
            compatibility = (
                "historical Batch-1 compatibility scenario; exact compatibility head is already an ancestor "
                "of the current PR head, so no duplicate overlay execution is required"
            )
        specs.append(ScenarioSpec(
            scenario_id, scenario_id, "Grouped Multi-Service Intent Live Review",
            GROUPED_TOPICS[scenario_id], grouped_counts[scenario_id],
            "current-head local PostgreSQL grouped harness",
            "outer SQL transaction rollback; write_executor=None", "grouped", "main", compatibility,
        ))
    for scenario_id, topic in V2_TOPICS.items():
        specs.append(ScenarioSpec(
            scenario_id, scenario_id, "V2 Grouped Visit Live Review", topic, v2_counts[scenario_id],
            "staging DB-backed V2 live-chat harness",
            "outer SQL transaction rollback after real write execution", "v2_grouped_visit", scenario_id,
        ))
    _validate_inventory(specs)
    return specs


def _validate_inventory(specs: list[ScenarioSpec]) -> None:
    ids = [item.scenario_id for item in specs]
    expected = set(HUMAN_FUNCTIONS) | set(GROUPED_TOPICS) | set(V2_TOPICS)
    if len(ids) != len(set(ids)):
        raise RuntimeError("Duplicate scenario IDs in Full Evidence Audit inventory")
    if any("C7" in item for item in ids):
        raise RuntimeError("C7 must not be included in Full Evidence Audit")
    if set(ids) != expected:
        raise RuntimeError(f"Scenario inventory mismatch: expected={sorted(expected)} actual={sorted(ids)}")


def inventory_payload(specs: list[ScenarioSpec]) -> dict[str, Any]:
    return {
        "unique_conversations_selected": len(specs),
        "turns_planned": sum(item.number_of_turns_planned for item in specs),
        "source_suites": sorted({item.source_suite for item in specs}),
        "duplicates_intentionally_executed": 0,
        "c7": "NOT_INCLUDED",
        "phase_3_3": "NOT_RUN",
        "scenarios": [asdict(item) for item in specs],
    }


def _static_assert_message(source: str, node: ast.Assert) -> str:
    expression = ast.get_source_segment(source, node.test) or ast.unparse(node.test)
    if isinstance(node.msg, ast.Constant) and isinstance(node.msg.value, str):
        return node.msg.value
    if node.msg is not None:
        return ast.get_source_segment(source, node.msg) or ast.unparse(node.msg)
    return expression


class _AssertRecorder(ast.NodeTransformer):
    def __init__(self, *, source: str, source_name: str) -> None:
        self.source = source
        self.source_name = source_name

    def visit_Assert(self, node: ast.Assert):  # noqa: N802
        self.generic_visit(node)
        expression = ast.get_source_segment(self.source, node.test) or ast.unparse(node.test)
        call = ast.Expr(value=ast.Call(
            func=ast.Name(id="_audit_eval_check", ctx=ast.Load()),
            args=[
                ast.Constant(value=f"{self.source_name}:{node.lineno}"),
                ast.Lambda(
                    args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                    body=node.test,
                ),
                ast.Constant(value=_static_assert_message(self.source, node)),
                ast.Constant(value=expression),
            ],
            keywords=[],
        ))
        return ast.copy_location(call, node)


def _audit_eval_check(name: str, thunk, message: str, expression: str) -> bool:
    record: dict[str, Any] = {
        "legacy_check_name": name,
        "assertion_expression": expression,
        "legacy_error_if_false": message,
        "condition_result": None,
        "condition_error": None,
    }
    try:
        result = bool(thunk())
    except Exception as exc:
        record["condition_error"] = f"{type(exc).__name__}: {exc}"
        _AUDIT_CHECKS.append(record)
        return False
    record["condition_result"] = result
    _AUDIT_CHECKS.append(record)
    return result


def _compile_with_recorded_asserts(source: str, *, source_name: str):
    tree = ast.parse(source, filename=source_name)
    tree = _AssertRecorder(source=source, source_name=source_name).visit(tree)
    ast.fix_missing_locations(tree)
    return compile(tree, source_name, "exec")


def _load_transformed_module(path: Path, module_name: str):
    source = path.read_text(encoding="utf-8")
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = None
    module.__dict__["_audit_eval_check"] = _audit_eval_check
    sys.modules[module_name] = module
    exec(_compile_with_recorded_asserts(source, source_name=str(path)), module.__dict__)
    return module


def _internal_error(exc: BaseException) -> dict[str, Any]:
    return {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}


def _run_internal_legacy(suite: str, scenario_id: str) -> int:
    _AUDIT_CHECKS.clear()
    error: dict[str, Any] | None = None
    try:
        if suite == "human":
            module = _load_transformed_module(HUMAN_RUNNER, "_full_audit_human_runner")
            getattr(module, HUMAN_FUNCTIONS[scenario_id])()
        elif suite == "grouped":
            os.environ["RA_G_SCENARIOS"] = scenario_id
            os.environ["RA_G_MODE"] = "standalone"
            module = _load_transformed_module(GROUPED_RUNNER, "_full_audit_grouped_runner")
            module.main()
        else:
            raise RuntimeError(f"Unsupported internal legacy suite: {suite}")
    except BaseException as exc:
        error = _internal_error(exc)
    print("AUDIT_LEGACY_CHECKS=" + json.dumps(_AUDIT_CHECKS, ensure_ascii=False, default=str), flush=True)
    if error is not None:
        print("AUDIT_INTERNAL_ERROR=" + json.dumps(error, ensure_ascii=False, default=str), flush=True)
    return 0


def _exec_check_segment(segment: str, namespace: dict[str, Any], *, source_name: str) -> dict[str, Any] | None:
    _AUDIT_CHECKS.clear()
    namespace["_audit_eval_check"] = _audit_eval_check
    try:
        exec(_compile_with_recorded_asserts(segment, source_name=source_name), namespace)
    except BaseException as exc:
        return _internal_error(exc)
    return None


def _run_internal_v2() -> int:
    code = _extract_v2_python()
    setup_end = code.find("from sqlalchemy import create_engine")
    if setup_end < 0:
        raise RuntimeError("V2 setup/execution boundary not found")
    namespace: dict[str, Any] = {"__name__": "_full_audit_v2_inline", "_audit_eval_check": _audit_eval_check}
    setup_code = code[:setup_end]
    exec(compile(ast.parse(setup_code, filename=str(V2_WORKFLOW)), str(V2_WORKFLOW), "exec"), namespace)

    core = namespace["core"]
    scenarios = namespace["scenarios"]
    original_state = namespace["original_state"]
    from sqlalchemy import create_engine

    from app.core.config import settings

    recovery_start = code.find("assert result.error is None")
    control_marker = code.find("# Control:")
    control_start = code.find("assert control.error is None")
    control_print = code.find('print("MIXED_AUTHORITY_EXPLICIT_B_CONTROL=PASS"', control_start)
    if min(recovery_start, control_marker, control_start, control_print) < 0:
        raise RuntimeError("V2 legacy-check boundaries not found")
    segments = {
        "mixed_authority_recovery": code[recovery_start:control_marker],
        "mixed_authority_confirm_b": code[control_start:control_print],
    }

    outputs: list[dict[str, Any]] = []
    try:
        for scenario_id in V2_TOPICS:
            engine = create_engine(settings.database_url, pool_pre_ping=True)
            try:
                result = core.execute_one(engine, scenario_id, scenarios.TOPICS[scenario_id])
            finally:
                engine.dispose()
            check_namespace = dict(namespace)
            check_namespace["result"] = result
            check_namespace["control"] = result
            check_error = _exec_check_segment(
                segments[scenario_id], check_namespace,
                source_name=f"{V2_WORKFLOW}:{scenario_id}",
            )
            outputs.append({
                "scenario": scenario_id,
                "result": asdict(result),
                "legacy_checks": list(_AUDIT_CHECKS),
                "legacy_evaluation_error": check_error,
            })
    finally:
        core.state = original_state
    for payload in outputs:
        print("AUDIT_V2_RESULT=" + json.dumps(payload, ensure_ascii=False, default=str), flush=True)
    return 0


def _extract_prefixed_json_lines(text: str, prefix: str) -> list[Any]:
    values: list[Any] = []
    for line in text.splitlines():
        if line.startswith(prefix):
            values.append(json.loads(line[len(prefix):]))
    return values


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _model_configuration() -> dict[str, Any]:
    return {
        "provider": os.environ.get("LLM_PROVIDER", "openai"),
        "primary_model": os.environ.get("OPENAI_MODEL", "gpt-6-luna"),
        "fallback_model": os.environ.get("OPENAI_FALLBACK_MODEL", "gpt-5-mini"),
        "reasoning_effort": os.environ.get("OPENAI_REASONING_EFFORT", "low"),
        "fallback_reasoning_effort": os.environ.get("OPENAI_FALLBACK_REASONING_EFFORT", "low"),
    }


def _quality_flags(reply: Any) -> dict[str, bool]:
    text = str(reply or "")
    sections = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    if len(sections) < 2:
        sections = [item.strip() for item in text.splitlines() if item.strip()]
    lowered = text.lower()
    return {
        "empty_reply": not bool(text.strip()),
        "duplicate_identical_response_sections": len(sections) != len(set(sections)),
        "uncaught_fallback_or_error_text": any(
            marker in lowered
            for marker in ("traceback (most recent call last)", "structuredoutputerror", "internal server error")
        ),
        "internal_marker_leakage": any(marker in text for marker in ("TIA_LASER_DEVICE", "||")),
    }


def _proposal_from_task(task: Any) -> Any:
    return task.get("proposal") if isinstance(task, dict) else None


def _option_snapshot_from_task(task: Any) -> Any:
    return task.get("option_snapshot") if isinstance(task, dict) else None


def _db_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    delta: dict[str, int] = {}
    for key in set(before) | set(after):
        left, right = before.get(key), after.get(key)
        if isinstance(left, int) and isinstance(right, int):
            delta[key] = right - left
    return delta


def _write_evidence(*, plan: Any, traces: list[Any], outcomes: list[Any], pending_write: Any, simulated: bool, rollback: bool) -> dict[str, Any]:
    proposed: list[Any] = []
    if isinstance(plan, dict):
        for step in plan.get("steps") or []:
            if isinstance(step, dict) and step.get("disposition") == "write_ready":
                proposed.append(step)
    ready = [trace for trace in traces if isinstance(trace, dict) and trace.get("pending_write") is True]
    simulated_writes = [
        trace.get("simulated_write") for trace in traces
        if isinstance(trace, dict) and trace.get("simulated_write") is not None
    ]
    attempted: list[Any] = []
    completed: list[Any] = []
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        action = outcome.get("action_result")
        if isinstance(action, dict) and action.get("write_kind"):
            attempted.append(action)
            if outcome.get("status") == "completed":
                completed.append(action)
    return {
        "write_proposed": proposed,
        "write_ready": ready or ([pending_write] if pending_write is not None else []),
        "write_attempted": [] if simulated else attempted,
        "write_completed": [] if simulated else completed,
        "simulated_writes": simulated_writes,
        "audit_transaction_rollback_enabled": rollback,
    }


def _normalize_human_turns(raw_turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    previous_task: Any = None
    for index, raw in enumerate(raw_turns, start=1):
        canonical = raw.get("canonical") if isinstance(raw.get("canonical"), dict) else {}
        task_after = canonical.get("active_task")
        traces = list(canonical.get("traces") or [])
        outcomes = list(canonical.get("outcomes") or [])
        plan = canonical.get("plan")
        operations = list(canonical.get("operation_details") or [])
        turns.append({
            "turn_number": index,
            "customer_message": raw.get("customer"),
            "agent_visible_reply": raw.get("agent"),
            "model_used": raw.get("model"),
            "structured_understanding": canonical.get("structured_understanding") or {"operations": operations},
            "operations": operations,
            "execution_intent_per_operation": [x.get("execution_intent") for x in operations if isinstance(x, dict)],
            "active_task_relationship": [x.get("active_task_relationship") for x in operations if isinstance(x, dict)],
            "active_task_before": previous_task,
            "active_task_after": task_after,
            "proposal_before": _proposal_from_task(previous_task),
            "proposal_after": _proposal_from_task(task_after),
            "option_snapshot": _option_snapshot_from_task(task_after),
            "recent_verified_read_context": canonical.get("recent_read_context"),
            "recent_verified_action_context": canonical.get("verified_action_context"),
            "availability_reference_context": canonical.get("availability_reference_context"),
            "planner_traces": traces,
            "reads": [read for trace in traces if isinstance(trace, dict) for read in list(trace.get("reads") or [])],
            "write_evidence": _write_evidence(
                plan=plan, traces=traces, outcomes=outcomes, pending_write=None, simulated=True, rollback=False
            ),
            "completed_outcomes": [x for x in outcomes if isinstance(x, dict) and x.get("status") == "completed"],
            "response_goal": [trace.get("response_goal") for trace in traces if isinstance(trace, dict)],
            "canonical_facts": [trace.get("facts") for trace in traces if isinstance(trace, dict) and trace.get("facts")],
            "db_before": None,
            "db_after": None,
            "db_delta": None,
            "exceptions_errors": [],
            "customer_visible_quality_flags": _quality_flags(raw.get("agent")),
        })
        previous_task = task_after
    return turns


def _normalize_grouped_turns(raw_turns: list[dict[str, Any]], db_before: dict[str, Any] | None) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    previous_db = db_before
    for index, raw in enumerate(raw_turns, start=1):
        task_before = raw.get("active_task_before")
        task_after = raw.get("active_task")
        understanding = raw.get("understanding") if isinstance(raw.get("understanding"), dict) else {}
        operations = list(understanding.get("operations") or [])
        traces = list(raw.get("traces") or [])
        outcomes = list(raw.get("outcomes") or [])
        db_after = raw.get("db_state_after") if isinstance(raw.get("db_state_after"), dict) else None
        turns.append({
            "turn_number": index,
            "customer_message": raw.get("customer"),
            "agent_visible_reply": raw.get("reply"),
            "model_used": raw.get("responder_model"),
            "structured_understanding": understanding,
            "operations": operations,
            "execution_intent_per_operation": [x.get("execution_intent") for x in operations if isinstance(x, dict)],
            "active_task_relationship": [x.get("active_task_relationship") for x in operations if isinstance(x, dict)],
            "active_task_before": task_before,
            "active_task_after": task_after,
            "proposal_before": _proposal_from_task(task_before),
            "proposal_after": raw.get("proposal") or _proposal_from_task(task_after),
            "option_snapshot": _option_snapshot_from_task(task_after) or raw.get("pending_choice"),
            "recent_verified_read_context": raw.get("availability_reference_context"),
            "recent_verified_action_context": raw.get("verified_action_context"),
            "availability_reference_context": raw.get("availability_reference_context"),
            "planner_traces": traces,
            "reads": [trace.get("read_kinds") for trace in traces if isinstance(trace, dict) and trace.get("read_kinds")],
            "write_evidence": _write_evidence(
                plan=raw.get("plan"), traces=traces, outcomes=outcomes,
                pending_write=raw.get("pending_write"), simulated=False, rollback=True
            ),
            "completed_outcomes": [x for x in outcomes if isinstance(x, dict) and x.get("status") == "completed"],
            "response_goal": [x.get("response_goal") for x in outcomes if isinstance(x, dict)],
            "canonical_facts": [x.get("facts") for x in outcomes if isinstance(x, dict) and x.get("facts")],
            "db_before": previous_db,
            "db_after": db_after,
            "db_delta": _db_delta(previous_db, db_after),
            "exceptions_errors": [],
            "customer_visible_quality_flags": _quality_flags(raw.get("reply")),
        })
        previous_db = db_after or previous_db
    return turns


def _normalize_v2_turns(raw_turns: list[dict[str, Any]], db_before: dict[str, Any] | None) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    previous_task: Any = None
    previous_db = db_before
    for index, raw in enumerate(raw_turns, start=1):
        understanding = raw.get("understanding") if isinstance(raw.get("understanding"), dict) else {}
        operations = list(understanding.get("operations") or [])
        traces = list(raw.get("traces") or [])
        outcomes = list(raw.get("outcomes") or [])
        task_after = raw.get("active_task")
        db_after = raw.get("state_after") if isinstance(raw.get("state_after"), dict) else None
        turns.append({
            "turn_number": index,
            "customer_message": raw.get("customer"),
            "agent_visible_reply": raw.get("assistant"),
            "model_used": raw.get("model"),
            "structured_understanding": understanding,
            "operations": operations,
            "execution_intent_per_operation": [x.get("execution_intent") for x in operations if isinstance(x, dict)],
            "active_task_relationship": [x.get("active_task_relationship") for x in operations if isinstance(x, dict)],
            "active_task_before": previous_task,
            "active_task_after": task_after,
            "proposal_before": _proposal_from_task(previous_task),
            "proposal_after": _proposal_from_task(task_after),
            "option_snapshot": _option_snapshot_from_task(task_after) or raw.get("pending_choice"),
            "recent_verified_read_context": None,
            "recent_verified_action_context": raw.get("verified_action_context"),
            "availability_reference_context": None,
            "planner_traces": traces,
            "reads": [trace.get("read_kinds") for trace in traces if isinstance(trace, dict) and trace.get("read_kinds")],
            "write_evidence": _write_evidence(
                plan=raw.get("plan"), traces=traces, outcomes=outcomes,
                pending_write=raw.get("pending_write"), simulated=False, rollback=True
            ),
            "completed_outcomes": [x for x in outcomes if isinstance(x, dict) and x.get("status") == "completed"],
            "response_goal": [x.get("response_goal") for x in outcomes if isinstance(x, dict)],
            "canonical_facts": [x.get("facts") for x in outcomes if isinstance(x, dict) and x.get("facts")],
            "db_before": previous_db,
            "db_after": db_after,
            "db_delta": _db_delta(previous_db, db_after),
            "exceptions_errors": [],
            "customer_visible_quality_flags": _quality_flags(raw.get("assistant")),
            "duration_ms": raw.get("duration_ms"),
            "token_usage": raw.get("token_usage"),
        })
        previous_task = task_after
        previous_db = db_after or previous_db
    return turns


def _final_state(turns: list[dict[str, Any]], db_after: dict[str, Any] | None) -> dict[str, Any]:
    last = turns[-1] if turns else {}
    db = db_after or {}
    return {
        "active_appointments": db.get("active_appointments", db.get("appointments")),
        "active_packages": db.get("active_packages", db.get("patient_packages_active")),
        "cancelled_packages": db.get("cancelled_packages", db.get("patient_packages_cancelled")),
        "package_usage_totals": db.get("package_usages"),
        "payment_totals_or_transaction_presence": {
            "payments": db.get("payments"), "settlement_transactions": db.get("settlement_transactions")
        },
        "settlement_state": db.get("settlements"),
        "refund_state": db.get("refunds"),
        "active_task": last.get("active_task_after"),
        "current_proposal": last.get("proposal_after"),
        "recent_action_context": last.get("recent_verified_action_context"),
    }


def _scenario_local_errors(internal_error: Any, checks: list[dict[str, Any]], legacy_error: Any = None) -> list[Any]:
    errors: list[Any] = []
    if internal_error:
        errors.append({"phase": "scenario_execution_or_legacy_followup", "error": internal_error})
    if legacy_error:
        errors.append({"phase": "legacy_check_evaluation", "error": legacy_error})
    for check in checks:
        if check.get("condition_error"):
            errors.append({
                "phase": "legacy_check_condition",
                "legacy_check_name": check.get("legacy_check_name"),
                "error": check.get("condition_error"),
            })
    return errors


def _conversation_record(
    spec: ScenarioSpec, *, turns: list[dict[str, Any]], checks: list[dict[str, Any]],
    db_before: dict[str, Any] | None, db_after: dict[str, Any] | None, db_delta: dict[str, Any] | None,
    internal_error: Any = None, legacy_evaluation_error: Any = None, observations: list[Any] | None = None,
) -> dict[str, Any]:
    executed = len(turns)
    status = "EXECUTED" if executed == spec.number_of_turns_planned else "INCOMPLETE" if executed else "EXECUTION_ERROR"
    source_runner = HUMAN_RUNNER if spec.runner_kind == "human" else GROUPED_RUNNER if spec.runner_kind == "grouped" else V2_WORKFLOW
    return {
        "scenario_id": spec.scenario_id,
        "scenario_name": spec.scenario_name,
        "source_suite": spec.source_suite,
        "business_topic": spec.business_topic,
        "number_of_turns_planned": spec.number_of_turns_planned,
        "number_of_turns_executed": executed,
        "environment_context_variant": spec.environment_context_variant,
        "model_configuration": _model_configuration(),
        "transaction_rollback_mode": spec.transaction_rollback_mode,
        "scenario_variant": None,
        "compatibility_context": spec.compatibility_context,
        "execution_status": status,
        "turns": turns,
        "db_evidence": {"before": db_before, "after": db_after, "delta": db_delta},
        "final_state": _final_state(turns, db_after),
        "legacy_checks": checks,
        "legacy_assertion_triggered": any(x.get("condition_result") is False for x in checks),
        "legacy_assertion_messages": [x.get("legacy_error_if_false") for x in checks if x.get("condition_result") is False],
        "scenario_local_errors": _scenario_local_errors(internal_error, checks, legacy_evaluation_error),
        "observations": list(observations or []),
        "portfolio_metadata": {
            "source_runner": str(source_runner.relative_to(REPO_ROOT)),
            "entrypoint": spec.entrypoint,
            "messages_owned_by_existing_runner": True,
            "classification_for_team_lead": None,
        },
    }


def _run_subprocess(args: list[str], *, env: dict[str, str] | None = None, timeout: int = 900) -> dict[str, Any]:
    try:
        result = subprocess.run(
            args, cwd=BACKEND_DIR, env=env, text=True, capture_output=True, timeout=timeout, check=False
        )
        return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "timeout": False}
    except subprocess.TimeoutExpired as exc:
        return {"returncode": None, "stdout": exc.stdout or "", "stderr": exc.stderr or "", "timeout": True}


def _run_legacy_spec(spec: ScenarioSpec) -> dict[str, Any]:
    raw = _run_subprocess([
        sys.executable, str(SCRIPT_PATH), "--internal-suite", spec.runner_kind, "--scenario", spec.scenario_id
    ])
    stdout = str(raw.get("stdout") or "")
    prefix = "RA_RESULT=" if spec.runner_kind == "human" else "RA_G_RESULT="
    payloads = _extract_prefixed_json_lines(stdout, prefix)
    payload = payloads[-1] if payloads else None
    check_rows = _extract_prefixed_json_lines(stdout, "AUDIT_LEGACY_CHECKS=")
    checks = check_rows[-1] if check_rows and isinstance(check_rows[-1], list) else []
    errors = _extract_prefixed_json_lines(stdout, "AUDIT_INTERNAL_ERROR=")
    internal_error: Any = errors[-1] if errors else None
    if raw.get("timeout"):
        internal_error = {"type": "TimeoutExpired", "message": "scenario subprocess exceeded audit timeout"}
    elif raw.get("returncode") not in (0, None) and internal_error is None:
        internal_error = {
            "type": "SubprocessExit", "message": f"scenario subprocess exited {raw['returncode']}",
            "stderr_tail": str(raw.get("stderr") or "")[-4000:],
        }
    if not isinstance(payload, dict):
        if spec.runner_kind == "human":
            partial_rows = _extract_prefixed_json_lines(stdout, "RA_H_TURN=")
            partial_turns = [
                row.get("turn") for row in partial_rows
                if isinstance(row, dict) and row.get("scenario") == spec.scenario_id and isinstance(row.get("turn"), dict)
            ]
            if partial_turns:
                return _conversation_record(
                    spec, turns=_normalize_human_turns(partial_turns), checks=checks,
                    db_before=None, db_after=None, db_delta=None,
                    internal_error=internal_error or {"type": "IncompleteConversation", "message": f"{prefix} final payload not emitted"},
                )
        else:
            start_rows = _extract_prefixed_json_lines(stdout, "RA_G_START=")
            partial_rows = _extract_prefixed_json_lines(stdout, "RA_G_TURN=")
            start = next(
                (row for row in start_rows if isinstance(row, dict) and row.get("scenario") == spec.scenario_id),
                None,
            )
            partial_turns = [
                row.get("turn") for row in partial_rows
                if isinstance(row, dict) and row.get("scenario") == spec.scenario_id and isinstance(row.get("turn"), dict)
            ]
            if partial_turns:
                partial_before = start.get("db_before") if isinstance(start, dict) and isinstance(start.get("db_before"), dict) else None
                partial_after = partial_turns[-1].get("db_state_after") if isinstance(partial_turns[-1].get("db_state_after"), dict) else None
                return _conversation_record(
                    spec, turns=_normalize_grouped_turns(partial_turns, partial_before), checks=checks,
                    db_before=partial_before, db_after=partial_after, db_delta=_db_delta(partial_before, partial_after),
                    internal_error=internal_error or {"type": "IncompleteConversation", "message": f"{prefix} final payload not emitted"},
                )
        return _conversation_record(
            spec, turns=[], checks=checks, db_before=None, db_after=None, db_delta=None,
            internal_error=internal_error or {"type": "EvidenceMissing", "message": f"{prefix} payload not emitted"},
        )
    if spec.runner_kind == "human":
        return _conversation_record(
            spec, turns=_normalize_human_turns(list(payload.get("turns") or [])), checks=checks,
            db_before=None, db_after=None, db_delta=None, internal_error=internal_error,
        )
    db_before = payload.get("db_before") if isinstance(payload.get("db_before"), dict) else None
    db_after = payload.get("db_after") if isinstance(payload.get("db_after"), dict) else None
    db_delta = payload.get("db_delta") if isinstance(payload.get("db_delta"), dict) else _db_delta(db_before, db_after)
    return _conversation_record(
        spec, turns=_normalize_grouped_turns(list(payload.get("turns") or []), db_before), checks=checks,
        db_before=db_before, db_after=db_after, db_delta=db_delta, internal_error=internal_error,
    )


def _run_v2_specs(specs: list[ScenarioSpec]) -> list[dict[str, Any]]:
    env = dict(os.environ)
    live_url = env.get("FULL_AUDIT_LIVE_DATABASE_URL", "")
    env["DATABASE_URL"] = live_url
    env["MIGRATION_DATABASE_URL"] = live_url
    raw = _run_subprocess([sys.executable, str(SCRIPT_PATH), "--internal-suite", "v2"], env=env, timeout=1800)
    payloads = _extract_prefixed_json_lines(str(raw.get("stdout") or ""), "AUDIT_V2_RESULT=")
    by_name = {item.get("scenario"): item for item in payloads if isinstance(item, dict)}
    records: list[dict[str, Any]] = []
    for spec in specs:
        payload = by_name.get(spec.scenario_id)
        if not isinstance(payload, dict):
            error: Any = {"type": "EvidenceMissing", "message": "AUDIT_V2_RESULT payload not emitted"}
            if raw.get("timeout"):
                error = {"type": "TimeoutExpired", "message": "V2 grouped-visit audit exceeded timeout"}
            elif raw.get("returncode") not in (0, None):
                error = {
                    "type": "SubprocessExit", "message": f"V2 audit subprocess exited {raw['returncode']}",
                    "stderr_tail": str(raw.get("stderr") or "")[-4000:],
                }
            records.append(_conversation_record(
                spec, turns=[], checks=[], db_before=None, db_after=None, db_delta=None, internal_error=error
            ))
            continue
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        db_before = result.get("db_before") if isinstance(result.get("db_before"), dict) else None
        db_after = result.get("db_after") if isinstance(result.get("db_after"), dict) else None
        db_delta = result.get("db_delta") if isinstance(result.get("db_delta"), dict) else _db_delta(db_before, db_after)
        result_error = result.get("error")
        internal_error = {"type": "ScenarioResultError", "message": result_error} if result_error else None
        records.append(_conversation_record(
            spec,
            turns=_normalize_v2_turns(list(result.get("turns") or []), db_before),
            checks=list(payload.get("legacy_checks") or []),
            db_before=db_before, db_after=db_after, db_delta=db_delta,
            internal_error=internal_error,
            legacy_evaluation_error=payload.get("legacy_evaluation_error"),
            observations=list(result.get("observations") or []),
        ))
    return records


def _preflight_environment(specs: list[ScenarioSpec]) -> list[str]:
    errors: list[str] = []
    if not os.environ.get("OPENAI_API_KEY"):
        errors.append("OPENAI_API_KEY is missing")
    local_url = os.environ.get("DATABASE_URL")
    live_url = os.environ.get("FULL_AUDIT_LIVE_DATABASE_URL")
    if not local_url:
        errors.append("DATABASE_URL for local grouped harness is missing")
    if not live_url:
        errors.append("FULL_AUDIT_LIVE_DATABASE_URL is missing")
    try:
        _validate_inventory(specs)
        _extract_v2_python()
    except Exception as exc:
        errors.append(f"scenario inventory/V2 extraction invalid: {type(exc).__name__}: {exc}")
    if local_url:
        try:
            from sqlalchemy import create_engine, text
            engine = create_engine(local_url, pool_pre_ping=True)
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
            finally:
                engine.dispose()
        except Exception as exc:
            errors.append(f"local audit database unavailable: {type(exc).__name__}: {exc}")
    if live_url:
        try:
            from sqlalchemy import create_engine, text
            engine = create_engine(live_url, pool_pre_ping=True)
            try:
                with engine.connect() as connection:
                    workspace = connection.execute(
                        text("SELECT slug FROM workspaces WHERE slug = 'tia' LIMIT 1")
                    ).scalar_one_or_none()
                    if workspace != "tia":
                        errors.append("wrong live database identity: workspace 'tia' not found")
            finally:
                engine.dispose()
        except Exception as exc:
            errors.append(f"live audit database unavailable: {type(exc).__name__}: {exc}")
    try:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        probe = REPORT.parent / ".full-audit-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        errors.append(f"artifact writer unavailable: {type(exc).__name__}: {exc}")
    return errors


def _positive_delta(conversation: dict[str, Any], *keys: str) -> int:
    delta = conversation.get("db_evidence", {}).get("delta")
    if not isinstance(delta, dict):
        return 0
    for key in keys:
        value = delta.get(key)
        if isinstance(value, int):
            return max(value, 0)
    return 0


def _factual_write_totals(conversations: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "appointments": sum(_positive_delta(x, "appointments") for x in conversations),
        "patient_packages_created": sum(_positive_delta(x, "patient_packages") for x in conversations),
        "patient_packages_cancelled": sum(
            _positive_delta(x, "cancelled_packages", "patient_packages_cancelled") for x in conversations
        ),
        "package_usages": sum(_positive_delta(x, "package_usages") for x in conversations),
        "payments": sum(_positive_delta(x, "payments") for x in conversations),
        "settlements": sum(_positive_delta(x, "settlements") for x in conversations),
        "refunds": sum(_positive_delta(x, "refunds") for x in conversations),
    }


def _technical_summary(conversations: list[dict[str, Any]], inventory: dict[str, Any], infrastructure_errors: list[str]) -> dict[str, Any]:
    checks = [check for item in conversations for check in item.get("legacy_checks", []) if isinstance(check, dict)]
    totals = _factual_write_totals(conversations)
    return {
        "conversations_selected": len(conversations),
        "conversations_fully_executed": sum(x.get("execution_status") == "EXECUTED" for x in conversations),
        "conversations_incomplete": sum(x.get("execution_status") == "INCOMPLETE" for x in conversations),
        "conversations_execution_error": sum(x.get("execution_status") == "EXECUTION_ERROR" for x in conversations),
        "turns_planned": inventory["turns_planned"],
        "turns_executed": sum(int(x.get("number_of_turns_executed") or 0) for x in conversations),
        "scenario_local_exceptions": sum(bool(x.get("scenario_local_errors")) for x in conversations),
        "infrastructure_errors": len(infrastructure_errors),
        "legacy_assertions_triggered": sum(x.get("condition_result") is False for x in checks),
        "legacy_check_evaluation_errors": sum(bool(x.get("condition_error")) for x in checks),
        "appointments_created_during_execution": totals["appointments"],
        "packages_created": totals["patient_packages_created"],
        "packages_cancelled": totals["patient_packages_cancelled"],
        "payments_created": totals["payments"],
        "package_usages_created": totals["package_usages"],
    }


def _write_report(payload: dict[str, Any]) -> int:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    payload.setdefault("audit_metadata", {})["artifact_size_bytes"] = REPORT.stat().st_size
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return REPORT.stat().st_size


def run_full_audit() -> int:
    started = datetime.now(UTC)
    specs = scenario_inventory()
    inventory = inventory_payload(specs)
    infrastructure_errors = _preflight_environment(specs)
    payload: dict[str, Any] = {
        "audit_metadata": {
            "audit_name": "FULL REAL-AGENT EVIDENCE AUDIT",
            "started_at": started.isoformat(),
            "finished_at": None,
            "head": _git_head(),
            "model_configuration": _model_configuration(),
            "database_writes_persisted": False,
            "business_verdict": "NOT_PROVIDED",
            "c7": "NOT_INCLUDED",
            "phase_3_3": "NOT_RUN",
        },
        "scenario_inventory": inventory,
        "conversations": [],
        "technical_summary": {},
        "factual_write_totals": {},
        "infrastructure_errors": infrastructure_errors,
    }
    if infrastructure_errors:
        payload["audit_metadata"]["finished_at"] = datetime.now(UTC).isoformat()
        payload["audit_metadata"]["technical_result"] = "BLOCKED"
        payload["technical_summary"] = _technical_summary([], inventory, infrastructure_errors)
        payload["factual_write_totals"] = _factual_write_totals([])
        _write_report(payload)
        print("FULL_AUDIT_TECHNICAL_RESULT=BLOCKED", flush=True)
        for error in infrastructure_errors:
            print(f"INFRASTRUCTURE_ERROR={error}", flush=True)
        print(f"REPORT={REPORT}", flush=True)
        return 1

    conversations: list[dict[str, Any]] = []
    legacy_specs = [x for x in specs if x.runner_kind in {"human", "grouped"}]
    for index, spec in enumerate(legacy_specs, start=1):
        print(f"[{index:02d}/{len(specs)}] executing {spec.scenario_id}", flush=True)
        conversations.append(_run_legacy_spec(spec))
    v2_specs = [x for x in specs if x.runner_kind == "v2_grouped_visit"]
    print(f"[{len(legacy_specs)+1:02d}/{len(specs)}] executing V2 grouped-visit conversations", flush=True)
    conversations.extend(_run_v2_specs(v2_specs))

    order = {item.scenario_id: index for index, item in enumerate(specs)}
    conversations.sort(key=lambda item: order[item["scenario_id"]])
    payload["conversations"] = conversations
    payload["technical_summary"] = _technical_summary(conversations, inventory, infrastructure_errors)
    payload["factual_write_totals"] = _factual_write_totals(conversations)
    payload["audit_metadata"]["finished_at"] = datetime.now(UTC).isoformat()
    payload["audit_metadata"]["technical_result"] = "SUCCESS"
    size = _write_report(payload)
    print("FULL_AUDIT_SUMMARY=" + json.dumps(payload["technical_summary"], ensure_ascii=False, separators=(",", ":")), flush=True)
    print("FULL_AUDIT_WRITES=" + json.dumps(payload["factual_write_totals"], separators=(",", ":")), flush=True)
    print(f"ARTIFACT_SIZE_BYTES={size}", flush=True)
    print(f"REPORT={REPORT}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the merge-relevant real-Agent Full Evidence Audit.")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--internal-suite", choices=("human", "grouped", "v2"))
    parser.add_argument("--scenario")
    args = parser.parse_args()
    if args.inventory_only:
        print(json.dumps(inventory_payload(scenario_inventory()), ensure_ascii=False, indent=2))
        return 0
    if args.internal_suite == "v2":
        return _run_internal_v2()
    if args.internal_suite in {"human", "grouped"}:
        if not args.scenario:
            raise SystemExit("--scenario is required for human/grouped internal execution")
        return _run_internal_legacy(args.internal_suite, args.scenario)
    return run_full_audit()


if __name__ == "__main__":
    raise SystemExit(main())
