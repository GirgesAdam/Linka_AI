from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.database.session import SessionLocal
from app.services.operational_readiness import build_workspace_operational_readiness


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Linka workspace production-readiness gate."
    )
    parser.add_argument("--workspace-id", required=True)
    args = parser.parse_args()

    workspace_id = UUID(args.workspace_id)
    print("=== Linka v0.17.0 PRODUCTION READINESS GATE ===")

    with SessionLocal() as db:
        result = build_workspace_operational_readiness(
            db,
            workspace_id=workspace_id,
        )

    for check in result.checks:
        label = {
            "pass": "PASS",
            "warn": "WARN",
            "fail": "FAIL",
        }[check.severity]
        print(f"[{label}] {check.key} — {check.message}")

    print()
    print("=== SUMMARY ===")
    print(
        f"STATUS={result.status.upper()} "
        f"PASS={result.pass_count} WARN={result.warn_count} FAIL={result.fail_count}"
    )
    print(
        "AI:",
        result.provider["provider"],
        "| agent:",
        result.provider["agent_model"],
        "| onboarding:",
        result.provider["onboarding_primary_model"],
        "->",
        result.provider["onboarding_fallback_model"],
    )

    return 2 if result.fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
