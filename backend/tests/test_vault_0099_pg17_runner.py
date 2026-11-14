"""CI opt-in runner for local-only Migration 0099 financial invariants.

VAULT_PG17_TEST_DATABASE_URL must be an isolated PostgreSQL 17 test target
already migrated to 0099. Set VAULT_PG17_REQUIRED=1 in the dedicated CI job.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_vault_0099_pg17_real_services():
    if not os.environ.get("VAULT_PG17_TEST_DATABASE_URL"):
        if os.environ.get("VAULT_PG17_REQUIRED") == "1":
            pytest.fail("Vault 0099 PG17 job must supply a disposable localhost test DB.")
        pytest.skip("Requires explicitly provisioned isolated PostgreSQL 17.6 at revision 0099.")
    file = Path(__file__).parent / "integration" / "vault_0099_pg17_rehearsal.py"
    run = subprocess.run(
        [sys.executable, str(file)],
        cwd=file.parents[2],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert run.returncode == 0, (
        "Local PG17 vault financial harness failed. "
        "STDOUT:\n" + run.stdout[-5000:] + "\nSTDERR:\n" + run.stderr[-5000:]
    )
    required_markers = (
        "SYNTHETIC_CI_FINANCIAL_FIXTURE_SEEDED",
        "ELECTRONIC_PREACTIVATION_BEFORE_ANY_ACCOUNT_PENDING_PASS",
        "SYNTHETIC_TWO_BRANCH_SAME_PATIENT_PG17_FIXTURE_READY",
        "SINGLE_OPENING_BALANCE_DB_CONSTRAINT_PASS",
        "ELECTRONIC_PREACTIVATION_AUDITED_NO_DOUBLE_CREDIT_PASS",
        "OPENING_BALANCE_ATTESTATION_CAP_GUARD_PASS",
        "ELECTRONIC_POSTACTIVATION_CONCURRENT_RETRY_PASS",
        "POSTED_APPOINTMENT_IMMUTABLE_PREPOSTING_CORRECTIONS_PASS",
        "BOUNDED_DB_FIRST_SEEN_CLOCK_SKEW_GUARD_PASS",
        "LEGACY_SOURCE_EXCLUSION_PASS",
        "ELECTRONIC_CHANNELS_POST_WITHOUT_BRANCH_PASS",
        "PRE_ACTIVATION_NO_UNAPPROVED_CASH_BACKFILL_PASS",
        "POSTED_ALLOCATION_EXCESS_INSERT_GUARD_PASS",
        "POSTED_ALLOCATION_UPDATE_DELETE_GUARDS_PASS",
        "CROSS_PATIENT_ALLOCATION_FAIL_CLOSED_PASS",
        "MULTIBRANCH_PENDING_FAIL_CLOSED_PASS",
        "TWO_CONCURRENT_FULL_TRANSFERS_PASS",
        "TWO_CONCURRENT_EXPENSES_PASS",
        "EXPENSE_VS_TRANSFER_RACE_PASS",
        "CASH_REFUND_VS_TRANSFER_RACE_PASS",
        "PENDING_RESOLUTION_CONCURRENCY_PASS",
        "DUPLICATE_KEY_AND_LOST_RESPONSE_RETRY_PASS",
        "FAILED_REFUND_FULL_ROLLBACK_PASS",
        "LOCAL_PG17_SERVICE_CONCURRENCY_ALL_PASS",
    )
    missing = [marker for marker in required_markers if marker not in run.stdout]
    assert not missing, f"Required financial scenarios not executed: {missing}"
    print(f"VAULT_FINANCIAL_POSTGRES_17_6: {len(required_markers)} mandatory markers PASS")
