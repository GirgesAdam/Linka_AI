from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.automation_job import AutomationJob
from app.models.conversation import Conversation
from app.models.crm_task import CRMTask
from app.models.lead import Lead
from app.models.patient import Patient
from app.models.workspace_member import WorkspaceMember
from app.services.activity import record_activity_event

ACTIVE_TASK_STATUSES = ("pending", "in_progress")
TERMINAL_TASK_STATUSES = ("completed", "cancelled")
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"in_progress", "completed", "cancelled"}),
    "in_progress": frozenset({"pending", "completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


class CRMTaskError(ValueError):
    pass


class CRMTaskConflict(CRMTaskError):
    pass


class CRMTaskPermissionError(PermissionError):
    pass


def _active_ai_followup_job(db: Session, *, task: CRMTask) -> AutomationJob | None:
    return db.scalar(
        select(AutomationJob).where(
            AutomationJob.workspace_id == task.workspace_id,
            AutomationJob.crm_task_id == task.id,
            AutomationJob.job_kind == "crm_follow_up",
            AutomationJob.status.in_(("queued", "processing", "failed")),
        )
    )


def _cancel_ai_followup_job(db: Session, *, task: CRMTask, reason: str) -> None:
    job = _active_ai_followup_job(db, task=task)
    if job is None:
        return
    job.status = "cancelled"
    job.locked_at = None
    job.next_attempt_at = None
    job.completed_at = datetime.now(UTC)
    job.result_json = {**(job.result_json or {}), "reason": reason}


def _ensure_ai_followup_job(db: Session, *, task: CRMTask) -> AutomationJob:
    existing = db.scalar(
        select(AutomationJob).where(
            AutomationJob.workspace_id == task.workspace_id,
            AutomationJob.crm_task_id == task.id,
            AutomationJob.job_kind == "crm_follow_up",
        )
    )
    if existing is not None:
        if existing.status in {"queued", "failed"}:
            existing.scheduled_for = task.due_at
            existing.next_attempt_at = None
        return existing
    job = AutomationJob(
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
    db.add(job)
    db.flush([job])
    return job


def is_active_workspace_member(db: Session, *, workspace_id: UUID, user_id: UUID) -> bool:
    return (
        db.scalar(
            select(WorkspaceMember.id).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.is_active.is_(True),
            )
        )
        is not None
    )


def validate_assignee(db: Session, *, workspace_id: UUID, user_id: UUID | None) -> None:
    if user_id is None:
        return
    if not is_active_workspace_member(db, workspace_id=workspace_id, user_id=user_id):
        raise CRMTaskError("Assigned user is not an active member of this workspace.")


def validate_task_links(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    lead_id: UUID | None,
    conversation_id: UUID | None,
) -> None:
    patient = db.scalar(
        select(Patient.id).where(
            Patient.workspace_id == workspace_id,
            Patient.id == patient_id,
        )
    )
    if patient is None:
        raise CRMTaskError("Patient not found in this workspace.")

    if lead_id is not None:
        lead = db.scalar(
            select(Lead).where(
                Lead.workspace_id == workspace_id,
                Lead.id == lead_id,
            )
        )
        if lead is None:
            raise CRMTaskError("Lead not found in this workspace.")
        if lead.patient_id != patient_id:
            raise CRMTaskError("Lead does not belong to the selected patient.")

    if conversation_id is not None:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.workspace_id == workspace_id,
                Conversation.id == conversation_id,
            )
        )
        if conversation is None:
            raise CRMTaskError("Conversation not found in this workspace.")
        if conversation.patient_id != patient_id:
            raise CRMTaskError("Conversation does not belong to the selected patient.")


def sync_lead_next_follow_up(
    db: Session,
    *,
    workspace_id: UUID,
    lead_id: UUID | None,
) -> None:
    if lead_id is None:
        return
    lead = db.scalar(
        select(Lead).where(
            Lead.workspace_id == workspace_id,
            Lead.id == lead_id,
        )
    )
    if lead is None:
        return
    next_due = db.scalar(
        select(func.min(CRMTask.due_at)).where(
            CRMTask.workspace_id == workspace_id,
            CRMTask.lead_id == lead_id,
            CRMTask.task_type == "follow_up",
            CRMTask.status.in_(ACTIVE_TASK_STATUSES),
        )
    )
    lead.next_follow_up_at = next_due


