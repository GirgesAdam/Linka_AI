from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime

from sqlalchemy import select

from app.database.session import SessionLocal
from app.models.automation_rule import AutomationRule
from app.models.automation_worker import AutomationWorker
from app.models.clinic_integration_sync import ClinicIntegrationSyncSchedule
from app.models.workspace import Workspace
from app.services.automation_template_rotation import sync_approved_automation_template_rotation
from app.services.automations import (
    AutomationError,
    PlanningResult,
    automation_planning_may_have_work,
    claim_due_jobs,
    ensure_default_rules,
    execute_job,
    generate_worker_token,
    plan_automation_jobs,
)
from app.services.clinic_integration_sync_runtime import run_scheduled_sync_tick
from app.services.demo_reset import DemoResetError, scheduled_demo_reset_state
from app.services.workspace_runtime_policy import workspace_runtime_policy

logger = logging.getLogger("tia.automation_scheduler")

RUNTIME_WORKER_NAME = "Linka Railway Automation Scheduler"
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_JOB_LIMIT = 20
DEFAULT_PLANNING_HORIZON_DAYS = 14
DEFAULT_SYNC_PAGE_SIZE = 100
DEFAULT_SYNC_MAX_PAGES = 10


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default
    return max(minimum, min(value, maximum))


def _heartbeat(db, workspace_id, *, now: datetime) -> None:
    worker = db.scalar(
        select(AutomationWorker).where(
            AutomationWorker.workspace_id == workspace_id,
            AutomationWorker.name == RUNTIME_WORKER_NAME,
        )
    )
    if worker is None:
        _, token_hash = generate_worker_token()
        worker = AutomationWorker(
            workspace_id=workspace_id,
            name=RUNTIME_WORKER_NAME,
            token_hash=token_hash,
            status="active",
            created_by_user_id=None,
        )
        db.add(worker)
    else:
        worker.status = "active"
    worker.last_seen_at = now
    db.commit()


def _retire_booking_confirmation(db, workspace_id) -> None:
    legacy_rule = db.scalar(
        select(AutomationRule).where(
            AutomationRule.workspace_id == workspace_id,
            AutomationRule.key == "booking_confirmation",
        )
    )
    if legacy_rule is not None and legacy_rule.enabled:
        legacy_rule.enabled = False
        db.commit()


