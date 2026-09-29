from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess, get_workspace_admin
from app.database.session import get_db
from app.models.automation_job import AutomationJob
from app.models.crm_task import CRMTask
from app.services.activity import record_activity_event
from app.services.crm_tasks import CRMTaskError, validate_assignee

router = APIRouter()


class CRMTaskExecutorUpdate(BaseModel):
    executor: Literal["tia", "staff", "unassigned"]
    assigned_user_id: UUID | None = None


class CRMTaskExecutorRead(BaseModel):
    task_id: UUID
    execution_mode: Literal["ai", "human"]
    assigned_user_id: UUID | None


def _follow_up_job(db: Session, *, workspace_id: UUID, task_id: UUID) -> AutomationJob | None:
    return db.scalar(
        select(AutomationJob)
        .where(
            AutomationJob.workspace_id == workspace_id,
            AutomationJob.crm_task_id == task_id,
            AutomationJob.job_kind == "crm_follow_up",
        )
        .with_for_update()
    )


def _cancel_pending_ai_job(job: AutomationJob | None, *, reason: str) -> None:
    if job is None:
        return
    job.status = "cancelled"
    job.locked_at = None
    job.next_attempt_at = None
    job.completed_at = datetime.now(UTC)
    job.result_json = {**(job.result_json or {}), "reason": reason}


def _queue_ai_job(db: Session, *, task: CRMTask, job: AutomationJob | None) -> None:
    if job is not None:
        job.status = "queued"
        job.scheduled_for = task.due_at
        job.locked_at = None
        job.next_attempt_at = None
        job.completed_at = None
        job.last_error = None
        job.message_id = None
        job.dispatch_id = None
        job.result_json = {}
        return

    db.add(
        AutomationJob(
            workspace_id=task.workspace_id,
            rule_id=None,
            appointment_id=None,
            crm_task_id=task.id,
            patient_id=task.patient_id,
            job_kind="crm_follow_up",
            status="queued",
            scheduled_for=task.due_at,
            dedupe_key=f"crm-followup:{task.id}",
            attempts=0,
            payload_json={"crm_task_id": str(task.id)},
            result_json={},
        )
    )


@router.patch("/tasks/{task_id}/executor", response_model=CRMTaskExecutorRead)
def update_task_executor(
    task_id: UUID,
    payload: CRMTaskExecutorUpdate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> CRMTaskExecutorRead:
    task = db.scalar(
        select(CRMTask)
        .where(
            CRMTask.workspace_id == access.workspace.id,
            CRMTask.id == task_id,
        )
        .with_for_update()
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    if task.status not in {"pending", "in_progress"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Completed or cancelled follow-ups cannot be reassigned.",
        )
    if task.task_type != "follow_up" and payload.executor == "tia":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only follow-up tasks can be executed by Linka.",
        )

    previous_mode = task.execution_mode
    previous_assignee = task.assigned_user_id
    job = _follow_up_job(db, workspace_id=task.workspace_id, task_id=task.id)
    if job is not None and job.status in {"processing", "dispatched"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This follow-up is already being sent or was already dispatched, so its executor cannot be changed.",
        )

    if payload.executor == "tia":
        if task.status != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A follow-up can only be handed back to Linka before execution starts.",
            )
        task.execution_mode = "ai"
        task.assigned_user_id = None
        _queue_ai_job(db, task=task, job=job)
    elif payload.executor == "staff":
        if payload.assigned_user_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Select a team member before assigning this follow-up.",
            )
        try:
            validate_assignee(
                db,
                workspace_id=task.workspace_id,
                user_id=payload.assigned_user_id,
            )
        except CRMTaskError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        task.execution_mode = "human"
        task.assigned_user_id = payload.assigned_user_id
        _cancel_pending_ai_job(job, reason="staff_assignment")
    else:
        task.execution_mode = "human"
        task.assigned_user_id = None
        _cancel_pending_ai_job(job, reason="unassigned_by_admin")

    record_activity_event(
        db,
        workspace_id=task.workspace_id,
        actor_type="staff",
        actor_user_id=access.user.id,
        action="crm_task.executor_changed",
        entity_type="crm_task",
        entity_id=task.id,
        summary="CRM follow-up executor changed",
        metadata={
            "previous_execution_mode": previous_mode,
            "execution_mode": task.execution_mode,
            "previous_assigned_user_id": previous_assignee,
            "assigned_user_id": task.assigned_user_id,
        },
    )
    db.commit()
    db.refresh(task)
    return CRMTaskExecutorRead(
        task_id=task.id,
        execution_mode=task.execution_mode,
        assigned_user_id=task.assigned_user_id,
    )
