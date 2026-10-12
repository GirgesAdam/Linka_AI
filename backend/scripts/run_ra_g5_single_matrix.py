from __future__ import annotations

import json
import run_ra_g5_followup_matrix as followup
import run_grouped_multiservice_intent_live_review as live


def main() -> int:
    outputs = []
    failures = []
    for name, mode in (("M4", "option_ref"), ("M5", "matching_time"), ("M6", "new_time")):
        last = None
        for attempt in range(1, 6):
            try:
                result = followup.run_single(name, mode)
                result["attempt"] = attempt
                outputs.append(result)
                break
            except Exception as exc:
                last = exc
                print(
                    "RA_G5_SINGLE_ATTEMPT_FAILURE="
                    + json.dumps(
                        {
                            "case": name,
                            "attempt": attempt,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "raw": list(live.RAW),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    flush=True,
                )
        else:
            failures.append({"case": name, "error": str(last)})
    print(
        "RA_G5_SINGLE_SUMMARY="
        + json.dumps(
            {item["case"]: item["proposal_status"] for item in outputs}
            | {"failures": failures},
            separators=(",", ":"),
        ),
        flush=True,
    )
    if failures:
        raise AssertionError(f"single-service comparator failures: {failures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
