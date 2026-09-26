from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from time import perf_counter

from tools.agent_eval.registry import (
    batch_module,
    failed_ids_from_report,
    select_scenarios,
)
from tools.agent_eval.reporting import print_summary, summary_from_rows


def _parse_selectors(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {item.strip() for item in value.split(",") if item.strip()}


def _report_path(output: str) -> Path | None:
    for line in output.splitlines():
        if line.startswith("JSON_RESULT="):
            return Path(line.split("=", 1)[1].strip())
    return None


def _execute_legacy(batch: str, scenarios, extra_args: list[str]) -> int:
    module = batch_module(batch)
    original_cases = module.CASES
    original_argv = sys.argv
    module.CASES = [row.resolve_runner() for row in scenarios]
    sys.argv = [module.__name__, *extra_args]
    buffer = io.StringIO()
    started = perf_counter()
    try:
        with redirect_stdout(buffer):
            code = int(module.main())
    finally:
        module.CASES = original_cases
        sys.argv = original_argv
    duration = perf_counter() - started
    legacy_output = buffer.getvalue()
    print(legacy_output, end="")
    path = _report_path(legacy_output)
    if path and path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = list(payload.get("scenario_results") or ())
        print_summary(summary_from_rows(rows, duration_seconds=duration))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--batch",
        required=True,
        choices=("batch_01", "batch_02", "batch_03", "batch_04", "batch_05", "batch_06"),
    )
    parser.add_argument("--scenario")
    parser.add_argument("--tag")
    parser.add_argument(
        "--profile",
        default="quick",
        choices=("quick", "full", "failed-only"),
    )
    parser.add_argument("--rerun-failed")
    args, extra_args = parser.parse_known_args(argv)

    selectors = _parse_selectors(args.scenario)
    try:
        scenarios = select_scenarios(
            args.batch,
            selectors=selectors,
            tag=args.tag,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.profile == "failed-only" and not args.rerun_failed:
        parser.error("--profile failed-only requires --rerun-failed REPORT.json")

    if args.rerun_failed:
        payload = json.loads(Path(args.rerun_failed).read_text(encoding="utf-8"))
        failed = failed_ids_from_report(payload)
        scenarios = [
            row for row in scenarios
            if row.canonical_id in failed or row.id in failed
        ]

    if not scenarios:
        parser.error("No scenarios matched the requested selection.")

    print(f"Selected scenarios: {len(scenarios)}")
    for row in scenarios:
        print(f"{row.id} {row.canonical_id} [{','.join(row.tags)}]")

    if args.profile == "quick":
        print("Quick profile: selection/import validation only; no DB or LLM execution.")
        return 0

    return _execute_legacy(args.batch, scenarios, extra_args)


if __name__ == "__main__":
    raise SystemExit(main())