def run_workspace_tick(
    workspace_id,
    *,
    job_limit: int = DEFAULT_JOB_LIMIT,
    planning_horizon_days: int = DEFAULT_PLANNING_HORIZON_DAYS,
    sync_page_size: int = DEFAULT_SYNC_PAGE_SIZE,
    sync_max_pages: int = DEFAULT_SYNC_MAX_PAGES,
) -> None:
    with SessionLocal() as db:
        workspace = db.get(Workspace, workspace_id)
        if workspace is None or not workspace.is_active:
            return

        now = datetime.now(UTC)
        if workspace.is_demo:
            try:
                reset_state = scheduled_demo_reset_state(db, workspace=workspace, now=now)
            except DemoResetError:
                db.rollback()
                logger.exception(
                    "Daily demo reset failed workspace=%s",
                    workspace.id,
                )
            else:
                if reset_state == "reset":
                    logger.info("Daily demo reset completed workspace=%s", workspace.id)
                    workspace = db.get(Workspace, workspace.id)
                    if workspace is None or not workspace.is_active:
                        return
                elif reset_state == "missed_window":
                    logger.warning(
                        "Daily demo reset window missed workspace=%s; no daytime catch-up will run",
                        workspace.id,
                    )

        _heartbeat(db, workspace.id, now=now)
        _retire_booking_confirmation(db, workspace.id)

        # Default rules must exist before we can sync the approved template pool.
        # Rotation is system-owned; admins still only control rule enablement/timing.
        rules = ensure_default_rules(db, workspace.id)
        sync_approved_automation_template_rotation(db, workspace_id=workspace.id)

        planning_skipped_preflight = not automation_planning_may_have_work(
            db,
            workspace_id=workspace.id,
            rules=rules,
            planning_horizon_days=planning_horizon_days,
            now=now,
        )
        planning = (
            PlanningResult(planned=0, cancelled=0)
            if planning_skipped_preflight
            else plan_automation_jobs(
                db,
                workspace_id=workspace.id,
                planning_horizon_days=planning_horizon_days,
                now=now,
                rules=rules,
            )
        )
        claimed = claim_due_jobs(
            db,
            workspace_id=workspace.id,
            limit=job_limit,
        )

        executed = 0
        failed = 0
        for claimed_job in claimed:
            try:
                execute_job(
                    db,
                    workspace_id=workspace.id,
                    job_id=claimed_job.job_id,
                )
                executed += 1
            except AutomationError as exc:
                db.rollback()
                failed += 1
                logger.warning(
                    "Automation job %s could not execute: %s",
                    claimed_job.job_id,
                    exc,
                )
            except Exception:
                db.rollback()
                failed += 1
                logger.exception(
                    "Unexpected automation execution failure for job %s",
                    claimed_job.job_id,
                )

        sync_claimed = False
        sync_reason = "sync_disabled"
        policy = workspace_runtime_policy(workspace)
        schedule = db.get(ClinicIntegrationSyncSchedule, workspace.id)
        if schedule is not None and schedule.enabled:
            if not policy.allow_external_sync:
                sync_reason = "external_sync_blocked_for_demo"
            else:
                sync = run_scheduled_sync_tick(
                    db,
                    workspace=workspace,
                    page_size=sync_page_size,
                    max_pages_per_domain=sync_max_pages,
                )
                sync_claimed = sync.claimed
                sync_reason = sync.reason or (sync.cycle.status if sync.cycle else None)

        logger.info(
            "automation_tick workspace=%s planning_skipped_preflight=%s planned=%s cancelled=%s claimed=%s executed=%s failed=%s sync_claimed=%s sync_reason=%s",
            workspace.id,
            int(planning_skipped_preflight),
            planning.planned,
            planning.cancelled,
            len(claimed),
            executed,
            failed,
            sync_claimed,
            sync_reason,
        )


def run_once() -> None:
    job_limit = _int_env(
        "AUTOMATION_JOB_LIMIT",
        DEFAULT_JOB_LIMIT,
        minimum=1,
        maximum=100,
    )
    planning_horizon_days = _int_env(
        "AUTOMATION_PLANNING_HORIZON_DAYS",
        DEFAULT_PLANNING_HORIZON_DAYS,
        minimum=1,
        maximum=90,
    )
    sync_page_size = _int_env(
        "AUTOMATION_SYNC_PAGE_SIZE",
        DEFAULT_SYNC_PAGE_SIZE,
        minimum=1,
        maximum=500,
    )
    sync_max_pages = _int_env(
        "AUTOMATION_SYNC_MAX_PAGES",
        DEFAULT_SYNC_MAX_PAGES,
        minimum=1,
        maximum=100,
    )

    with SessionLocal() as db:
        workspace_ids = list(
            db.scalars(
                select(Workspace.id)
                .where(Workspace.is_active.is_(True))
                .order_by(Workspace.created_at)
            )
        )

    for workspace_id in workspace_ids:
        try:
            run_workspace_tick(
                workspace_id,
                job_limit=job_limit,
                planning_horizon_days=planning_horizon_days,
                sync_page_size=sync_page_size,
                sync_max_pages=sync_max_pages,
            )
        except Exception:
            logger.exception("Automation scheduler tick failed for workspace %s", workspace_id)


async def run_forever() -> None:
    interval_seconds = _int_env(
        "AUTOMATION_TICK_INTERVAL_SECONDS",
        DEFAULT_INTERVAL_SECONDS,
        minimum=15,
        maximum=3600,
    )
    logger.info("Starting native Linka automation scheduler interval=%ss", interval_seconds)

    while True:
        started = time.monotonic()
        await asyncio.to_thread(run_once)
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, interval_seconds - elapsed))


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
