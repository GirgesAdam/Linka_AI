from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.api.dependencies.section_access import require_section_access
from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.database.session import get_db
from app.models.automation_job import AutomationJob
from app.models.automation_rule import AutomationRule
from app.models.automation_worker import AutomationWorker
from app.models.message_dispatch import MessageDispatch
from app.models.workspace import Workspace
from app.schemas.automation import (
    AutomationExecuteResponse,
    AutomationJobActionResponse,
    AutomationJobRead,
    AutomationJobStatus,
    AutomationOperationsOverview,
    AutomationRuleRead,
    AutomationRuleUpdate,
    AutomationTickRequest,
    AutomationTickResponse,
    AutomationWorkerCreate,
    AutomationWorkerCreated,
    AutomationWorkerRead,
    AutomationWorkerStatusUpdate,
    AutomationWorkerTokenRotated,
)
from app.schemas.clinic_integration import (
    ClinicSyncWorkerTickRequest,
    ClinicSyncWorkerTickResponse,
)
from app.services.activity import record_activity_event
from app.services.automations import (
    AUTOMATION_JOB_STALE_MINUTES,
    AutomationError,
    automation_operations_overview,
    cancel_automation_job,
    cancel_system_lead_followups,
    claim_due_jobs,
    ensure_default_rules,
    execute_job,
    generate_worker_token,
    get_worker_by_token,
    plan_automation_jobs,
    retry_automation_job,
)
from app.services.clinic_integration_sync_runtime import run_scheduled_sync_tick

router = APIRouter()


@dataclass(frozen=True)
class AutomationWorkerAccess:
    worker: AutomationWorker


def get_automation_worker_access(
    x_automation_token: Annotated[str, Header(alias="X-Automation-Token")],
    db: Annotated[Session, Depends(get_db)],
) -> AutomationWorkerAccess:
    worker = get_worker_by_token(db, x_automation_token)
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid automation worker token.",
        )
    worker.last_seen_at = datetime.now(UTC)
    db.commit()
    return AutomationWorkerAccess(worker=worker)


