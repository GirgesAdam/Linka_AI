from __future__ import annotations

import run_ra_g5_followup_matrix as followup


def main() -> int:
    last = None
    for attempt in range(1, 6):
        try:
            result = followup.run_grouped_p4_proof()
            print(f"RA_G5_P4_PROOF_PASS_ATTEMPT={attempt}", flush=True)
            print(f"RA_G5_P4_PROOF_PROPOSAL={result['proposal'] is not None}", flush=True)
            return 0
        except Exception as exc:
            last = exc
            print(f"RA_G5_P4_PROOF_ATTEMPT_FAILURE={attempt}:{type(exc).__name__}:{exc}", flush=True)
    raise AssertionError(f"P4 proof failed after retries: {last}")


if __name__ == "__main__":
    raise SystemExit(main())
