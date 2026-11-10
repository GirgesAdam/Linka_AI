from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

MIGRATION_RELATIVE_PATH = "backend/alembic/versions/0097_member_page_permissions.py"
EXPECTED_PREVIOUS_REVISION = "0096_patient_customer_id"
EXPECTED_FINAL_REVISION = "0097_member_page_permissions"
ALLOWED_ENVIRONMENTS = frozenset({"staging", "production"})


class PreflightError(ValueError):
    """A production dispatch did not satisfy its fail-closed preflight."""


def _git_stdout(workspace: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise PreflightError("Unable to verify the checked-out source commit.")
    return result.stdout


def validate_preflight(
    *,
    selected_environment: str,
    github_ref: str,
    run_attempt: str | int,
    github_sha: str,
    expected_main_sha: str,
    expected_migration_sha256: str,
    expected_current_revision: str,
    workspace: Path,
) -> dict[str, str]:
    if selected_environment not in ALLOWED_ENVIRONMENTS:
        raise PreflightError("The selected migration environment is not allowed.")

    if selected_environment == "staging":
        return {
            "selected_environment": "staging",
            "source_sha": github_sha,
            "approved_main_sha": "",
            "migration_sha256": "",
            "expected_previous_revision": "",
            "migration_target": "alembic upgrade head",
        }

    if github_ref != "refs/heads/main":
        raise PreflightError("Production migrations may only be dispatched from main.")

    if str(run_attempt) != "1":
        raise PreflightError(
            "Production workflow reruns are blocked; dispatch a new run."
        )

    if not re.fullmatch(r"[0-9a-f]{40}", github_sha):
        raise PreflightError("The workflow source SHA is invalid.")

    if not re.fullmatch(r"[0-9a-f]{40}", expected_main_sha):
        raise PreflightError("A valid approved Main SHA is required.")

    if expected_main_sha != github_sha:
        raise PreflightError(
            "The approved Main SHA does not match the workflow source SHA."
        )

    actual_head = (
        _git_stdout(workspace, "rev-parse", "--verify", "HEAD")
        .decode("ascii", errors="strict")
        .strip()
    )
    if actual_head != github_sha:
        raise PreflightError(
            "The checked-out source SHA does not match the workflow source SHA."
        )

    if expected_current_revision != EXPECTED_PREVIOUS_REVISION:
        raise PreflightError("The expected previous Alembic revision is not approved.")

    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_migration_sha256):
        raise PreflightError("A valid approved Migration 0097 SHA-256 is required.")

    try:
        migration_blob = _git_stdout(
            workspace,
            "show",
            f"{github_sha}:{MIGRATION_RELATIVE_PATH}",
        )
    except PreflightError as exc:
        raise PreflightError(
            "Migration 0097 is absent from the approved source commit."
        ) from exc

    actual_migration_sha256 = hashlib.sha256(migration_blob).hexdigest()
    if actual_migration_sha256 != expected_migration_sha256.lower():
        raise PreflightError(
            "Migration 0097 SHA-256 does not match the approved value."
        )

    return {
        "selected_environment": "production",
        "source_sha": github_sha,
        "approved_main_sha": expected_main_sha,
        "migration_sha256": actual_migration_sha256,
        "expected_previous_revision": EXPECTED_PREVIOUS_REVISION,
        "migration_target": f"alembic upgrade {EXPECTED_FINAL_REVISION}",
    }


def _write_outputs(values: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as output:
        for key, value in values.items():
            print(f"{key}={value}", file=output)


def main() -> int:
    try:
        values = validate_preflight(
            selected_environment=os.environ.get("SELECTED_ENVIRONMENT", ""),
            github_ref=os.environ.get("GITHUB_REF", ""),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            github_sha=os.environ.get("GITHUB_SHA", ""),
            expected_main_sha=os.environ.get("EXPECTED_MAIN_SHA", ""),
            expected_migration_sha256=os.environ.get("EXPECTED_MIGRATION_SHA256", ""),
            expected_current_revision=os.environ.get("EXPECTED_CURRENT_REVISION", ""),
            workspace=Path(os.environ.get("GITHUB_WORKSPACE", Path.cwd())),
        )
        _write_outputs(values)
    except PreflightError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    print(f"Migration preflight passed for {values['selected_environment']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
