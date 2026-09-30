from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import settings
from app.models.patient import Patient
from app.models.workspace import Workspace
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import (
    ScenarioResult,
    assert_demo_only,
    batch_token_summary,
    default_evaluation,
    jsonable,
)
from tools.agent_eval.run_batch_03 import (
    db_delta,
    extended_state_snapshot,
    make_result,
)


def named_patient(db: Session, workspace: Workspace, first: str, last: str = "") -> Patient:
    row = db.scalar(
        select(Patient).where(
            Patient.workspace_id == workspace.id,
            Patient.first_name == first,
            Patient.last_name == last,
        ).limit(1)
    )
    if row is None:
        raise RuntimeError(f"EVAL_INFRA_ERROR: patient not found: {first} {last}")
    return row


def new_patient(
    db: Session,
    workspace: Workspace,
    *,
    first: str,
    status: str = "active",
) -> Patient:
    suffix = str(uuid4().int % 1_000_000_000).zfill(9)
    phone = f"+208{suffix}"
    row = Patient(
        workspace_id=workspace.id,
        first_name=first,
        last_name="Readiness",
        phone=phone,
        phone_normalized=phone,
        preferred_language="ar",
        source="other",
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def business_delta_changed(delta: dict[str, Any]) -> bool:
    for key in (
        "appointments",
        "packages",
        "package_usages",
        "pulse_packs",
        "payments",
        "pulse_usages",
        "pulse_settlements",
    ):
        value = delta.get(key) or {}
        if value.get("created") or value.get("removed") or value.get("changed"):
            return True
    return bool(delta.get("pulse_balance_delta"))


def read_result(
    db: Session,
    workspace: Workspace,
    *,
    sid: str,
    category: str,
    patient: Patient,
    turns,
    expected: str,
    required_reads: set[str] | None = None,
    handoff_expected: bool | None = None,
) -> ScenarioResult:
    before = extended_state_snapshot(db, workspace, patient)
    rows = turns() if callable(turns) else turns
    after = extended_state_snapshot(db, workspace, patient)
    delta = db_delta(before, after)
    changed = business_delta_changed(delta)
    observed_reads = {read for turn in rows for read in turn.verified_reads}
    reads_ok = required_reads is None or required_reads.issubset(observed_reads)
    replies_ok = all(bool((turn.agent_response or "").strip()) for turn in rows)
    handoff_ok = True
    if handoff_expected is not None:
        handoff_ok = (any(turn.handoff_state for turn in rows) is handoff_expected)
    ok = (not changed) and reads_ok and replies_ok and handoff_ok
    return make_result(
        scenario_id=sid,
        category=category,
        purpose=expected,
        turns=rows,
        before=before,
        after=after,
        verification={
            "observed_reads": sorted(observed_reads),
            "business_state_changed": changed,
            "handoff_expected": handoff_expected,
        },
        deterministic_ok=ok,
        expected=expected,
        issue_severity="P1" if changed else "P2",
        issue_title="Read-only scenario violated deterministic expectations",
        issue_detail="The turn mutated business state or missed its required verified-read path.",
        handoff_ok=handoff_ok if handoff_expected is not None else None,
    )


def stage_metrics(results: list[ScenarioResult]) -> dict[str, Any]:
    calls = [call for row in results for turn in row.turns for call in turn.llm_calls]
    return {
        "total_calls": len(calls),
        "input_tokens": sum(int(call.get("input_tokens_actual") or 0) for call in calls),
        "cached_input_tokens": sum(int(call.get("cached_tokens_actual") or 0) for call in calls),
        "output_tokens": sum(int(call.get("output_tokens_actual") or 0) for call in calls),
        "total_latency_ms": sum(int(call.get("latency_ms") or 0) for call in calls),
        "fallback_calls": sum(bool(call.get("fallback_used")) for call in calls),
        "models": sorted({str(call.get("model")) for call in calls if call.get("model")}),
    }


def run_isolated_case(engine, workspace_slug: str, case_fn) -> ScenarioResult:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        workspace = db.scalar(
            select(Workspace).where(Workspace.slug == workspace_slug)
        )
        if workspace is None:
            raise RuntimeError("Workspace not found")
        assert_demo_only(workspace)
        return case_fn(db, workspace)
    except Exception as exc:
        return ScenarioResult(
            id=case_fn.__name__.removeprefix("case_"),
            category="infrastructure",
            purpose="Scenario execution failed before review.",
            turns=[],
            state_before={},
            state_after={},
            db_verification={},
            evaluation=default_evaluation(db_ok=False, grounding_ok=False),
            issues=[],
            token_usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "uncached_input_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
                "metadata_missing_calls": 0,
            },
            execution_error=f"{type(exc).__name__}: {exc}",
            review={
                "status": "INFRASTRUCTURE_FAILURE",
                "expected": "",
                "observed": {},
                "reviewer_notes": f"{type(exc).__name__}: {exc}",
                "severity": None,
                "root_cause": "Evaluation infrastructure or scenario fixture failure",
            },
        )
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


def run_group(cases, *, output_name: str) -> int:
    if os.getenv("TIA_AGENT_EVAL_CONFIRM_DEMO") != "1":
        raise RuntimeError("Set TIA_AGENT_EVAL_CONFIRM_DEMO=1")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as db:
        ws = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if ws is None:
            raise RuntimeError("Demo workspace not found")
        assert_demo_only(ws)

    results = [run_isolated_case(engine, "tia", case) for case in cases]
    summary = {
        "scenarios": len(results),
        "turns": sum(len(row.turns) for row in results),
        "issues": [
            {**issue, "scenario_id": row.id}
            for row in results
            for issue in row.issues
        ],
        "infra_failures": [row.id for row in results if row.execution_error],
        "tokens": batch_token_summary(results),
        "llm": stage_metrics(results),
    }
    payload = {
        "run_metadata": {
            "git_sha": os.getenv("TIA_AGENT_EVAL_GIT_SHA")
            or "2d4614de06586d4d51f641a0457eda42d880610f",
            "workspace": "tia",
            "model": settings.openai_model,
            "reasoning_effort": settings.openai_reasoning_effort,
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "scenario_results": [jsonable(row) for row in results],
        "summary": summary,
    }
    output = Path(f"eval_results/{output_name}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    for row in results:
        compact = {
            "id": row.id,
            "error": row.execution_error,
            "issues": row.issues,
            "turns": [
                {
                    "n": turn.turn_number,
                    "user": turn.user_message,
                    "reply": turn.agent_response,
                    "reads": turn.verified_reads,
                    "write": turn.write_attempted,
                    "write_result": turn.write_result,
                    "tokens": turn.token_usage,
                    "latency_ms": turn.latency_ms,
                    "model": turn.model,
                    "llm_calls": turn.llm_calls,
                }
                for turn in row.turns
            ],
            "db": row.db_verification,
        }
        print("READINESS_SCENARIO=" + json.dumps(compact, ensure_ascii=False, default=str), flush=True)
    print("READINESS_SUMMARY=" + json.dumps(summary, ensure_ascii=False, default=str), flush=True)
    print("READINESS_JSON=" + str(output), flush=True)
    engine.dispose()
    return 0
