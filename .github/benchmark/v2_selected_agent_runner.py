from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = ROOT / ".github" / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))
import v2_selected_agent_execution as execution

REPORT_DEFAULT = Path("artifacts/rm-selected-agent-run.json")
ALLOWED_IDS = tuple(f"RM{i:02d}" for i in range(1, 30))

CONTRACTS_PATH = ROOT / ".github" / "benchmark" / "v2_rm_scenario_contracts.json"
CONTRACT_DOCUMENT = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
SCENARIOS: dict[str, dict[str, Any]] = {
    item["scenario_id"]: item for item in CONTRACT_DOCUMENT["contracts"]
}
if tuple(SCENARIOS) != ALLOWED_IDS:
    raise RuntimeError("Canonical RM contract registry must define RM01-RM29 exactly once.")

SUBCASES: dict[str, dict[str, Any]] = {}
SUBCASE_PARENTS: dict[str, str] = {}
for parent_id, contract in SCENARIOS.items():
    for subcase in contract.get("subcases", []):
        subcase_id = str(subcase.get("subcase_id") or "").upper()
        if not subcase_id or not subcase_id.startswith(parent_id + "-"):
            raise RuntimeError(f"Invalid subcase ID {subcase_id!r} under {parent_id}.")
        if subcase_id in SUBCASES or subcase_id in SCENARIOS:
            raise RuntimeError(f"Duplicate canonical execution ID: {subcase_id}")
        SUBCASES[subcase_id] = subcase
        SUBCASE_PARENTS[subcase_id] = parent_id

ALLOWED_SELECTION_IDS = (*ALLOWED_IDS, *SUBCASES)


class SelectionError(ValueError):
    pass


def parse_requested_ids(raw: str, max_scenarios: int = 1) -> list[str]:
    """Parse explicit selectors without expanding umbrella coverage IDs."""
    if not isinstance(raw, str) or not raw.strip():
        raise SelectionError("Select at least one scenario ID, for example RM01.")
    requested = [part.strip().upper() for part in raw.split(",") if part.strip()]
    if not requested:
        raise SelectionError("Select at least one scenario ID, for example RM01.")
    unknown = sorted(set(requested) - set(ALLOWED_SELECTION_IDS))
    if unknown:
        raise SelectionError("Unknown scenario ID(s): " + ", ".join(unknown))
    if len(requested) != len(set(requested)):
        raise SelectionError("Duplicate explicit scenario IDs are not allowed.")
    if not isinstance(max_scenarios, int) or max_scenarios < 1 or max_scenarios > len(ALLOWED_IDS):
        raise SelectionError("max_scenarios must be an integer from 1 to 29.")
    if len(requested) > max_scenarios:
        raise SelectionError(f"Selected {len(requested)} selectors, exceeding max_scenarios={max_scenarios}.")
    return requested


def expand_selection(requested: list[str]) -> list[str]:
    """Expand umbrella IDs and de-duplicate umbrella/subcase overlap deterministically."""
    expanded: list[str] = []
    seen: set[str] = set()
    for selector in requested:
        if selector in SUBCASES:
            execution_ids = [selector]
        else:
            execution_ids = [
                str(item["subcase_id"]).upper()
                for item in SCENARIOS[selector].get("subcases", [])
            ] or [selector]
        for execution_id in execution_ids:
            if execution_id not in seen:
                expanded.append(execution_id)
                seen.add(execution_id)
    return expanded


def parse_scenario_ids(raw: str, max_scenarios: int = 1) -> list[str]:
    """Return independently attributable execution IDs after umbrella expansion."""
    return expand_selection(parse_requested_ids(raw, max_scenarios))


