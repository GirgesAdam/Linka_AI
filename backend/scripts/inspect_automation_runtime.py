from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.database.session import SessionLocal
from app.models.automation_job import AutomationJob
from app.models.automation_rule import AutomationRule
from app.models.automation_worker import AutomationWorker
from app.services.operational_readiness import (
    AUTOMATION_WORKER_HEARTBEAT_MINUTES,
    STALE_LOCK_MINUTES,
    _is_explicit_test_job,
    _is_explicit_test_rule,
    _is_explicit_test_worker,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only automation worker/job diagnostics."
    )
    parser.add_argument("--workspace-id", required=True)
    args = parser.parse_args()

    workspace_id = UUID(args.workspace_id)
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=STALE_LOCK_MINUTES)
    fresh_after = now - timedelta(
        minutes=AUTOMATION_WORKER_HEARTBEAT_MINUTES
    )

    with SessionLocal() as db:
        workers = list(
            db.scalars(
                select(AutomationWorker)
                .where(AutomationWorker.workspace_id == workspace_id)
                .order_by(AutomationWorker.created_at)
            )
        )
        rules = {
            rule.id: rule
            for rule in db.scalars(
                select(AutomationRule).where(
                    AutomationRule.workspace_id == workspace_id
                )
            )
        }
        jobs = list(
            db.scalars(
                select(AutomationJob)
                .where(
                    AutomationJob.workspace_id == workspace_id,
                    AutomationJob.status == "processing",
                    AutomationJob.locked_at.is_not(None),
                    AutomationJob.locked_at <= stale_before,
                )
                .order_by(AutomationJob.locked_at)
            )
        )

    runtime_rules = [
        rule for rule in rules.values()
        if rule.enabled and not _is_explicit_test_rule(rule)
    ]
    test_rules = [
        rule for rule in rules.values()
        if rule.enabled and _is_explicit_test_rule(rule)
    ]

    print("=== Linka AUTOMATION RUNTIME DIAGNOSTIC ===")
    print()
    print(
        "Enabled rules: "
        f"runtime={len(runtime_rules)} explicit_test={len(test_rules)}"
    )
    print()
    print("Workers:")
    if not workers:
        print("- none")

    fresh_runtime_workers = []
    for worker in workers:
        test_worker = _is_explicit_test_worker(worker)
        fresh = (
            worker.status == "active"
            and worker.last_seen_at is not None
            and worker.last_seen_at >= fresh_after
        )
        if fresh and not test_worker:
            fresh_runtime_workers.append(worker)
        print(
            f"- name={worker.name!r} status={worker.status} "
            f"last_seen_at={worker.last_seen_at} fresh={fresh} "
            f"test_artifact={test_worker}"
        )

    print()
    print(f"Stale processing jobs (>={STALE_LOCK_MINUTES}m): {len(jobs)}")
    runtime_jobs = []
    test_jobs = []

    for job in jobs:
        rule = rules.get(job.rule_id)
        test_artifact = _is_explicit_test_job(job) or (
            rule is not None and _is_explicit_test_rule(rule)
        )
        if test_artifact:
            test_jobs.append(job)
        else:
            runtime_jobs.append(job)

        age_minutes = (
            int((now - job.locked_at).total_seconds() // 60)
            if job.locked_at is not None
            else None
        )
        print(
            "- "
            f"job_id={job.id} "
            f"rule={getattr(rule, 'key', None)!r} "
            f"attempts={job.attempts} "
            f"lock_age_minutes={age_minutes} "
            f"test_artifact={test_artifact} "
            f"dedupe_key={job.dedupe_key!r}"
        )

    print()
    if runtime_jobs:
        if runtime_rules and not fresh_runtime_workers:
            print(
                "Diagnosis: real runtime jobs are stale and no fresh runtime "
                "automation worker heartbeat exists. Verify the n8n scheduler."
            )
        else:
            print(
                "Diagnosis: real runtime stale jobs exist. Inspect n8n tick/execute "
                "behavior before changing their database state."
            )
    elif test_jobs:
        print(
            "Diagnosis: all stale jobs are explicit staging/regression test "
            "artifacts. They can be removed with the staging-only test-artifact "
            "cleanup script."
        )
    elif runtime_rules and not fresh_runtime_workers:
        print(
            "Diagnosis: runtime automation rules are enabled but no fresh runtime "
            "worker heartbeat exists. Verify the n8n scheduler."
        )
    elif not runtime_rules:
        print(
            "Diagnosis: no runtime automation rule is enabled. Test rules/workers "
            "do not count as production automation health."
        )
    else:
        print("Diagnosis: automation runtime has no stale processing job.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
