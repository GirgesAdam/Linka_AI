"""Opt-in real-model tests using Linka's existing fixture harness.

These checks do not verify database persistence, channel delivery or all reply wording.
INTEGRATION cases stay in the corpus and are not falsely executed by a weaker harness.
"""
from __future__ import annotations

import json
import re
import subprocess
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

DATA = json.loads(Path(__file__).with_name("customer_scenarios.json").read_text())
CASES = [c for c in DATA["cases"] if c["execution_level"] == "LIVE_FIXTURE"]


def _dump(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _money_values(value):
    """Read explicit structured money strings only, never parse assistant prose."""
    found = set()
    if isinstance(value, dict):
        for child in value.values():
            found.update(_money_values(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_money_values(child))
    elif isinstance(value, str):
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?) EGP", value)
        if match:
            found.add(int(Decimal(match.group(1)) * 100))
    return found


def _check(result, checks):
    operations = result.understanding.operations
    kinds = {op.type for op in operations}
    if checks.get("operations_any"):
        assert kinds.intersection(checks["operations_any"]), f"operation kinds={kinds}"
    if checks.get("operations_all"):
        assert set(checks["operations_all"]) <= kinds, f"operation kinds={kinds}"
    assert not kinds.intersection(checks.get("operations_forbidden", [])), f"forbidden operations={kinds}"
    reads = {kind for trace in result.traces for kind in trace.read_kinds}
    if checks.get("reads_any"):
        assert reads.intersection(checks["reads_any"]), f"read kinds={reads}"
    for key, attr in [("clinic_details", "requested_clinic_details"), ("service_details", "requested_service_details"),
                      ("package_details", "requested_package_details"), ("patient_details", "requested_patient_details")]:
        if checks.get(key):
            actual = {v for op in operations for v in getattr(op, attr)}
            assert set(checks[key]) <= actual, f"{key}: {actual}"
    if checks.get("safety_signals"):
        assert set(checks["safety_signals"]) <= set(result.understanding.safety_signals)
    for key in ("handoff_category", "handoff_priority"):
        if key in checks:
            assert getattr(result.plan, key) == checks[key]
    if "response_disposition" in checks:
        assert result.understanding.response_disposition == checks["response_disposition"]
    if "package_sessions" in checks:
        assert any(op.entities.package_sessions == checks["package_sessions"] for op in operations)
    for key, attr in [("semantic_date", "date"), ("semantic_time", "time")]:
        if key in checks:
            field = "start_date" if attr == "date" else "start_time"
            assert any(getattr(getattr(op.entities, attr), field, None) == checks[key] for op in operations), key
    if "time_mode" in checks:
        assert any(op.entities.time and op.entities.time.mode == checks["time_mode"] for op in operations)
    task = result.active_task
    if checks.get("task_is_none"):
        assert task is None
    if "task_type" in checks:
        assert task and task.task_type == checks["task_type"]
    constraints = getattr(task, "constraints", None) or getattr(task, "replacement", None)
    if "task_service_ids" in checks:
        actual = set(getattr(task, "requested_service_ids", []))
        if not actual and constraints and constraints.service_id:
            actual = {constraints.service_id}
        assert actual == set(checks["task_service_ids"]), f"requested services={actual}"
    for key, attr in [("device", "device_key"), ("doctor", "doctor_id")]:
        if key in checks:
            assert constraints and getattr(constraints, attr) == checks[key], key
    if checks.get("doctor_is_none"):
        assert constraints and constraints.doctor_id is None
    if "date" in checks:
        assert constraints and constraints.date and constraints.date.start_date == checks["date"]
    prices = _money_values([_dump(o) for o in result.outcomes])
    if "price_minor_contains" in checks:
        assert checks["price_minor_contains"] in prices, f"structured prices={prices}"
    if "price_minor_contains_all" in checks:
        assert set(checks["price_minor_contains_all"]) <= prices, f"structured prices={prices}"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_customer_live_matrix(case, request):
    selected = {s.strip() for s in request.config.getoption("--qa-case").split(",") if s.strip()}
    if selected and case["id"] not in selected:
        pytest.skip("Scenario not selected")
    if not request.config.getoption("--qa-live"):
        pytest.skip("NOT_RUN: real-model execution requires --qa-live")

    from langchain_core.messages import AIMessage, HumanMessage

    from app.core.config import get_settings
    from app.services.agent_v2.stateful_test_harness import run_v2_stateful_fixture_turn
    from app.services.agent_v2.test_harness import V2FixtureEnvironment

    if not get_settings().openai_api_key:
        pytest.skip("BLOCKED: no model credential configured; not a product PASS")
    repo = Path(__file__).resolve().parents[3]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "merge-base", "--is-ancestor", DATA["baseline_sha"], head], cwd=repo, check=True)
    subprocess.run(["git", "diff", "--exit-code", DATA["baseline_sha"], head, "--", "backend/app"], cwd=repo, check=True)
    assert case["fixture"] == "default", "This runner supports only the documented default fixture"

    base = V2FixtureEnvironment()
    env = V2FixtureEnvironment(**{key: deepcopy(getattr(base, key)) for key in
                                 ("catalog", "clinic_info", "profile", "history", "slots", "package_offers", "refund_quotes")})
    now = datetime.fromisoformat(case["clock"]).astimezone(ZoneInfo(case["timezone"]))
    history, task, recent, availability = [], None, None, None
    records = []
    run_id = f"{case['id']}-{uuid4().hex}"
    destination = Path(request.config.getoption("--qa-evidence-dir")) / run_id
    destination.mkdir(parents=True, exist_ok=False)
    status = "AUTOMATED_CHECKS_PASSED_REPLY_REVIEW_PENDING"
    failure = None
    try:
        for turn_no, spec in enumerate(case["turns"], 1):
            before = _dump(task)
            history.append(HumanMessage(content=spec["customer"]))
            result = run_v2_stateful_fixture_turn(
                history=history, local_now=now, timezone_name=case["timezone"],
                env=env, active_task=task, recent_read_context=recent,
                availability_reference_context=availability, simulate_writes=False,
                turn_id=f"qa-{case['id']}-{turn_no}",
            )
            record = {"turn": turn_no, "customer": spec["customer"], "expected": spec["expected"],
                      "checks": spec["checks"], "state_before": before, "state_after": _dump(result.active_task),
                      "understanding": _dump(result.understanding), "plan": _dump(result.plan),
                      "outcomes": [_dump(o) for o in result.outcomes], "reply": result.reply,
                      "model": result.responder_model,
                      "trace": [{"operation": t.operation_type, "reads": list(t.read_kinds),
                                 "simulated_write": t.simulated_write} for t in result.traces]}
            records.append(record)
            assert not any(t.simulated_write for t in result.traces), "Fixture must never execute a write"
            if turn_no not in case["allowed_transactional_write_turns"]:
                assert not any(s.write_intent and s.write_intent.authorized for s in result.plan.steps), "Unexpected write authorization"
            _check(result, spec["checks"])
            task, recent, availability = result.active_task, result.recent_read_context, result.availability_reference_context
            history.append(AIMessage(content=result.reply))
    except AssertionError as exc:
        status, failure = "ASSERTION_FAILED_REQUIRES_TRIAGE", str(exc)
        raise
    except Exception as exc:
        status, failure = "ERROR_REQUIRES_TRIAGE", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        payload = {"run_id": run_id, "case_id": case["id"], "scenario": case, "execution_sha": head, "baseline_sha": DATA["baseline_sha"], "level": "LIVE_FIXTURE",
                   "status": status, "error": failure, "records": records,
                   "database_delta": "NOT_OBSERVED_IN_MEMORY_HARNESS", "reply_review": "PENDING"}
        (destination / f"{case['id']}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        if failure:
            report = [f"# Investigation — {case['id']}", "", "Classification: INCONCLUSIVE pending evidence triage.",
                      f"Baseline: {head}", f"Scenario: {case['title']}", f"Invariant: {case['invariant']}", "",
                      f"Failure: {failure}", "", f"Evidence: {case['id']}.json", "",
                      "Expected behavior by turn:", *[f"- {i}: {t['expected']}" for i, t in enumerate(case['turns'], 1)], "",
                      "Reproduce:", f"python -m pytest tests/qa_customer_matrix/test_customer_live_matrix.py --qa-live --qa-case {case['id']} -q", "",
                      "Root cause: not established. Inspect the last correct and first incorrect stages in the evidence.",
                      "No database write outcome or persistence is proven by this fixture harness.",
                      "Team Leader: classify first, link to existing defects when supported, then decide task grouping."]
            (destination / f"{case['id']}_INVESTIGATION.md").write_text("\n".join(report))
