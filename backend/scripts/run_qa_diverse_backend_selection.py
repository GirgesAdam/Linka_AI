"""Run eight distinct existing audit conversations once, with unchanged validators."""
from __future__ import annotations

import json
import os
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import run_grouped_multiservice_intent_live_review as grouped
import run_phase3_real_world_agent_audit as audit

SELECTED_RW = ("RW-2", "RW-5", "RW-6", "RW-8", "RW-9", "RW-10", "RW-11")


def main():
    directory = Path("artifacts/qa-diverse/backend")
    directory.mkdir(parents=True, exist_ok=True)
    by_id = {spec.name: spec for spec in audit.SCENARIOS}
    assert set(SELECTED_RW) <= set(by_id)
    results = []
    jobs = [("RA-C7-REPLACEMENT", None), *[(name, by_id[name]) for name in SELECTED_RW]]
    for name, spec in jobs:
        path = directory / f"{name}.log"
        with path.open("w", encoding="utf-8") as stream, redirect_stdout(stream), redirect_stderr(stream):
            try:
                if spec is None:
                    grouped.run_scenario(name, [grouped.C7_INITIAL, grouped.C7_REPLACEMENT], grouped.validate_c7_replacement)
                    result = {"scenario": name, "status": "PASS", "error": None}
                else:
                    result = audit._run_scenario(spec)
            except Exception as exc:  # noqa: BLE001 - retain evidence and continue independent audit cases
                traceback.print_exc()
                result = {"scenario": name, "status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
        result["evidence"] = str(path)
        results.append(result)
        print("QA_RESULT=" + json.dumps(result, ensure_ascii=False), flush=True)
    summary = {"runtime_baseline_sha": "0a2885809efe6dec7d6bc184abc158725228faba",
               "execution_sha": os.environ.get("GITHUB_SHA"), "run_id": os.environ.get("GITHUB_RUN_ID"),
               "selection": list(SELECTED_RW), "results": results,
               "conversation_count": len(results), "automatic_reruns": 0,
               "scope": "Real model and canonical reads/proposals on rollback fixtures. No appointment writes executed."}
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if any(r["status"] != "PASS" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
