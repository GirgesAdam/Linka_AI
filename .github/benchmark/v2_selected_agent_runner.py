from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
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
    """Fail closed before model invocation unless the dedicated QA environment is proven."""
    if os.getenv("LINKA_QA_LIVE_APPROVED", "").lower() != "true":
        return False, "Live execution requires LINKA_QA_LIVE_APPROVED=true."
    if os.getenv("LINKA_QA_EXTERNAL_SIDE_EFFECTS_DISABLED", "").lower() != "true":
        return False, "External side effects have not been explicitly disabled."
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return False, "OPENAI_API_KEY is missing."
    database_url = os.getenv("LINKA_QA_DATABASE_URL", "").strip()
    expected_name = os.getenv("LINKA_QA_EXPECTED_DATABASE_NAME", "").strip()
    expected_host = os.getenv("LINKA_QA_EXPECTED_DATABASE_HOST", "").strip().lower()
    if not database_url or not expected_name or not expected_host:
        return False, "Dedicated QA database URL and expected host/name are required."
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
    return True, "Environment declarations passed static checks; runtime database identity must still be verified."


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
        "started_at": datetime.now(timezone.utc).isoformat(),
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
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
            else:
                # The existing benchmark's transactional execution and external side-effect
                # guarantees have not yet been proven for all RM contracts/subcases. Never call
                # a model until an isolated executor and its rollback/side-effect tests exist.
                refusal = "dedicated DB runtime identity and external-side-effect isolation have not been proven by this runner. " + detail
        if refusal:
            report = make_report(
                selected, mode, sha,
                "Live execution refused before model or database access: " + refusal,
                requested_ids=requested,
            )
            report_path = _write_report(report, args.report)
            print("LIVE_REFUSED: " + refusal, file=sys.stderr)
            print("REPORT=" + str(report_path))
            return 3

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