def replace_legacy_lead_follow_up(
    db: Session,
    *,
    lead: Lead,
    due_at: datetime | None,
    actor_user_id: UUID | None,
) -> None:
    active_tasks = list(
        db.scalars(
            select(CRMTask).where(
                CRMTask.workspace_id == lead.workspace_id,
                CRMTask.lead_id == lead.id,
                CRMTask.task_type == "follow_up",
                CRMTask.status.in_(ACTIVE_TASK_STATUSES),
            )
        )
    )
    now = datetime.now(UTC)
    for task in active_tasks:
        task.status = "cancelled"
        task.updated_at = now
    if due_at is not None:
        create_crm_task(
            db,
            workspace_id=lead.workspace_id,
            patient_id=lead.patient_id,
            lead_id=lead.id,
            assigned_user_id=lead.assigned_user_id,
            created_by_user_id=actor_user_id,
            task_type="follow_up",
            priority="normal",
            title="Lead follow-up",
            description=None,
            due_at=due_at,
            source="manual" if actor_user_id else "system",
            commit=False,
        )
    sync_lead_next_follow_up(db, workspace_id=lead.workspace_id, lead_id=lead.id)


def create_crm_task(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    title: str,
    due_at: datetime,
    task_type: str = "follow_up",
    priority: str = "normal",
    description: str | None = None,
    lead_id: UUID | None = None,
    conversation_id: UUID | None = None,
    assigned_user_id: UUID | None = None,
    created_by_user_id: UUID | None = None,
    source: str = "manual",
    execution_mode: str = "human",
    dedupe_key: str | None = None,
    commit: bool = True,
) -> CRMTask:
    title = title.strip()
    if not title:
        raise CRMTaskError("Task title cannot be empty.")
    if len(title) > 200:
        raise CRMTaskError("Task title is too long.")
    if description is not None and len(description) > 5000:
        raise CRMTaskError("Task description is too long.")
    if due_at.tzinfo is None or due_at.utcoffset() is None:
        raise CRMTaskError("Task due_at must include a timezone offset.")
    if task_type not in {"follow_up", "general"}:
        raise CRMTaskError("Unsupported task type.")
    if priority not in {"low", "normal", "high", "urgent"}:
        raise CRMTaskError("Unsupported task priority.")
    if source not in {"manual", "ai", "system"}:
        raise CRMTaskError("Unsupported task source.")
    if execution_mode not in {"human", "ai"}:
        raise CRMTaskError("Unsupported task execution mode.")
    if execution_mode == "ai" and task_type != "follow_up":
        raise CRMTaskError("Only follow-up tasks can be executed by Linka.")
    if execution_mode == "ai" and assigned_user_id is not None:
        raise CRMTaskError("AI follow-ups cannot be assigned to a staff member at the same time.")

    validate_task_links(
        db,
        workspace_id=workspace_id,
        patient_id=patient_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
    )
    validate_assignee(db, workspace_id=workspace_id, user_id=assigned_user_id)

    if dedupe_key:
        existing = db.scalar(
            select(CRMTask).where(
                CRMTask.workspace_id == workspace_id,
                CRMTask.dedupe_key == dedupe_key,
            )
        )
        if existing is not None:
            return existing

    task = CRMTask(
        workspace_id=workspace_id,
        patient_id=patient_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        assigned_user_id=assigned_user_id,
        created_by_user_id=created_by_user_id,
        completed_by_user_id=None,
        task_type=task_type,
        source=source,
        status="pending",
        execution_mode=execution_mode,
        priority=priority,
        title=title,
        description=description.strip() if description and description.strip() else None,
        due_at=due_at.astimezone(UTC),
        completed_at=None,
        dedupe_key=dedupe_key,
    )

    if dedupe_key:
        # Keep an idempotency race local to this insert. A duplicate AI retry must
        # not roll back unrelated state already present in the surrounding agent
        # transaction. PostgreSQL's unique key remains the final authority.
        savepoint = db.begin_nested()
        db.add(task)
        try:
            db.flush([task])
            savepoint.commit()
        except IntegrityError as exc:
            savepoint.rollback()
            existing = db.scalar(
                select(CRMTask).where(
                    CRMTask.workspace_id == workspace_id,
                    CRMTask.dedupe_key == dedupe_key,
                )
            )
            if existing is not None:
                return existing
            raise CRMTaskConflict(
                "Task could not be created because of a concurrent conflict."
            ) from exc
    else:
        db.add(task)
        try:
            db.flush([task])
        except IntegrityError as exc:
            db.rollback()
            raise CRMTaskConflict(
                "Task could not be created because of a concurrent conflict."
            ) from exc

    if task.execution_mode == "ai":
        _ensure_ai_followup_job(db, task=task)
    sync_lead_next_follow_up(db, workspace_id=workspace_id, lead_id=lead_id)
    actor_type = (
        "staff"
        if created_by_user_id is not None
        else (source if source in {"ai", "system"} else "system")
    )
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type=actor_type,
        actor_user_id=created_by_user_id,
        action="crm_task.created",
        entity_type="crm_task",
        entity_id=task.id,
        summary="CRM task created",
        metadata={
            "task_type": task.task_type,
            "execution_mode": task.execution_mode,
            "priority": task.priority,
            "assigned_user_id": task.assigned_user_id,
            "due_at": task.due_at,
        },
    )
    if not commit:
        return task
    db.commit()
    db.refresh(task)
    return task


