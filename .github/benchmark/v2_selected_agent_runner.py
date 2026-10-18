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


class SelectionError(ValueError):
    pass


def parse_scenario_ids(raw: str, max_scenarios: int = 1) -> list[str]:
    if not isinstance(raw, str) or not raw.strip():
        raise SelectionError("Select at least one scenario ID, for example RM01.")
    selected = [part.strip().upper() for part in raw.split(",") if part.strip()]
    if not selected:
        raise SelectionError("Select at least one scenario ID, for example RM01.")
    unknown = sorted(set(selected) - set(ALLOWED_IDS))
    if unknown:
        raise SelectionError("Unknown scenario ID(s): " + ", ".join(unknown))
    if len(selected) != len(set(selected)):
        raise SelectionError("Duplicate scenario IDs are not allowed.")
    if not isinstance(max_scenarios, int) or max_scenarios < 1 or max_scenarios > len(ALLOWED_IDS):
        raise SelectionError("max_scenarios must be an integer from 1 to 29.")
    if len(selected) > max_scenarios:
        raise SelectionError(f"Selected {len(selected)} scenarios, exceeding max_scenarios={max_scenarios}.")
    return selected


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


def make_report(selected: list[str], mode: str, sha: str | None, reason: str) -> dict[str, Any]:
    results = []
    for scenario_id in selected:
        item = SCENARIOS[scenario_id]
        status = item["specification_status"]
        if status == "SPEC_BLOCKED":
            detail = " Contract is SPEC_BLOCKED and is not an executable test."
        else:
            detail = " Contract assertions are specified but not wired to the live runner; remains NOT_RUN."
        results.append({
            "scenario_id": scenario_id,
            "name": item["scenario_name"],
            "topic": item["capability_under_test"],
            "source_scenarios": item["source_scenarios"],
            "execution_group": item["execution_group"],
            "specification_status": status,
            "assertion_implementation_status": item["assertion_implementation_status"],
            "git_sha": sha,
            "status": "NOT_RUN",
            "expected": None,
            "actual": None,
            "evidence": {},
            "coverage_status": item["coverage_status"],
            "required_assertion_count": len(item["required_assertions"]),
            "reason": reason + detail,
        })
    return {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": sha,
        "mode": mode,
        "scenario_count_requested": len(selected),
        "selected_ids": selected,
        "real_model_executions": 0,
        "database_writes": 0,
        "overall_status": "NOT_RUN",
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely select RM01-RM29 Agent V2 scenarios.")
    parser.add_argument("--scenario-ids", required=True, help="Comma-separated IDs, e.g. RM01 or RM01,RM04")
    parser.add_argument("--max-scenarios", type=int, default=1)
    parser.add_argument("--mode", choices=("validate_only", "live"), default="validate_only")
    parser.add_argument("--live-approved", action="store_true", help="Explicit opt-in for a separately approved live run")
    parser.add_argument("--report", default=str(REPORT_DEFAULT))
    args = parser.parse_args(argv)
    try:
        selected = parse_scenario_ids(args.scenario_ids, args.max_scenarios)
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
                # guarantees have not yet been proven for all 29 RM contracts. Never call a
                # model until an isolated executor and its rollback/side-effect tests exist.
                refusal = "dedicated DB runtime identity and external-side-effect isolation have not been proven by this runner. " + detail
        if refusal:
            report = make_report(selected, mode, sha, "Live execution refused before model or database access: " + refusal)
            report_path = Path(args.report)
            if not report_path.is_absolute():
                report_path = ROOT / report_path
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("LIVE_REFUSED: " + refusal, file=sys.stderr)
            print("REPORT=" + str(report_path))
            return 3

    report = make_report(selected, mode, sha, reason)
    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": mode, "selected_ids": selected, "count": len(selected), "report": str(report_path), "real_model_executions": 0, "database_writes": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