def _execution_contract(execution_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if execution_id in SCENARIOS:
        contract = SCENARIOS[execution_id]
        return execution_id, contract, contract
    parent_id = SUBCASE_PARENTS[execution_id]
    return parent_id, SCENARIOS[parent_id], SUBCASES[execution_id]


def current_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return os.getenv("GITHUB_SHA") or None


def validate_live_environment() -> tuple[bool, str]:
    """Validate declarations only; runtime DB identity is proven separately before model use."""
    if os.getenv("LINKA_QA_LIVE_APPROVED", "").lower() != "true":
        return False, "Live execution requires LINKA_QA_LIVE_APPROVED=true."
    if os.getenv("LINKA_QA_EXTERNAL_SIDE_EFFECTS_DISABLED", "").lower() != "true":
        return False, "External side effects have not been explicitly disabled."
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return False, "OPENAI_API_KEY is missing."
    if os.getenv("AGENT_V2_LIVE_ENABLED", "").lower() != "true":
        return False, "AGENT_V2_LIVE_ENABLED=true is required for the V2-only runner."
    environment = os.getenv("ENVIRONMENT", "").strip().lower()
    if environment not in {"qa", "test", "sandbox"}:
        return False, "ENVIRONMENT must explicitly identify qa/test/sandbox."
    database_url = os.getenv("LINKA_QA_DATABASE_URL", "").strip()
    expected_name = os.getenv("LINKA_QA_EXPECTED_DATABASE_NAME", "").strip()
    expected_host = os.getenv("LINKA_QA_EXPECTED_DATABASE_HOST", "").strip().lower()
    runtime_marker = os.getenv("LINKA_QA_RUNTIME_MARKER", "").strip()
    workspace_id = os.getenv("LINKA_QA_EXPECTED_WORKSPACE_ID", "").strip()
    workspace_slug = os.getenv("LINKA_QA_EXPECTED_WORKSPACE_SLUG", "").strip()
    if not all((database_url, expected_name, expected_host, runtime_marker, workspace_id, workspace_slug)):
        return False, "Dedicated QA DB host/name/marker and exact workspace ID/slug are required."
    if os.getenv("DATABASE_URL", "").strip() != database_url:
        return False, "DATABASE_URL must be exactly the approved LINKA_QA_DATABASE_URL."
    if os.getenv("MIGRATION_DATABASE_URL", "").strip() != database_url:
        return False, "MIGRATION_DATABASE_URL must be exactly the approved LINKA_QA_DATABASE_URL."
    try:
        parsed = urlsplit(database_url)
        actual_host = (parsed.hostname or "").lower()
        actual_name = parsed.path.lstrip("/").split("/", 1)[0]
    except ValueError:
        return False, "QA database URL is malformed."
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+psycopg", "postgresql+psycopg2"}:
        return False, "QA database URL must use PostgreSQL."
    if actual_host != expected_host or actual_name != expected_name:
        return False, "QA database URL does not match the declared expected host/name."
    if not re.search(r"(^|[_-])(qa|test|sandbox)([_-]|$)", actual_name, re.IGNORECASE):
        return False, "Database name must visibly identify an isolated QA/test/sandbox database."
    if actual_name.lower() in {"postgres", "production", "prod", "main"}:
        return False, "Refusing a production-like or default database name."
    if "ycuxjlkhnubztgqmhtom" in actual_host:
        return False, "Refusing the known shared Supabase project host."
    return True, "Static QA declarations passed; runtime DB marker/workspace/integration proof is still required."


def make_report(
    selected: list[str],
    mode: str,
    sha: str | None,
    reason: str,
    *,
    requested_ids: list[str] | None = None,
) -> dict[str, Any]:
    results = []
    for execution_id in selected:
        parent_id, parent, item = _execution_contract(execution_id)
        status = item.get("specification_status", parent["specification_status"])
        if status == "SPEC_BLOCKED":
            detail = " Contract is SPEC_BLOCKED and is not an executable test."
        elif item.get("real_execution_authorized") is False:
            detail = " Contract is specification-ready, but real execution is not authorized; remains NOT_RUN."
        else:
            detail = " Contract assertions are specified but not wired to the live runner; remains NOT_RUN."
        readiness = execution.execution_readiness(CONTRACT_DOCUMENT, execution_id)
        results.append({
            "scenario_id": execution_id,
            "coverage_id": parent_id,
            "subcase_id": execution_id if execution_id in SUBCASES else None,
            "name": item["scenario_name"],
            "topic": item["capability_under_test"],
            "source_scenarios": item["source_scenarios"],
            "execution_group": item.get("execution_group"),
            "specification_status": status,
            "assertion_implementation_status": parent["assertion_implementation_status"],
            "git_sha": sha,
            "status": "NOT_RUN",
            "execution_status": "NOT_RUN",
            "live_readiness": readiness,
            "expected": None,
            "actual": None,
            "evidence": {},
            "coverage_status": item["coverage_status"],
            "required_assertion_count": len(item["required_assertions"]),
            "real_execution_authorized": item.get("real_execution_authorized"),
            "reason": reason + detail,
        })
    return {
        "schema_version": 2,
        "started_at": datetime.now(UTC).isoformat(),
        "git_sha": sha,
        "mode": mode,
        "selector_count_requested": len(requested_ids or selected),
        "scenario_count_requested": len(selected),
        "requested_ids": requested_ids or selected,
        "selected_ids": selected,
        "real_model_executions": 0,
        "database_connections": 0,
        "database_writes": 0,
        "overall_status": "NOT_RUN",
        "results": results,
    }


def _write_report(report: dict[str, Any], report_arg: str) -> Path:
    report_path = Path(report_arg)
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    safe_report = execution.redact_artifact(report)
    report_path.write_text(json.dumps(safe_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely select RM01-RM29 Agent V2 contracts and approved subcases.")
    parser.add_argument("--scenario-ids", required=True, help="Comma-separated selectors, e.g. RM01, RM22, or RM22-A")
    parser.add_argument("--max-scenarios", type=int, default=1, help="Maximum explicit selectors before umbrella expansion")
    parser.add_argument("--mode", choices=("validate_only", "live"), default="validate_only")
    parser.add_argument("--live-approved", action="store_true", help="Explicit opt-in for a separately approved live run")
    parser.add_argument("--report", default=str(REPORT_DEFAULT))
    args = parser.parse_args(argv)
    try:
        requested = parse_requested_ids(args.scenario_ids, args.max_scenarios)
        selected = expand_selection(requested)
    except SelectionError as exc:
        print(f"SELECTION_REJECTED: {exc}", file=sys.stderr)
        return 2

    sha = current_sha()
    mode = args.mode
    reason = "Selection and scenario registry validated; validate-only does not call the model or database."
    if mode == "live":
        refusal = None
        if not args.live_approved:
            refusal = "pass --live-approved after separate authorization."
        else:
            approved, detail = validate_live_environment()
            if not approved:
                refusal = detail
        readiness = [execution.execution_readiness(CONTRACT_DOCUMENT, execution_id) for execution_id in selected]
        unsafe = [item for item in readiness if not item["safe_to_execute"]]
        if refusal is None and unsafe:
            refusal = "; ".join(f"{item['execution_id']}: {item['reason']}" for item in unsafe)
        if refusal:
            report = make_report(
                selected, mode, sha,
                "Live execution refused before model or database access: " + refusal,
                requested_ids=requested,
            )
            report["overall_status"] = "LIVE_REFUSED"
            for row in report["results"]:
                row["status"] = "LIVE_REFUSED"
                row["execution_status"] = "LIVE_REFUSED"
            report_path = _write_report(report, args.report)
            print("LIVE_REFUSED: " + refusal, file=sys.stderr)
            print("REPORT=" + str(report_path))
            return 3

        engine = None
        database_connections = 0
        try:
            side_effect_evidence = execution.verify_external_effect_isolation()
            engine, db_identity = execution.verify_runtime_database_identity(
                os.environ["LINKA_QA_DATABASE_URL"],
                expected_database_name=os.environ["LINKA_QA_EXPECTED_DATABASE_NAME"],
                expected_marker=os.environ["LINKA_QA_RUNTIME_MARKER"],
                expected_workspace_id=os.environ["LINKA_QA_EXPECTED_WORKSPACE_ID"],
                expected_workspace_slug=os.environ["LINKA_QA_EXPECTED_WORKSPACE_SLUG"],
            )
            database_connections = 1
            rollback_evidence = execution.verify_outer_transaction_rollback(engine)
            environment_evidence = {
                "database_identity": db_identity,
                "rollback": rollback_evidence,
                "external_effect_isolation": side_effect_evidence,
            }
        except execution.LiveSafetyError as exc:
            report = make_report(
                selected, mode, sha,
                "Live execution refused before model invocation: " + str(exc),
                requested_ids=requested,
            )
            report["overall_status"] = "LIVE_REFUSED"
            report["database_connections"] = database_connections
            for row in report["results"]:
                row["status"] = "LIVE_REFUSED"
                row["execution_status"] = "LIVE_REFUSED"
            report_path = _write_report(report, args.report)
            print("LIVE_REFUSED: " + str(exc), file=sys.stderr)
            print("REPORT=" + str(report_path))
            return 3
        except Exception as exc:  # noqa: BLE001 - environment boundary
            report = make_report(
                selected, mode, sha,
                "Live environment check failed before model invocation: " + f"{type(exc).__name__}: {exc}",
                requested_ids=requested,
            )
            report["overall_status"] = "ENVIRONMENT_ERROR"
            report["database_connections"] = database_connections
            for row in report["results"]:
                row["status"] = "ENVIRONMENT_ERROR"
                row["execution_status"] = "ENVIRONMENT_ERROR"
            report_path = _write_report(report, args.report)
            print("ENVIRONMENT_ERROR: runtime QA environment verification failed.", file=sys.stderr)
            print("REPORT=" + str(report_path))
            return 4

        try:
            evidence_rows = []
            for execution_id in selected:
                row = execution.execute_with_backend(
                    document=CONTRACT_DOCUMENT,
                    execution_id=execution_id,
                    git_sha=sha,
                    backend=lambda selected_id, source: execution.execute_real_agent_probe(
                        engine, selected_id, source
                    ),
                    environment_evidence=environment_evidence,
                    enforce_live_readiness=True,
                )
                evidence_rows.append(row)
        finally:
            if engine is not None:
                engine.dispose()

        statuses = {row["execution_status"] for row in evidence_rows}
        overall = "RUNTIME_ERROR" if "RUNTIME_ERROR" in statuses else "COMPLETED"
        report = {
            "schema_version": 3,
            "started_at": datetime.now(UTC).isoformat(),
            "git_sha": sha,
            "mode": "live",
            "requested_ids": requested,
            "selected_ids": selected,
            "overall_status": overall,
            "real_model_executions": sum(int((row.get("token_usage") or {}).get("call_count") or 0) for row in evidence_rows),
            "database_connections": database_connections,
            "database_writes": sum(
                max(0, int(value))
                for row in evidence_rows
                for key, value in (row.get("db_delta") or {}).items()
                if key not in {"activity_events", "handoff_requests", "patient_marketing_consent"}
                and isinstance(value, (int, float))
            ),
            "results": evidence_rows,
        }
        report_path = _write_report(report, args.report)
        print(json.dumps({
            "mode": "live", "selected_ids": selected, "overall_status": overall,
            "report": str(report_path), "real_model_executions": report["real_model_executions"]
        }, ensure_ascii=False))
        return 5 if overall == "RUNTIME_ERROR" else 0

    report = make_report(selected, mode, sha, reason, requested_ids=requested)
    report_path = _write_report(report, args.report)
    print(json.dumps({
        "mode": mode,
        "requested_ids": requested,
        "selected_ids": selected,
        "count": len(selected),
        "report": str(report_path),
        "real_model_executions": 0,
        "database_connections": 0,
        "database_writes": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