def assert_task_actor_can_manage(
    task: CRMTask,
    *,
    actor_user_id: UUID,
    actor_is_admin: bool,
) -> None:
    if actor_is_admin:
        return
    if task.assigned_user_id == actor_user_id:
        return
    raise CRMTaskPermissionError("Only the assigned staff member or an admin can update this task.")


def update_crm_task(
    db: Session,
    *,
    task: CRMTask,
    actor_user_id: UUID,
    actor_is_admin: bool,
    updates: dict,
    commit: bool = True,
) -> CRMTask:
    assert_task_actor_can_manage(
        task,
        actor_user_id=actor_user_id,
        actor_is_admin=actor_is_admin,
    )
    if task.status in TERMINAL_TASK_STATUSES and updates:
        raise CRMTaskConflict("Completed or cancelled tasks cannot be modified.")

    previous_status = task.status
    previous_assigned_user_id = task.assigned_user_id
    previous_execution_mode = task.execution_mode

    if "assigned_user_id" in updates:
        new_assignee = updates["assigned_user_id"]
        if not actor_is_admin and new_assignee != actor_user_id:
            raise CRMTaskPermissionError("Only admins can assign a task to another team member.")
        validate_assignee(db, workspace_id=task.workspace_id, user_id=new_assignee)
        if task.execution_mode == "ai" and new_assignee is not None:
            task.execution_mode = "human"
            _cancel_ai_followup_job(db, task=task, reason="staff_assignment")

    if "title" in updates:
        title = str(updates["title"] or "").strip()
        if not title:
            raise CRMTaskError("Task title cannot be empty.")
        if len(title) > 200:
            raise CRMTaskError("Task title is too long.")
        updates["title"] = title
    if "description" in updates:
        value = updates["description"]
        updates["description"] = value.strip() if isinstance(value, str) and value.strip() else None
    if "due_at" in updates:
        due_at = updates["due_at"]
        if due_at is None or due_at.tzinfo is None or due_at.utcoffset() is None:
            raise CRMTaskError("Task due_at must include a timezone offset.")
        updates["due_at"] = due_at.astimezone(UTC)

    if "due_at" in updates and task.execution_mode == "ai":
        job = _active_ai_followup_job(db, task=task)
        if job is not None and job.status in {"queued", "failed"}:
            job.scheduled_for = updates["due_at"]
            job.next_attempt_at = None

    new_status = updates.get("status")
    if new_status is not None and new_status != task.status:
        allowed = _ALLOWED_TRANSITIONS.get(task.status, frozenset())
        if new_status not in allowed:
            raise CRMTaskConflict(f"Task cannot transition from {task.status} to {new_status}.")
        if new_status == "completed":
            task.completed_at = datetime.now(UTC)
            task.completed_by_user_id = actor_user_id
        if new_status in TERMINAL_TASK_STATUSES and task.execution_mode == "ai":
            _cancel_ai_followup_job(db, task=task, reason=f"task_{new_status}")

    for key, value in updates.items():
        setattr(task, key, value)

    if updates:
        action = (
            f"crm_task.{task.status}"
            if task.status != previous_status and task.status in TERMINAL_TASK_STATUSES
            else "crm_task.updated"
        )
        summary = (
            "CRM task completed"
            if task.status == "completed" and task.status != previous_status
            else (
                "CRM task cancelled"
                if task.status == "cancelled" and task.status != previous_status
                else "CRM task updated"
            )
        )
        record_activity_event(
            db,
            workspace_id=task.workspace_id,
            actor_type="staff",
            actor_user_id=actor_user_id,
            action=action,
            entity_type="crm_task",
            entity_id=task.id,
            summary=summary,
            metadata={
                "changed_fields": sorted(updates),
                "from_status": previous_status,
                "to_status": task.status,
                "previous_assigned_user_id": previous_assigned_user_id,
                "assigned_user_id": task.assigned_user_id,
                "previous_execution_mode": previous_execution_mode,
                "execution_mode": task.execution_mode,
            },
        )

    sync_lead_next_follow_up(db, workspace_id=task.workspace_id, lead_id=task.lead_id)
    if commit:
        db.commit()
        db.refresh(task)
    else:
        db.flush()
    return task