@router.get("/rules", response_model=list[AutomationRuleRead])
def list_rules(
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[AutomationRule]:
    return [
        rule
        for rule in ensure_default_rules(db, access.workspace.id)
        if rule.key != "booking_confirmation"
    ]


@router.patch("/rules/{rule_id}", response_model=AutomationRuleRead)
def update_rule(
    rule_id: UUID,
    payload: AutomationRuleUpdate,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> AutomationRule:
    rule = db.scalar(
        select(AutomationRule).where(
            AutomationRule.workspace_id == access.workspace.id,
            AutomationRule.id == rule_id,
        )
    )
    if rule is None or rule.key == "booking_confirmation":
        raise HTTPException(status_code=404, detail="Automation rule not found.")

    changes = payload.model_dump(exclude_unset=True)
    changed_fields = sorted(changes)
    previous_enabled = rule.enabled

    if (
        rule.key == "appointment_reminder_6h"
        and rule.template_name == "tia_reminder_6h_01"
        and "offset_minutes" in changes
        and changes["offset_minutes"] != -360
        and changes.get("template_name", rule.template_name) == "tia_reminder_6h_01"
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "The currently approved tia_reminder_6h_01 template explicitly says 6 hours. "
                "Switch to the timing-neutral reminder template before changing this timing."
            ),
        )
    if "config" in changes:
        rule.config_json = changes.pop("config") or {}
    if "template_variants" in changes:
        variants = changes.pop("template_variants")
        config = dict(rule.config_json or {})
        if variants:
            config["template_variants"] = variants
        else:
            config.pop("template_variants", None)
        rule.config_json = config
    for key, value in changes.items():
        setattr(rule, key, value)

    if payload.enabled is False:
        cancellable_job_ids = list(
            db.scalars(
                select(AutomationJob.id)
                .outerjoin(MessageDispatch, MessageDispatch.id == AutomationJob.dispatch_id)
                .where(
                    AutomationJob.workspace_id == access.workspace.id,
                    AutomationJob.rule_id == rule.id,
                    or_(
                        AutomationJob.status.in_(("queued", "failed")),
                        and_(
                            AutomationJob.status == "dispatched",
                            MessageDispatch.status == "queued",
                        ),
                    ),
                )
            )
        )
        for pending_job_id in cancellable_job_ids:
            cancelled_job, _ = cancel_automation_job(
                db,
                workspace_id=access.workspace.id,
                job_id=pending_job_id,
                actor_user_id=access.user.id,
            )
            cancelled_job.result_json = {
                **(cancelled_job.result_json or {}),
                "reason": "rule_disabled_by_admin",
            }
        if rule.key == "lead_not_booked_followup":
            cancel_system_lead_followups(
                db,
                workspace_id=access.workspace.id,
            )

    if changed_fields:
        record_activity_event(
            db,
            workspace_id=access.workspace.id,
            actor_type="staff",
            actor_user_id=access.user.id,
            action="automation.rule_updated",
            entity_type="automation_rule",
            entity_id=rule.id,
            summary="Automation rule updated",
            metadata={
                "changed_fields": changed_fields,
                "enabled_from": previous_enabled,
                "enabled_to": rule.enabled,
                "rule_key": rule.key,
            },
        )

    db.commit()
    db.refresh(rule)
    return rule


@router.get("/overview", response_model=AutomationOperationsOverview)
def automation_overview(
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> AutomationOperationsOverview:
    return automation_operations_overview(db, workspace_id=access.workspace.id)


@router.get("/jobs", response_model=list[AutomationJobRead])
def list_jobs(
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
    job_status: Annotated[AutomationJobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AutomationJobRead]:
    stmt = (
        select(AutomationJob, MessageDispatch.status, MessageDispatch.last_error)
        .outerjoin(MessageDispatch, MessageDispatch.id == AutomationJob.dispatch_id)
        .where(AutomationJob.workspace_id == access.workspace.id)
    )
    if job_status:
        stmt = stmt.where(AutomationJob.status == job_status)
    rows = db.execute(stmt.order_by(AutomationJob.created_at.desc()).limit(limit)).all()
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=AUTOMATION_JOB_STALE_MINUTES)
    result: list[AutomationJobRead] = []
    for job, dispatch_status, dispatch_last_error in rows:
        attention_reason = None
        if job.status == "failed":
            attention_reason = "execution_failed"
        elif dispatch_status == "failed":
            attention_reason = "delivery_failed"
        elif (
            job.status == "processing"
            and job.locked_at is not None
            and job.locked_at <= stale_before
        ):
            attention_reason = "stuck_processing"
        result.append(
            AutomationJobRead.model_validate(job).model_copy(
                update={
                    "dispatch_status": dispatch_status,
                    "dispatch_last_error": dispatch_last_error,
                    "attention_reason": attention_reason,
                }
            )
        )
    return result


@router.post("/jobs/{job_id}/retry", response_model=AutomationJobActionResponse)
def retry_job(
    job_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> AutomationJobActionResponse:
    try:
        job, dispatch = retry_automation_job(
            db,
            workspace_id=access.workspace.id,
            job_id=job_id,
            actor_user_id=access.user.id,
        )
    except AutomationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AutomationJobActionResponse(
        job_id=job.id,
        job_status=job.status,
        dispatch_status=dispatch.status if dispatch is not None else None,
        action="retry",
    )


@router.post("/jobs/{job_id}/cancel", response_model=AutomationJobActionResponse)
def cancel_job(
    job_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> AutomationJobActionResponse:
    try:
        job, dispatch = cancel_automation_job(
            db,
            workspace_id=access.workspace.id,
            job_id=job_id,
            actor_user_id=access.user.id,
        )
    except AutomationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AutomationJobActionResponse(
        job_id=job.id,
        job_status=job.status,
        dispatch_status=dispatch.status if dispatch is not None else None,
        action="cancel",
    )


@router.post("/workers", response_model=AutomationWorkerCreated, status_code=201)
def create_worker(
    payload: AutomationWorkerCreate,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> AutomationWorkerCreated:
    raw, token_hash = generate_worker_token()
    worker = AutomationWorker(
        workspace_id=access.workspace.id,
        name=payload.name,
        token_hash=token_hash,
        status="active",
        created_by_user_id=access.user.id,
    )
    db.add(worker)
    db.flush()
    record_activity_event(
        db,
        workspace_id=access.workspace.id,
        actor_type="staff",
        actor_user_id=access.user.id,
        action="automation.worker_created",
        entity_type="automation_worker",
        entity_id=worker.id,
        summary="Automation worker created",
        metadata={"status": worker.status},
    )
    db.commit()
    db.refresh(worker)
    return AutomationWorkerCreated(
        **AutomationWorkerRead.model_validate(worker).model_dump(),
        worker_token=raw,
    )


@router.get("/workers", response_model=list[AutomationWorkerRead])
def list_workers(
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[AutomationWorker]:
    return list(
        db.scalars(
            select(AutomationWorker)
            .where(AutomationWorker.workspace_id == access.workspace.id)
            .order_by(AutomationWorker.created_at)
        )
    )


@router.patch("/workers/{worker_id}", response_model=AutomationWorkerRead)
def update_worker_status(
    worker_id: UUID,
    payload: AutomationWorkerStatusUpdate,
    access: Annotated[WorkspaceAccess, Depends(require_section_access("administration"))],
    db: Annotated[Session, Depends(get_db)],
) -> AutomationWorker:
    worker = db.scalar(
        select(AutomationWorker).where(
            AutomationWorker.workspace_id == access.workspace.id,
            AutomationWorker.id == worker_id,
        )
    )
    if worker is None:
        raise HTTPException(status_code=404, detail="Automation worker not found.")
    previous_status = worker.status
    worker.status = payload.status
    if worker.status != previous_status:
        record_activity_event(
            db,
            workspace_id=access.workspace.id,
            actor_type="staff",
            actor_user_id=access.user.id,
            action="automation.worker_status_updated",
            entity_type="automation_worker",
            entity_id=worker.id,
            summary="Automation worker status updated",
            metadata={"from_status": previous_status, "to_status": worker.status},
        )
    db.commit()
    db.refresh(worker)
    return worker


@router.post("/workers/{worker_id}/rotate-token", response_model=AutomationWorkerTokenRotated, dependencies=[Depends(require_section_access("administration"))])
def rotate_worker_token(
    worker_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_access)],
    db: Annotated[Session, Depends(get_db)],
) -> AutomationWorkerTokenRotated:
    worker = db.scalar(
        select(AutomationWorker).where(
            AutomationWorker.workspace_id == access.workspace.id,
            AutomationWorker.id == worker_id,
        )
    )
    if worker is None:
        raise HTTPException(status_code=404, detail="Automation worker not found.")
    raw, token_hash = generate_worker_token()
    worker.token_hash = token_hash
    worker.status = "active"
    record_activity_event(
        db,
        workspace_id=access.workspace.id,
        actor_type="staff",
        actor_user_id=access.user.id,
        action="automation.worker_token_rotated",
        entity_type="automation_worker",
        entity_id=worker.id,
        summary="Automation worker token rotated",
        metadata={"status": worker.status},
    )
    db.commit()
    return AutomationWorkerTokenRotated(worker_id=worker.id, worker_token=raw)


@router.post("/adapter/tick", response_model=AutomationTickResponse)
def automation_tick(
    payload: AutomationTickRequest,
    worker_access: Annotated[AutomationWorkerAccess, Depends(get_automation_worker_access)],
    db: Annotated[Session, Depends(get_db)],
) -> AutomationTickResponse:
    workspace_id = worker_access.worker.workspace_id
    planning = plan_automation_jobs(
        db,
        workspace_id=workspace_id,
        planning_horizon_days=payload.planning_horizon_days,
    )
    claimed = claim_due_jobs(
        db,
        workspace_id=workspace_id,
        limit=payload.limit,
    )
    return AutomationTickResponse(
        planned=planning.planned,
        cancelled=planning.cancelled,
        claimed=claimed,
    )


@router.post("/adapter/clinic-sync/tick", response_model=ClinicSyncWorkerTickResponse)
def clinic_sync_tick(
    payload: ClinicSyncWorkerTickRequest,
    worker_access: Annotated[AutomationWorkerAccess, Depends(get_automation_worker_access)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicSyncWorkerTickResponse:
    workspace = db.get(Workspace, worker_access.worker.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Automation worker workspace not found.")
    return run_scheduled_sync_tick(
        db,
        workspace=workspace,
        page_size=payload.page_size,
        max_pages_per_domain=payload.max_pages_per_domain,
    )


@router.post("/adapter/jobs/{job_id}/execute", response_model=AutomationExecuteResponse)
def execute_automation_job(
    job_id: UUID,
    worker_access: Annotated[AutomationWorkerAccess, Depends(get_automation_worker_access)],
    db: Annotated[Session, Depends(get_db)],
) -> AutomationExecuteResponse:
    try:
        result = execute_job(
            db,
            workspace_id=worker_access.worker.workspace_id,
            job_id=job_id,
        )
    except AutomationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    job = result.job
    return AutomationExecuteResponse(
        job_id=job.id,
        status=job.status,
        message_id=job.message_id,
        dispatch_id=job.dispatch_id,
        reason=result.reason,
    )