def claim_crm_task(
    db: Session,
    *,
    task: CRMTask,
    actor_user_id: UUID,
    commit: bool = True,
) -> CRMTask:
    if task.status not in ACTIVE_TASK_STATUSES:
        raise CRMTaskConflict("Only active tasks can be claimed.")
    if task.assigned_user_id is not None and task.assigned_user_id != actor_user_id:
        raise CRMTaskConflict("Task is already assigned to another team member.")
    validate_assignee(db, workspace_id=task.workspace_id, user_id=actor_user_id)
    changed = task.assigned_user_id != actor_user_id or task.execution_mode == "ai"
    if task.execution_mode == "ai":
        task.execution_mode = "human"
        _cancel_ai_followup_job(db, task=task, reason="staff_claim")
    task.assigned_user_id = actor_user_id
    if changed:
        record_activity_event(
            db,
            workspace_id=task.workspace_id,
            actor_type="staff",
            actor_user_id=actor_user_id,
            action="crm_task.claimed",
            entity_type="crm_task",
            entity_id=task.id,
            summary="CRM task claimed",
            metadata={"execution_mode": task.execution_mode},
        )
    if commit:
        db.commit()
        db.refresh(task)
    else:
        db.flush()
    return task


def reconcile_ai_followup_dispatch(
    db: Session,
    *,
    dispatch,
    message,
) -> None:
    """Project provider delivery state back onto an automatic follow-up task.

    The outbox remains the authority for provider retries. A successful provider
    acceptance completes the follow-up; a terminal failure/cancellation hands the
    still-needed follow-up back to staff instead of silently losing it.
    """
    metadata = dispatch.metadata_json or {}
    raw_task_id = metadata.get("crm_task_id")
    if not raw_task_id:
        return
    try:
        task_id = UUID(str(raw_task_id))
    except (TypeError, ValueError):
        return
    task = db.scalar(
        select(CRMTask)
        .where(
            CRMTask.workspace_id == dispatch.workspace_id,
            CRMTask.id == task_id,
        )
        .with_for_update()
    )
    if task is None or task.execution_mode != "ai":
        return

    if dispatch.status in {"sent", "delivered", "read"}:
        if task.status in ACTIVE_TASK_STATUSES:
            task.status = "completed"
            task.completed_at = dispatch.sent_at or datetime.now(UTC)
            task.completed_by_user_id = None
        sync_lead_next_follow_up(db, workspace_id=task.workspace_id, lead_id=task.lead_id)
        return

    if dispatch.status in {"failed", "cancelled"}:
        conversation = db.get(Conversation, message.conversation_id) if message is not None else None
        task.execution_mode = "human"
        task.status = "pending"
        task.completed_at = None
        task.completed_by_user_id = None
        if conversation is not None and conversation.owner_type == "human":
            task.assigned_user_id = conversation.assigned_user_id
        sync_lead_next_follow_up(db, workspace_id=task.workspace_id, lead_id=task.lead_id)
