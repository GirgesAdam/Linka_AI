from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.automation_rules import DEFAULT_AUTOMATION_RULES, scheduled_for
from app.models.appointment import Appointment
from app.models.automation_job import AutomationJob
from app.models.automation_rule import AutomationRule
from app.models.automation_worker import AutomationWorker
from app.models.branch import Branch
from app.models.channel_connection import ChannelConnection
from app.models.channel_identity import ChannelIdentity
from app.models.conversation import Conversation
from app.models.crm_task import CRMTask
from app.models.doctor import Doctor
from app.models.handoff_request import HandoffRequest
from app.models.lead import Lead
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.workspace import Workspace
from app.schemas.automation import AutomationClaimedJob, AutomationOperationsOverview
from app.schemas.crm import normalize_patient_identity_phone
from app.services.activity import record_activity_event
from app.services.conversation_ownership import record_outbound_activity, return_to_ai
from app.services.crm_tasks import create_crm_task, sync_lead_next_follow_up


class AutomationError(ValueError):
    pass


AUTOMATION_WORKER_FRESH_MINUTES = 5
AUTOMATION_JOB_STALE_MINUTES = 10

LEAD_FOLLOWUP_RULE_KEY = "lead_not_booked_followup"
LEAD_FOLLOWUP_DEDUPE_PREFIX = "automation:lead-not-booked:"
LEAD_FOLLOWUP_ELIGIBLE_STATUSES = frozenset({"new", "contacted", "qualified"})
RETIRED_AUTOMATION_RULE_KEYS = frozenset({"no_show_followup"})

LEGACY_DEFAULT_TEMPLATE_NAMES: dict[str, frozenset[str]] = {
    "appointment_reminder_6h": frozenset(
        {"tia_appointment_reminder_ar", "tia_appointment_reminder_6h_ar", "tia_reminder_6h_01"}
    ),
    "post_visit_followup": frozenset({"tia_post_visit_followup_ar"}),
}


@dataclass(frozen=True)
class PlanningResult:
    planned: int
    cancelled: int


@dataclass(frozen=True)
class ExecutionResult:
    job: AutomationJob
    reason: str | None


def generate_worker_token() -> tuple[str, str]:
    raw = "tia_auto_" + secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def worker_token_hash(raw: str) -> str:
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def get_worker_by_token(db: Session, raw_token: str) -> AutomationWorker | None:
    if not raw_token or not raw_token.strip():
        return None
    return db.scalar(
        select(AutomationWorker).where(
            AutomationWorker.token_hash == worker_token_hash(raw_token),
            AutomationWorker.status == "active",
        )
    )


def ensure_default_rules(
    db: Session, workspace_id: UUID, *, commit: bool = True
) -> list[AutomationRule]:
    existing = {
        row.key: row
        for row in db.scalars(
            select(AutomationRule).where(AutomationRule.workspace_id == workspace_id)
        )
    }
    changed = False
    for retired_key in RETIRED_AUTOMATION_RULE_KEYS:
        retired = existing.get(retired_key)
        if retired is not None and retired.enabled:
            retired.enabled = False
            changed = True

    for definition in DEFAULT_AUTOMATION_RULES:
        if definition.key in existing:
            row = existing[definition.key]
            legacy_names = LEGACY_DEFAULT_TEMPLATE_NAMES.get(definition.key, frozenset())
            if row.template_name in legacy_names and row.template_name != definition.template_name:
                row.template_name = definition.template_name
                row.template_language = definition.template_language
                changed = True
            if (
                row.template_name == definition.template_name
                and row.template_language != definition.template_language
            ):
                row.template_language = definition.template_language
                changed = True
            if definition.key == "booking_confirmation" and not row.enabled:
                row.enabled = True
                changed = True
            continue
        row = AutomationRule(
            workspace_id=workspace_id,
            key=definition.key,
            name=definition.name,
            enabled=definition.enabled_by_default,
            trigger_kind=definition.trigger_kind,
            offset_minutes=definition.offset_minutes,
            channel=definition.channel,
            template_name=definition.template_name,
            template_language=definition.template_language,
            max_lateness_minutes=definition.max_lateness_minutes,
            config_json={},
        )
        db.add(row)
        existing[row.key] = row
        changed = True
    if changed:
        if commit:
            db.commit()
        else:
            db.flush()
    return sorted(existing.values(), key=lambda row: row.key)


def _job_dedupe_key(appointment_id: UUID, rule_key: str) -> str:
    return f"appointment:{appointment_id}:rule:{rule_key}"


def _cancel_pending_job_dispatch(
    db: Session,
    *,
    job: AutomationJob,
    reason: str,
) -> bool:
    """Cancel an automation message only while it is still safely queued."""
    if job.dispatch_id is None:
        return False
    dispatch = db.get(MessageDispatch, job.dispatch_id)
    if dispatch is None or dispatch.status != "queued":
        return False

    dispatch.status = "cancelled"
    dispatch.locked_at = None
    dispatch.next_attempt_at = None
    dispatch.last_error = reason[:2000]
    message = db.get(Message, dispatch.message_id)
    if message is not None and message.delivery_status == "queued":
        message.delivery_status = "cancelled"
    return True


REPLANNABLE_CANCELLATION_REASONS = {
    "rule_disabled",
    "rule_disabled_by_admin",
    "rule_disabled_or_appointment_no_longer_eligible",
}


def _cancelled_job_can_be_replanned(job: AutomationJob) -> bool:
    reason = str((job.result_json or {}).get("reason") or "")
    return job.status == "cancelled" and reason in REPLANNABLE_CANCELLATION_REASONS


def _eligible_for_rule(appointment: Appointment, rule: AutomationRule) -> bool:
    if rule.trigger_kind in {"appointment_created", "before_appointment"}:
        return appointment.status in {"pending", "confirmed"}
    if rule.trigger_kind == "after_completed":
        return appointment.status == "completed" and appointment.completed_at is not None
    if rule.trigger_kind == "after_no_show":
        return appointment.status == "no_show" and appointment.no_show_at is not None
    if rule.trigger_kind == "after_cancelled":
        return (
            (appointment.status == "cancelled" and appointment.cancelled_at is not None)
            or (appointment.status == "no_show" and appointment.no_show_at is not None)
        )
    return False


def _candidate_appointments(
    db: Session,
    *,
    workspace_id: UUID,
    rule: AutomationRule,
    now: datetime,
    horizon: datetime,
) -> list[Appointment]:
    if rule.trigger_kind in {"appointment_created", "before_appointment"}:
        appointment_horizon = horizon
        if rule.trigger_kind == "before_appointment" and rule.offset_minutes < 0:
            appointment_horizon = horizon + timedelta(minutes=abs(rule.offset_minutes))
        return list(
            db.scalars(
                select(Appointment).where(
                    Appointment.workspace_id == workspace_id,
                    Appointment.status.in_(("pending", "confirmed")),
                    Appointment.start_at > now,
                    Appointment.start_at <= appointment_horizon,
                )
            )
        )
    if rule.trigger_kind == "after_completed":
        oldest = now - max(
            timedelta(days=14),
            timedelta(minutes=max(0, rule.offset_minutes) + rule.max_lateness_minutes),
        )
        return list(
            db.scalars(
                select(Appointment).where(
                    Appointment.workspace_id == workspace_id,
                    Appointment.status == "completed",
                    Appointment.completed_at.is_not(None),
                    Appointment.completed_at >= oldest,
                )
            )
        )
    if rule.trigger_kind == "after_no_show":
        oldest = now - max(
            timedelta(days=7),
            timedelta(minutes=max(0, rule.offset_minutes) + rule.max_lateness_minutes),
        )
        return list(
            db.scalars(
                select(Appointment).where(
                    Appointment.workspace_id == workspace_id,
                    Appointment.status == "no_show",
                    Appointment.no_show_at.is_not(None),
                    Appointment.no_show_at >= oldest,
                )
            )
        )
    if rule.trigger_kind == "after_cancelled":
        oldest = now - max(
            timedelta(days=7),
            timedelta(minutes=max(0, rule.offset_minutes) + rule.max_lateness_minutes),
        )
        return list(
            db.scalars(
                select(Appointment).where(
                    Appointment.workspace_id == workspace_id,
                    or_(
                        and_(
                            Appointment.status == "cancelled",
                            Appointment.cancelled_at.is_not(None),
                            Appointment.cancelled_at >= oldest,
                        ),
                        and_(
                            Appointment.status == "no_show",
                            Appointment.no_show_at.is_not(None),
                            Appointment.no_show_at >= oldest,
                        ),
                    ),
                )
            )
        )
    return []



def _lead_followup_anchor(lead: Lead) -> datetime:
    return lead.last_contact_at or lead.created_at


def _lead_followup_dedupe_key(lead_id: UUID) -> str:
    return f"{LEAD_FOLLOWUP_DEDUPE_PREFIX}{lead_id}"


def _is_system_lead_followup_task(task: CRMTask) -> bool:
    return (
        task.source == "system"
        and task.execution_mode == "ai"
        and task.lead_id is not None
        and str(task.dedupe_key or "").startswith(LEAD_FOLLOWUP_DEDUPE_PREFIX)
    )


def _other_active_lead_followup(
    db: Session,
    *,
    workspace_id: UUID,
    lead_id: UUID,
    exclude_task_id: UUID | None = None,
) -> CRMTask | None:
    stmt = select(CRMTask).where(
        CRMTask.workspace_id == workspace_id,
        CRMTask.lead_id == lead_id,
        CRMTask.task_type == "follow_up",
        CRMTask.status.in_(("pending", "in_progress")),
    )
    if exclude_task_id is not None:
        stmt = stmt.where(CRMTask.id != exclude_task_id)
    return db.scalar(stmt.order_by(CRMTask.due_at, CRMTask.created_at).limit(1))


def _system_lead_followup_ineligible_reason(
    db: Session,
    *,
    task: CRMTask,
    for_update: bool = False,
) -> str | None:
    if not _is_system_lead_followup_task(task):
        return None

    rule_stmt = select(AutomationRule).where(
        AutomationRule.workspace_id == task.workspace_id,
        AutomationRule.key == LEAD_FOLLOWUP_RULE_KEY,
    )
    if for_update:
        rule_stmt = rule_stmt.with_for_update()
    rule = db.scalar(rule_stmt)
    if rule is None or not rule.enabled:
        return "lead_followup_rule_disabled"

    lead_stmt = select(Lead).where(
        Lead.workspace_id == task.workspace_id,
        Lead.id == task.lead_id,
    )
    if for_update:
        lead_stmt = lead_stmt.with_for_update()
    lead = db.scalar(lead_stmt)
    if lead is None or lead.patient_id != task.patient_id:
        return "lead_followup_target_missing"
    if lead.status not in LEAD_FOLLOWUP_ELIGIBLE_STATUSES:
        return "lead_no_longer_eligible"
    if _other_active_lead_followup(
        db,
        workspace_id=task.workspace_id,
        lead_id=lead.id,
        exclude_task_id=task.id,
    ) is not None:
        return "lead_followup_superseded_by_existing_task"
    return None


def _cancel_system_lead_followup_task(
    db: Session,
    *,
    task: CRMTask,
    now: datetime,
    reason: str,
) -> bool:
    if task.status not in {"pending", "in_progress"}:
        return False
    job = db.scalar(
        select(AutomationJob)
        .where(
            AutomationJob.workspace_id == task.workspace_id,
            AutomationJob.crm_task_id == task.id,
            AutomationJob.job_kind == "crm_follow_up",
        )
        .with_for_update()
    )
    if job is not None and job.status == "dispatched":
        if not _cancel_pending_job_dispatch(db, job=job, reason=reason):
            return False
    if job is not None and job.status in {"queued", "failed", "processing", "dispatched"}:
        job.status = "cancelled"
        job.locked_at = None
        job.next_attempt_at = None
        job.completed_at = now
        job.result_json = {**(job.result_json or {}), "reason": reason}
    task.status = "cancelled"
    task.completed_at = now
    sync_lead_next_follow_up(
        db,
        workspace_id=task.workspace_id,
        lead_id=task.lead_id,
    )
    return True


def cancel_system_lead_followups(
    db: Session,
    *,
    workspace_id: UUID,
    now: datetime | None = None,
) -> int:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    tasks = list(
        db.scalars(
            select(CRMTask).where(
                CRMTask.workspace_id == workspace_id,
                CRMTask.source == "system",
                CRMTask.execution_mode == "ai",
                CRMTask.status.in_(("pending", "in_progress")),
                CRMTask.dedupe_key.like(f"{LEAD_FOLLOWUP_DEDUPE_PREFIX}%"),
            )
        )
    )
    cancelled = 0
    for task in tasks:
        if _cancel_system_lead_followup_task(
            db,
            task=task,
            now=now,
            reason="lead_followup_rule_disabled",
        ):
            cancelled += 1
    return cancelled


def _plan_lead_followup_rule(
    db: Session,
    *,
    workspace_id: UUID,
    rule: AutomationRule,
    now: datetime,
    horizon: datetime,
) -> PlanningResult:
    active_system_tasks = list(
        db.scalars(
            select(CRMTask).where(
                CRMTask.workspace_id == workspace_id,
                CRMTask.source == "system",
                CRMTask.execution_mode == "ai",
                CRMTask.dedupe_key.like(f"{LEAD_FOLLOWUP_DEDUPE_PREFIX}%"),
                CRMTask.status.in_(("pending", "in_progress")),
            )
        )
    )
    cancelled = 0
    for task in active_system_tasks:
        reason = _system_lead_followup_ineligible_reason(db, task=task)
        if reason is not None and _cancel_system_lead_followup_task(
            db,
            task=task,
            now=now,
            reason=reason,
        ):
            cancelled += 1

    if not rule.enabled:
        return PlanningResult(planned=0, cancelled=cancelled)

    oldest = now - max(
        timedelta(days=30),
        timedelta(minutes=max(0, rule.offset_minutes) + rule.max_lateness_minutes),
    )
    leads = list(
        db.scalars(
            select(Lead).where(
                Lead.workspace_id == workspace_id,
                Lead.status.in_(tuple(LEAD_FOLLOWUP_ELIGIBLE_STATUSES)),
                or_(Lead.created_at >= oldest, Lead.last_contact_at >= oldest),
            )
        )
    )
    planned = 0
    for lead in leads:
        anchor = _lead_followup_anchor(lead)
        when = (anchor + timedelta(minutes=rule.offset_minutes)).astimezone(UTC)
        if when > horizon or when + timedelta(minutes=rule.max_lateness_minutes) < now:
            continue

        dedupe_key = _lead_followup_dedupe_key(lead.id)
        task = db.scalar(
            select(CRMTask).where(
                CRMTask.workspace_id == workspace_id,
                CRMTask.dedupe_key == dedupe_key,
            )
        )
        if task is not None:
            competing = _other_active_lead_followup(
                db,
                workspace_id=workspace_id,
                lead_id=lead.id,
                exclude_task_id=task.id,
            )
            if competing is not None:
                continue
            job = db.scalar(
                select(AutomationJob).where(
                    AutomationJob.workspace_id == workspace_id,
                    AutomationJob.crm_task_id == task.id,
                    AutomationJob.job_kind == "crm_follow_up",
                )
            )
            renewable = (
                task.status == "cancelled"
                and job is not None
                and job.status == "cancelled"
                and str((job.result_json or {}).get("reason") or "")
                == "lead_followup_rule_disabled"
            )
            if renewable:
                task.status = "pending"
                task.execution_mode = "ai"
                task.completed_at = None
                task.due_at = when
                job.status = "queued"
                job.scheduled_for = when
                job.locked_at = None
                job.next_attempt_at = None
                job.completed_at = None
                job.last_error = None
                job.message_id = None
                job.dispatch_id = None
                job.result_json = {}
                sync_lead_next_follow_up(db, workspace_id=workspace_id, lead_id=lead.id)
            elif task.status == "pending" and task.execution_mode == "ai" and job is not None:
                task.due_at = when
                if job.status in {"queued", "failed"}:
                    job.status = "queued"
                    job.scheduled_for = when
                    job.next_attempt_at = None
                    job.locked_at = None
                    job.last_error = None
                sync_lead_next_follow_up(db, workspace_id=workspace_id, lead_id=lead.id)
            continue

        if _other_active_lead_followup(
            db,
            workspace_id=workspace_id,
            lead_id=lead.id,
        ) is not None:
            continue

        create_crm_task(
            db,
            workspace_id=workspace_id,
            patient_id=lead.patient_id,
            lead_id=lead.id,
            title="مساعدة في استكمال الحجز",
            description=(
                "تابع مع العميل بشكل طبيعي لأنه مهتم ولم يكمل الحجز بعد. "
                "جاوب على أسئلته واعرض المساعدة في الحجز بدون اختراع خصم أو استعجال غير حقيقي."
            ),
            due_at=when,
            task_type="follow_up",
            priority="normal",
            source="system",
            execution_mode="ai",
            dedupe_key=dedupe_key,
            commit=False,
        )
        planned += 1
    return PlanningResult(planned=planned, cancelled=cancelled)

def plan_automation_jobs(
    db: Session,
    *,
    workspace_id: UUID,
    planning_horizon_days: int = 14,
    now: datetime | None = None,
) -> PlanningResult:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    horizon = now + timedelta(days=planning_horizon_days)
    rules = ensure_default_rules(db, workspace_id, commit=False)
    planned = 0
    cancelled = 0

    lead_rules = [rule for rule in rules if rule.trigger_kind == "after_lead_activity"]
    for lead_rule in lead_rules:
        lead_result = _plan_lead_followup_rule(
            db,
            workspace_id=workspace_id,
            rule=lead_rule,
            now=now,
            horizon=horizon,
        )
        planned += lead_result.planned
        cancelled += lead_result.cancelled

    enabled_rules = [
        rule
        for rule in rules
        if rule.enabled and rule.trigger_kind != "after_lead_activity"
    ]
    for rule in enabled_rules:
        for appointment in _candidate_appointments(
            db,
            workspace_id=workspace_id,
            rule=rule,
            now=now,
            horizon=horizon,
        ):
            when = scheduled_for(
                trigger_kind=rule.trigger_kind,
                offset_minutes=rule.offset_minutes,
                appointment_created_at=appointment.created_at,
                appointment_start_at=appointment.start_at,
                completed_at=appointment.completed_at,
                no_show_at=appointment.no_show_at,
                cancelled_at=appointment.cancelled_at or appointment.no_show_at,
            )
            if when is None:
                continue
            when = when.astimezone(UTC)

            latest_allowed = when + timedelta(minutes=rule.max_lateness_minutes)
            if latest_allowed < now:
                continue

            dedupe_key = _job_dedupe_key(appointment.id, rule.key)
            existing = db.scalar(
                select(AutomationJob).where(
                    AutomationJob.workspace_id == workspace_id,
                    AutomationJob.dedupe_key == dedupe_key,
                )
            )
            if existing is not None:
                renewable_cancel = _cancelled_job_can_be_replanned(existing)
                if existing.status in {"queued", "failed"} or renewable_cancel:
                    existing.status = "queued"
                    existing.scheduled_for = when
                    existing.next_attempt_at = None
                    existing.locked_at = None
                    existing.completed_at = None
                    existing.last_error = None
                    if renewable_cancel:
                        existing.message_id = None
                        existing.dispatch_id = None
                    existing.result_json = {}
                elif existing.status == "dispatched" and existing.scheduled_for != when:
                    if _cancel_pending_job_dispatch(
                        db,
                        job=existing,
                        reason="Appointment timing changed before provider send.",
                    ):
                        existing.status = "queued"
                        existing.scheduled_for = when
                        existing.next_attempt_at = None
                        existing.locked_at = None
                        existing.completed_at = None
                        existing.last_error = None
                        existing.message_id = None
                        existing.dispatch_id = None
                        existing.result_json = {"reason": "rescheduled_before_provider_send"}
                # Manual job cancellations stay terminal. Lifecycle cancellations
                # are renewable only when the rule/appointment becomes eligible again.
                continue

            db.add(
                AutomationJob(
                    workspace_id=workspace_id,
                    rule_id=rule.id,
                    appointment_id=appointment.id,
                    patient_id=appointment.patient_id,
                    status="queued",
                    scheduled_for=when,
                    dedupe_key=dedupe_key,
                    attempts=0,
                    payload_json={
                        "rule_key": rule.key,
                        "appointment_status_at_plan": appointment.status,
                    },
                    result_json={},
                )
            )
            planned += 1

    active_rule_ids = {rule.id for rule in enabled_rules}
    stale_stmt = (
        select(AutomationJob, Appointment)
        .join(
            Appointment,
            and_(
                Appointment.workspace_id == AutomationJob.workspace_id,
                Appointment.id == AutomationJob.appointment_id,
            ),
        )
        .where(
            AutomationJob.workspace_id == workspace_id,
            AutomationJob.status.in_(("queued", "failed", "dispatched")),
        )
    )
    for job, appointment in db.execute(stale_stmt).all():
        rule = db.get(AutomationRule, job.rule_id)
        if (
            rule is None
            or rule.id not in active_rule_ids
            or not _eligible_for_rule(appointment, rule)
        ):
            if job.status == "dispatched" and not _cancel_pending_job_dispatch(
                db,
                job=job,
                reason="Appointment or rule became ineligible before provider send.",
            ):
                # A processing/sent provider delivery cannot be safely recalled.
                continue
            job.status = "cancelled"
            job.locked_at = None
            job.next_attempt_at = None
            job.completed_at = now
            job.result_json = {"reason": "rule_disabled_or_appointment_no_longer_eligible"}
            cancelled += 1

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Another planner may have inserted the same dedupe key concurrently.
        # A retry on the next scheduler tick is safe.
    return PlanningResult(planned=planned, cancelled=cancelled)


def claim_due_jobs(
    db: Session,
    *,
    workspace_id: UUID,
    limit: int,
    now: datetime | None = None,
) -> list[AutomationClaimedJob]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    stale_before = now - timedelta(minutes=10)

    stmt = (
        select(AutomationJob)
        .where(
            AutomationJob.workspace_id == workspace_id,
            AutomationJob.scheduled_for <= now,
            or_(
                and_(
                    AutomationJob.status.in_(("queued", "failed")),
                    or_(
                        AutomationJob.next_attempt_at.is_(None),
                        AutomationJob.next_attempt_at <= now,
                    ),
                ),
                and_(
                    AutomationJob.status == "processing",
                    AutomationJob.locked_at.is_not(None),
                    AutomationJob.locked_at <= stale_before,
                ),
            ),
        )
        .order_by(AutomationJob.scheduled_for, AutomationJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    jobs = list(db.scalars(stmt))
    claimed: list[AutomationClaimedJob] = []

    for job in jobs:
        rule_key: str | None = None
        if job.job_kind == "appointment_rule":
            rule = db.get(AutomationRule, job.rule_id)
            if rule is None or not rule.enabled:
                job.status = "cancelled"
                job.completed_at = now
                job.result_json = {"reason": "rule_disabled"}
                continue
            rule_key = rule.key
        elif job.job_kind == "crm_follow_up":
            task = db.get(CRMTask, job.crm_task_id)
            if (
                task is None
                or task.execution_mode != "ai"
                or task.task_type != "follow_up"
                or task.status not in {"pending", "in_progress"}
            ):
                job.status = "cancelled"
                job.completed_at = now
                job.result_json = {"reason": "follow_up_no_longer_ai_eligible"}
                continue
            if task.due_at > now:
                job.status = "queued"
                job.scheduled_for = task.due_at
                job.locked_at = None
                continue
        else:
            job.status = "cancelled"
            job.completed_at = now
            job.result_json = {"reason": "unsupported_job_kind"}
            continue

        job.status = "processing"
        job.attempts += 1
        job.locked_at = now
        job.last_error = None
        job.next_attempt_at = None
        claimed.append(
            AutomationClaimedJob(
                job_id=job.id,
                job_kind=job.job_kind,
                rule_key=rule_key,
                appointment_id=job.appointment_id,
                crm_task_id=job.crm_task_id,
                patient_id=job.patient_id,
                scheduled_for=job.scheduled_for,
                attempt=job.attempts,
            )
        )

    db.commit()
    return claimed

def _resolve_timezone(workspace: Workspace, branch: Branch | None) -> ZoneInfo:
    name = (branch.timezone if branch and branch.timezone else workspace.timezone) or "Africa/Cairo"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Africa/Cairo")


def _appointment_display_data(
    db: Session,
    *,
    workspace: Workspace,
    appointment: Appointment,
    patient: Patient,
) -> dict:
    branch = db.get(Branch, appointment.branch_id)
    service = db.get(Service, appointment.service_id)
    doctor = db.get(Doctor, appointment.doctor_id)
    staff = db.get(Staff, doctor.staff_id) if doctor is not None else None

    tz = _resolve_timezone(workspace, branch)
    local_start = appointment.start_at.astimezone(tz)

    doctor_name = "الدكتور المتاح"
    if staff is not None:
        doctor_name = f"{staff.first_name or ''} {staff.last_name or ''}".strip() or doctor_name

    return {
        "patient_name": (f"{patient.first_name or ''} {patient.last_name or ''}".strip() or patient.first_name or "العميل"),
        "service_name": service.name if service else "الخدمة",
        "branch_name": branch.name if branch else "الفرع",
        "doctor_name": doctor_name,
        "date": local_start.strftime("%d/%m/%Y"),
        "time": local_start.strftime("%H:%M"),
        "timezone": tz.key,
    }


def _fallback_text(rule_key: str, data: dict) -> str:
    if rule_key == "booking_confirmation":
        return (
            f"تمام يا {data['patient_name']}، حجز {data['service_name']} اتسجل يوم "
            f"{data['date']} الساعة {data['time']} في {data['branch_name']}. "
            "لو حابة تغيّري أي حاجة ابعتيلي هنا."
        )
    if rule_key == "appointment_reminder_6h":
        return (
            f"أهلًا {data['patient_name']} 👋 بفكرك إن عندك جلسة {data['service_name']} "
            f"الساعة {data['time']}. مستنيينك 💛"
        )
    # Legacy rules are kept readable for already-stored audit/history rows, but
    # v0.31.3 disables them and new workspaces no longer materialize them.
    if rule_key == "appointment_reminder_24h":
        return (
            f"أهلًا {data['patient_name']} 👋 بفكرك بموعدك لـ{data['service_name']} "
            f"يوم {data['date']} الساعة {data['time']} في {data['branch_name']}. "
            "لو محتاجة تعدّلي الموعد ابعتيلي هنا."
        )
    if rule_key == "appointment_reminder_2h":
        return (
            f"أهلًا {data['patient_name']}، فاضل تقريبًا ساعتين على جلسة "
            f"{data['service_name']} الساعة {data['time']} في {data['branch_name']}. مستنيينك 💛"
        )
    if rule_key == "post_visit_followup":
        return (
            f"إزيك {data['patient_name']}؟ حبيت أطمن عليكي بعد {data['service_name']} "
            f"اللي كانت يوم {data['date']}. كل حاجة تمام؟ "
            "لو محتاجة مساعدة أو حابة تحجزي الجلسة الجاية ابعتيلي هنا، "
            "ويسعدنا نعرف تقييمك للجلسة."
        )
    if rule_key == "cancellation_recovery":
        return (
            f"أهلًا {data['patient_name']}، حبيت أساعدك بعد إلغاء موعد {data['service_name']} "
            f"يوم {data['date']} الساعة {data['time']}. "
            "لو حابة نرتب ميعاد جديد ابعتيلي هنا وأنا أساعدك."
        )
    if rule_key == "no_show_followup":
        return (
            f"أهلًا {data['patient_name']}، لاحظت إن ميعاد {data['service_name']} فات. "
            "لو حابة نرتب ميعاد تاني ابعتيلي هنا وأنا أساعدك."
        )
    return "عندك تحديث جديد من Linka."


def _rule_template_candidates(rule: AutomationRule) -> list[tuple[str, str]]:
    """Return de-duplicated approved templates that share the rule's variable contract."""
    candidates: list[tuple[str, str]] = [(rule.template_name, rule.template_language)]
    raw_variants = (rule.config_json or {}).get("template_variants")
    if isinstance(raw_variants, list):
        for raw in raw_variants:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            language = str(raw.get("language_code") or rule.template_language).strip()
            if not name or not language:
                continue
            candidate = (name, language)
            if (
                rule.key == "appointment_reminder_6h"
                and name == "tia_reminder_6h_01"
                and rule.offset_minutes != -360
            ):
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _select_rule_template(rule: AutomationRule, appointment_id: UUID) -> tuple[str, str, int]:
    """Choose one stable template per appointment/rule so retries never change the copy."""
    candidates = _rule_template_candidates(rule)
    digest = hashlib.sha256(f"{appointment_id}:{rule.key}".encode()).digest()
    index = int.from_bytes(digest[:8], "big") % len(candidates)
    name, language = candidates[index]
    return name, language, len(candidates)


def _appointment_template_body_parameters(
    rule_key: str, data: dict, *, template_name: str | None = None
) -> list[str]:
    """Return the exact positional variables required by each approved Meta template."""
    patient_name = str(data.get("patient_name") or "العميل")[:256]
    service_name = str(data.get("service_name") or "الخدمة")[:256]
    date = str(data.get("date") or "-")[:64]
    time = str(data.get("time") or "-")[:64]
    branch_name = str(data.get("branch_name") or "العيادة")[:256]

    if rule_key == "appointment_reminder_6h":
        # Temporary compatibility for the already-approved Meta template while
        # the timing-neutral 3-variable replacement is still under review.
        if template_name == "tia_reminder_6h_01":
            return [patient_name, service_name, time, branch_name]
        return [patient_name, service_name, time]
    if rule_key == "cancellation_recovery":
        return [patient_name, service_name, date, time]
    if rule_key == "post_visit_followup":
        return [patient_name, service_name, date]

    # Opt-in / legacy appointment templates retain the original five-variable contract.
    return [patient_name, service_name, date, time, branch_name]


def _config_flag_enabled(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _proactive_whatsapp_recipient(patient: Patient) -> str | None:
    """Return the provider recipient id only when the CRM phone is globally routable."""
    try:
        _, normalized = normalize_patient_identity_phone(
            patient.phone_normalized or patient.phone
        )
    except ValueError:
        return None
    if not normalized or not normalized.startswith("+"):
        return None
    digits = normalized[1:]
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        return None
    return digits


def _real_proactive_whatsapp_connection(connection: ChannelConnection) -> bool:
    config = connection.config_json or {}
    return (
        connection.channel == "whatsapp"
        and connection.status == "active"
        and bool(connection.external_account_id)
        and str(config.get("runtime_kind") or "").strip().lower() == "real"
        and not _config_flag_enabled(config.get("mock"))
        and not _config_flag_enabled(config.get("do_not_send"))
    )


def _resolve_external_route(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    channel: str,
    now: datetime,
) -> tuple[Conversation, ChannelConnection] | None:
    channel_filter = [] if channel == "auto" else [ChannelConnection.channel == channel]

    conversation = db.scalar(
        select(Conversation)
        .join(
            ChannelConnection,
            and_(
                ChannelConnection.workspace_id == Conversation.workspace_id,
                ChannelConnection.id == Conversation.channel_connection_id,
            ),
        )
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.patient_id == patient_id,
            Conversation.channel_connection_id.is_not(None),
            ChannelConnection.status == "active",
            *channel_filter,
        )
        .order_by(
            Conversation.last_message_at.desc().nulls_last(),
            Conversation.started_at.desc(),
        )
        .limit(1)
    )
    if conversation is not None:
        connection = db.get(ChannelConnection, conversation.channel_connection_id)
        if connection is not None:
            return conversation, connection

    identity = db.scalar(
        select(ChannelIdentity)
        .join(
            ChannelConnection,
            and_(
                ChannelConnection.workspace_id == ChannelIdentity.workspace_id,
                ChannelConnection.id == ChannelIdentity.channel_connection_id,
            ),
        )
        .where(
            ChannelIdentity.workspace_id == workspace_id,
            ChannelIdentity.patient_id == patient_id,
            ChannelConnection.status == "active",
            *channel_filter,
        )
        .order_by(ChannelIdentity.updated_at.desc())
        .limit(1)
    )
    if identity is None:
        if channel not in {"auto", "whatsapp"}:
            return None
        patient = db.get(Patient, patient_id)
        if patient is None:
            return None
        recipient = _proactive_whatsapp_recipient(patient)
        if recipient is None:
            return None

        proactive_connections = [
            candidate
            for candidate in db.scalars(
                select(ChannelConnection).where(
                    ChannelConnection.workspace_id == workspace_id,
                    ChannelConnection.channel == "whatsapp",
                    ChannelConnection.status == "active",
                )
            )
            if _real_proactive_whatsapp_connection(candidate)
        ]
        # For a patient without an established identity, routing must be unambiguous.
        # Multi-number workspaces can still route through an existing identity/conversation.
        if len(proactive_connections) != 1:
            return None
        connection = proactive_connections[0]

        recipient_identity = db.scalar(
            select(ChannelIdentity).where(
                ChannelIdentity.workspace_id == workspace_id,
                ChannelIdentity.channel_connection_id == connection.id,
                ChannelIdentity.external_user_id == recipient,
            )
        )
        if recipient_identity is not None:
            if recipient_identity.patient_id != patient_id:
                # Never send to a phone already owned by another CRM patient.
                return None
            identity = recipient_identity
        else:
            display_name = (
                f"{patient.first_name or ''} {patient.last_name or ''}".strip()
                or patient.first_name
                or None
            )
            identity = ChannelIdentity(
                workspace_id=workspace_id,
                channel_connection_id=connection.id,
                patient_id=patient_id,
                external_user_id=recipient,
                display_name=display_name,
                phone=patient.phone,
                metadata_json={
                    "source": "crm_patient_phone",
                    "proactive_identity": True,
                },
            )
            db.add(identity)
            db.flush()
    else:
        connection = db.get(ChannelConnection, identity.channel_connection_id)
        if connection is None:
            return None

    conversation = Conversation(
        workspace_id=workspace_id,
        patient_id=patient_id,
        channel=connection.channel,
        status="open",
        external_conversation_id=identity.external_user_id,
        channel_connection_id=connection.id,
        started_at=now,
        last_message_at=None,
    )
    db.add(conversation)
    db.flush()
    return conversation, connection



def _followup_active_handoff(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
) -> HandoffRequest | None:
    return db.scalar(
        select(HandoffRequest).where(
            HandoffRequest.workspace_id == workspace_id,
            HandoffRequest.conversation_id == conversation_id,
            HandoffRequest.status.in_(("pending", "claimed")),
        )
    )


def _resolve_followup_route(
    db: Session,
    *,
    task: CRMTask,
    now: datetime,
) -> tuple[Conversation, ChannelConnection] | None:
    if task.conversation_id is not None:
        conversation = db.scalar(
            select(Conversation)
            .where(
                Conversation.workspace_id == task.workspace_id,
                Conversation.id == task.conversation_id,
                Conversation.patient_id == task.patient_id,
            )
            .with_for_update()
        )
        if conversation is None or conversation.channel_connection_id is None:
            return None
        connection = db.get(ChannelConnection, conversation.channel_connection_id)
        if (
            connection is None
            or connection.status != "active"
            or connection.channel != "whatsapp"
        ):
            return None
        return conversation, connection

    route = _resolve_external_route(
        db,
        workspace_id=task.workspace_id,
        patient_id=task.patient_id,
        channel="whatsapp",
        now=now,
    )
    if route is None:
        return None
    conversation, connection = route
    locked = db.scalar(
        select(Conversation)
        .where(
            Conversation.workspace_id == task.workspace_id,
            Conversation.id == conversation.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        return None
    return locked, connection


def _handoff_followup_to_staff(
    *,
    task: CRMTask,
    job: AutomationJob,
    conversation: Conversation | None,
    reason: str,
    now: datetime,
) -> ExecutionResult:
    task.execution_mode = "human"
    task.status = "pending"
    task.completed_at = None
    task.completed_by_user_id = None
    if conversation is not None and conversation.owner_type == "human":
        task.assigned_user_id = conversation.assigned_user_id
    job.status = "skipped"
    job.completed_at = now
    job.locked_at = None
    job.next_attempt_at = None
    job.result_json = {"reason": reason, "fallback": "human_task"}
    return ExecutionResult(job=job, reason=reason)


def _conversation_outbound_busy(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
) -> bool:
    return (
        db.scalar(
            select(MessageDispatch.id)
            .join(Message, Message.id == MessageDispatch.message_id)
            .where(
                Message.workspace_id == workspace_id,
                Message.conversation_id == conversation_id,
                Message.direction == "outbound",
                MessageDispatch.status.in_(("queued", "processing")),
            )
            .limit(1)
        )
        is not None
    )


def _latest_patient_inbound_at(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
) -> datetime | None:
    return db.scalar(
        select(Message.created_at)
        .where(
            Message.workspace_id == workspace_id,
            Message.conversation_id == conversation_id,
            Message.sender_type == "patient",
            Message.direction == "inbound",
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )


def _whatsapp_customer_service_window_open(
    *,
    latest_patient_inbound_at: datetime | None,
    now: datetime,
) -> bool:
    if latest_patient_inbound_at is None:
        return False
    inbound = latest_patient_inbound_at
    if inbound.tzinfo is None:
        inbound = inbound.replace(tzinfo=UTC)
    return inbound.astimezone(UTC) >= now.astimezone(UTC) - timedelta(hours=24)


def _ai_followup_template_candidates(connection: ChannelConnection) -> list[tuple[str, str]]:
    config = connection.config_json or {}
    candidates: list[tuple[str, str]] = []
    raw_pool = config.get("ai_followup_templates")
    if isinstance(raw_pool, list):
        for raw in raw_pool:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            language = str(raw.get("language_code") or "ar").strip() or "ar"
            candidate = (name[:512], language[:32])
            if name and candidate not in candidates:
                candidates.append(candidate)
    legacy = config.get("ai_followup_template")
    if isinstance(legacy, dict):
        name = str(legacy.get("name") or "").strip()
        language = str(legacy.get("language_code") or "ar").strip() or "ar"
        candidate = (name[:512], language[:32])
        if name and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _latest_ai_followup_template_name(
    db: Session, *, workspace_id: UUID, conversation_id: UUID
) -> str | None:
    messages = db.scalars(
        select(Message)
        .where(
            Message.workspace_id == workspace_id,
            Message.conversation_id == conversation_id,
            Message.direction == "outbound",
            Message.message_type == "template",
        )
        .order_by(Message.created_at.desc())
        .limit(10)
    )
    for message in messages:
        metadata = message.metadata_json or {}
        if metadata.get("source") != "ai_followup":
            continue
        raw = metadata.get("whatsapp_template")
        if isinstance(raw, dict):
            name = str(raw.get("name") or "").strip()
            if name:
                return name
    return None


def _select_ai_followup_template(
    connection: ChannelConnection,
    *,
    task_id: UUID,
    patient_id: UUID,
    previous_template: str | None = None,
) -> tuple[str, str] | None:
    candidates = _ai_followup_template_candidates(connection)
    if not candidates:
        return None
    digest = hashlib.sha256(f"{patient_id}:{task_id}".encode()).digest()
    index = int.from_bytes(digest[:8], "big") % len(candidates)
    if len(candidates) > 1 and previous_template and candidates[index][0] == previous_template:
        index = (index + 1) % len(candidates)
    return candidates[index]

def _dispatch_ai_followup_template(
    db: Session,
    *,
    workspace_id: UUID,
    task: CRMTask,
    job: AutomationJob,
    conversation: Conversation,
    connection: ChannelConnection,
    template_name: str,
    template_language: str,
    now: datetime,
) -> ExecutionResult:
    if conversation.status == "closed":
        return_to_ai(conversation, close=False, now=now)

    patient = db.get(Patient, task.patient_id)
    workspace = db.get(Workspace, workspace_id)
    patient_name = (
        f"{patient.first_name or ''} {patient.last_name or ''}".strip()
        if patient is not None
        else "العميل"
    ) or "العميل"
    clinic_name = workspace.name if workspace is not None else "العيادة"
    local_now = now
    if workspace is not None:
        try:
            local_now = now.astimezone(ZoneInfo(workspace.timezone or "Africa/Cairo"))
        except ZoneInfoNotFoundError:
            local_now = now.astimezone(ZoneInfo("Africa/Cairo"))
    template_body_parameters = [
        patient_name[:256],
        task.title[:256],
        local_now.strftime("%d/%m/%Y"),
        local_now.strftime("%H:%M"),
        clinic_name[:256],
    ]

    metadata = {
        "source": "ai_followup",
        "crm_task_id": str(task.id),
        "automation_job_id": str(job.id),
        "follow_up": {
            "goal_title": task.title,
            "delivery_mode": "approved_template",
        },
        "whatsapp_template": {
            "name": template_name,
            "language_code": template_language,
            "body_parameters": template_body_parameters,
        },
    }
    message = Message(
        workspace_id=workspace_id,
        conversation_id=conversation.id,
        channel_connection_id=connection.id,
        sender_type="ai",
        direction="outbound",
        message_type="template",
        content="WhatsApp approved follow-up template",
        delivery_status="queued",
        metadata_json=metadata,
    )
    db.add(message)
    db.flush()
    dispatch = MessageDispatch(
        workspace_id=workspace_id,
        channel_connection_id=connection.id,
        message_id=message.id,
        status="queued",
        attempts=0,
        metadata_json={
            "conversation_id": str(conversation.id),
            "sender_type": "ai",
            "source": "ai_followup",
            "crm_task_id": str(task.id),
            "automation_job_id": str(job.id),
        },
    )
    db.add(dispatch)
    db.flush()

    record_outbound_activity(conversation, now=now)
    task.status = "in_progress"
    job.status = "dispatched"
    job.message_id = message.id
    job.dispatch_id = dispatch.id
    job.completed_at = now
    job.locked_at = None
    job.result_json = {
        "message_id": str(message.id),
        "dispatch_id": str(dispatch.id),
        "channel": "whatsapp",
        "message_type": "template",
        "delivery_mode": "approved_template",
        "template_name": template_name,
    }
    db.commit()
    db.refresh(job)
    return ExecutionResult(job=job, reason=None)


def _recent_followup_history(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    limit: int = 12,
) -> list[dict[str, str]]:
    rows = list(
        db.scalars(
            select(Message)
            .where(
                Message.workspace_id == workspace_id,
                Message.conversation_id == conversation_id,
                Message.content.is_not(None),
                Message.direction != "internal",
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    )
    rows.reverse()
    history: list[dict[str, str]] = []
    for message in rows:
        content = " ".join((message.content or "").strip().split())
        if not content:
            continue
        role = "customer" if message.sender_type == "patient" else "tia_or_staff"
        history.append({"role": role, "text": content[:1200]})
    return history


def _execute_crm_followup_job(
    db: Session,
    *,
    workspace_id: UUID,
    job: AutomationJob,
    now: datetime,
) -> ExecutionResult:
    task = db.scalar(
        select(CRMTask)
        .where(
            CRMTask.workspace_id == workspace_id,
            CRMTask.id == job.crm_task_id,
        )
        .with_for_update()
    )
    patient = db.get(Patient, job.patient_id)
    workspace = db.get(Workspace, workspace_id)
    if task is None or patient is None or workspace is None:
        job.status = "skipped"
        job.completed_at = now
        job.locked_at = None
        job.result_json = {"reason": "follow_up_target_missing"}
        db.commit()
        return ExecutionResult(job=job, reason="follow_up_target_missing")

    lead_followup_reason = _system_lead_followup_ineligible_reason(
        db,
        task=task,
        for_update=True,
    )
    if lead_followup_reason is not None:
        task.status = "cancelled"
        task.completed_at = now
        job.status = "cancelled"
        job.completed_at = now
        job.locked_at = None
        job.next_attempt_at = None
        job.result_json = {"reason": lead_followup_reason}
        sync_lead_next_follow_up(
            db,
            workspace_id=workspace_id,
            lead_id=task.lead_id,
        )
        db.commit()
        return ExecutionResult(job=job, reason=lead_followup_reason)

    if task.execution_mode != "ai" or task.status not in {"pending", "in_progress"}:
        job.status = "cancelled"
        job.completed_at = now
        job.locked_at = None
        job.result_json = {"reason": "follow_up_no_longer_ai_eligible"}
        db.commit()
        return ExecutionResult(job=job, reason="follow_up_no_longer_ai_eligible")
    if task.due_at > now:
        job.status = "queued"
        job.scheduled_for = task.due_at
        job.locked_at = None
        job.next_attempt_at = None
        job.result_json = {"reason": "follow_up_rescheduled"}
        db.commit()
        return ExecutionResult(job=job, reason="follow_up_rescheduled")
    if patient.status != "active":
        task.status = "cancelled"
        task.completed_at = now
        job.status = "skipped"
        job.completed_at = now
        job.locked_at = None
        job.result_json = {"reason": "patient_not_active"}
        db.commit()
        return ExecutionResult(job=job, reason="patient_not_active")
    if not patient.whatsapp_opt_in:
        result = _handoff_followup_to_staff(
            task=task,
            job=job,
            conversation=None,
            reason="whatsapp_opt_in_required",
            now=now,
        )
        db.commit()
        return result

    route = _resolve_followup_route(db, task=task, now=now)
    if route is None:
        result = _handoff_followup_to_staff(
            task=task, job=job, conversation=None, reason="no_active_whatsapp_route", now=now
        )
        db.commit()
        return result
    conversation, connection = route
    active_handoff = _followup_active_handoff(
        db, workspace_id=workspace_id, conversation_id=conversation.id
    )
    if conversation.owner_type != "ai" or conversation.status == "pending" or active_handoff:
        result = _handoff_followup_to_staff(
            task=task,
            job=job,
            conversation=conversation,
            reason="conversation_human_owned_or_handoff_active",
            now=now,
        )
        db.commit()
        return result

    identity = db.scalar(
        select(ChannelIdentity.id).where(
            ChannelIdentity.workspace_id == workspace_id,
            ChannelIdentity.channel_connection_id == connection.id,
            ChannelIdentity.patient_id == patient.id,
        )
    )
    if identity is None:
        result = _handoff_followup_to_staff(
            task=task, job=job, conversation=conversation, reason="whatsapp_identity_missing", now=now
        )
        db.commit()
        return result

    if _conversation_outbound_busy(
        db, workspace_id=workspace_id, conversation_id=conversation.id
    ):
        job.status = "failed"
        job.locked_at = None
        job.next_attempt_at = now + timedelta(seconds=60)
        job.last_error = "follow_up_waiting_for_conversation_outbox"
        job.result_json = {"reason": "conversation_outbox_busy", "retry": True}
        db.commit()
        db.refresh(job)
        return ExecutionResult(job=job, reason="conversation_outbox_busy")

    latest_inbound_at = _latest_patient_inbound_at(
        db, workspace_id=workspace_id, conversation_id=conversation.id
    )
    if not _whatsapp_customer_service_window_open(
        latest_patient_inbound_at=latest_inbound_at, now=now
    ):
        previous_template = _latest_ai_followup_template_name(
            db, workspace_id=workspace_id, conversation_id=conversation.id
        )
        template = _select_ai_followup_template(
            connection,
            task_id=task.id,
            patient_id=patient.id,
            previous_template=previous_template,
        )
        if template is None:
            result = _handoff_followup_to_staff(
                task=task,
                job=job,
                conversation=conversation,
                reason="approved_whatsapp_followup_template_required",
                now=now,
            )
            db.commit()
            return result
        return _dispatch_ai_followup_template(
            db,
            workspace_id=workspace_id,
            task=task,
            job=job,
            conversation=conversation,
            connection=connection,
            template_name=template[0],
            template_language=template[1],
            now=now,
        )

    history = _recent_followup_history(
        db, workspace_id=workspace_id, conversation_id=conversation.id
    )
    job_id = job.id
    conversation_id = conversation.id
    last_message_at_snapshot = conversation.last_message_at
    connection_id = connection.id
    task_id = task.id
    patient_name = (
        f"{patient.first_name or ''} {patient.last_name or ''}".strip()
        or patient.first_name
        or "العميل"
    )
    goal_title = task.title
    goal_note = task.description
    clinic_name = workspace.name
    timezone_name = workspace.timezone or "Africa/Cairo"
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone_name = "Africa/Cairo"
        tz = ZoneInfo(timezone_name)

    # Release database locks before provider latency. The final write re-locks and
    # revalidates ownership/task state so staff takeover during generation wins.
    db.commit()
    try:
        from app.agents.followup_composer import compose_followup_message

        text, model_name = compose_followup_message(
            clinic_name=clinic_name,
            timezone_name=timezone_name,
            local_now=now.astimezone(tz),
            patient_name=patient_name,
            goal_title=goal_title,
            goal_note=goal_note,
            recent_messages=history,
        )
    except Exception as exc:
        retry_job = db.scalar(
            select(AutomationJob)
            .where(
                AutomationJob.workspace_id == workspace_id,
                AutomationJob.id == job_id,
            )
            .with_for_update()
        )
        if retry_job is not None and retry_job.status == "processing":
            retry_job.status = "failed"
            retry_job.locked_at = None
            retry_job.next_attempt_at = now + timedelta(seconds=60)
            retry_job.last_error = f"follow_up_composer_failed:{type(exc).__name__}"[:4000]
            retry_job.result_json = {"reason": "follow_up_composer_failed", "retry": True}
            db.commit()
            db.refresh(retry_job)
            return ExecutionResult(job=retry_job, reason="follow_up_composer_failed")
        if retry_job is None:
            raise AutomationError("AI follow-up job disappeared during generation.") from exc
        return ExecutionResult(job=retry_job, reason=retry_job.result_json.get("reason"))

    final_job = db.scalar(
        select(AutomationJob)
        .where(
            AutomationJob.workspace_id == workspace_id,
            AutomationJob.id == job_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if final_job is None:
        raise AutomationError("AI follow-up job not found after generation.")
    if final_job.status != "processing":
        return ExecutionResult(job=final_job, reason=final_job.result_json.get("reason"))

    final_task = db.scalar(
        select(CRMTask)
        .where(CRMTask.workspace_id == workspace_id, CRMTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    final_conversation = db.scalar(
        select(Conversation)
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.id == conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if final_task is None or final_conversation is None:
        final_job.status = "skipped"
        final_job.completed_at = now
        final_job.locked_at = None
        final_job.result_json = {"reason": "follow_up_target_missing_after_generation"}
        db.commit()
        return ExecutionResult(job=final_job, reason="follow_up_target_missing_after_generation")

    lead_followup_reason = _system_lead_followup_ineligible_reason(
        db,
        task=final_task,
        for_update=True,
    )
    if lead_followup_reason is not None:
        final_task.status = "cancelled"
        final_task.completed_at = now
        final_job.status = "cancelled"
        final_job.completed_at = now
        final_job.locked_at = None
        final_job.next_attempt_at = None
        final_job.result_json = {"reason": lead_followup_reason}
        sync_lead_next_follow_up(
            db,
            workspace_id=workspace_id,
            lead_id=final_task.lead_id,
        )
        db.commit()
        return ExecutionResult(job=final_job, reason=lead_followup_reason)

    if final_conversation.last_message_at != last_message_at_snapshot:
        final_job.status = "failed"
        final_job.locked_at = None
        final_job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=60)
        final_job.last_error = "follow_up_context_changed_during_generation"
        final_job.result_json = {"reason": "conversation_changed_during_generation", "retry": True}
        db.commit()
        db.refresh(final_job)
        return ExecutionResult(job=final_job, reason="conversation_changed_during_generation")

    final_handoff = _followup_active_handoff(
        db, workspace_id=workspace_id, conversation_id=conversation_id
    )
    if (
        final_task.execution_mode != "ai"
        or final_task.status not in {"pending", "in_progress"}
        or final_conversation.owner_type != "ai"
        or final_conversation.status == "pending"
        or final_handoff is not None
    ):
        result = _handoff_followup_to_staff(
            task=final_task,
            job=final_job,
            conversation=final_conversation,
            reason="follow_up_authority_changed_during_generation",
            now=now,
        )
        db.commit()
        return result

    final_connection = db.get(ChannelConnection, connection_id)
    if final_connection is None or final_connection.status != "active":
        result = _handoff_followup_to_staff(
            task=final_task,
            job=final_job,
            conversation=final_conversation,
            reason="whatsapp_connection_became_unavailable",
            now=now,
        )
        db.commit()
        return result

    if final_conversation.status == "closed":
        return_to_ai(final_conversation, close=False, now=now)

    metadata = {
        "source": "ai_followup",
        "crm_task_id": str(final_task.id),
        "automation_job_id": str(final_job.id),
        "follow_up": {
            "goal_title": final_task.title,
            "composer_model": model_name,
        },
    }
    message = Message(
        workspace_id=workspace_id,
        conversation_id=final_conversation.id,
        channel_connection_id=final_connection.id,
        sender_type="ai",
        direction="outbound",
        message_type="text",
        content=text,
        delivery_status="queued",
        metadata_json=metadata,
    )
    db.add(message)
    db.flush()
    dispatch = MessageDispatch(
        workspace_id=workspace_id,
        channel_connection_id=final_connection.id,
        message_id=message.id,
        status="queued",
        attempts=0,
        metadata_json={
            "conversation_id": str(final_conversation.id),
            "sender_type": "ai",
            "source": "ai_followup",
            "crm_task_id": str(final_task.id),
            "automation_job_id": str(final_job.id),
        },
    )
    db.add(dispatch)
    db.flush()

    record_outbound_activity(final_conversation, now=now)
    final_task.status = "in_progress"
    final_job.status = "dispatched"
    final_job.message_id = message.id
    final_job.dispatch_id = dispatch.id
    final_job.completed_at = now
    final_job.locked_at = None
    final_job.result_json = {
        "message_id": str(message.id),
        "dispatch_id": str(dispatch.id),
        "channel": "whatsapp",
        "message_type": "text",
        "composer_model": model_name,
    }
    db.commit()
    db.refresh(final_job)
    return ExecutionResult(job=final_job, reason=None)

def automation_operations_overview(
    db: Session,
    *,
    workspace_id: UUID,
    now: datetime | None = None,
) -> AutomationOperationsOverview:
    """Return a fixed-query operational snapshot for the automation dashboard."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    stale_before = now - timedelta(minutes=AUTOMATION_JOB_STALE_MINUTES)
    worker_fresh_after = now - timedelta(minutes=AUTOMATION_WORKER_FRESH_MINUTES)

    enabled_rules = int(
        db.scalar(
            select(func.count())
            .select_from(AutomationRule)
            .where(
                AutomationRule.workspace_id == workspace_id,
                AutomationRule.enabled.is_(True),
                AutomationRule.key.notin_(RETIRED_AUTOMATION_RULE_KEYS | {"booking_confirmation"}),
            )
        )
        or 0
    )

    status_rows = db.execute(
        select(AutomationJob.status, func.count())
        .where(AutomationJob.workspace_id == workspace_id)
        .group_by(AutomationJob.status)
    ).all()
    status_counts = {str(status): int(count) for status, count in status_rows}

    due_jobs = int(
        db.scalar(
            select(func.count())
            .select_from(AutomationJob)
            .where(
                AutomationJob.workspace_id == workspace_id,
                AutomationJob.status.in_(("queued", "failed")),
                AutomationJob.scheduled_for <= now,
                or_(
                    AutomationJob.next_attempt_at.is_(None),
                    AutomationJob.next_attempt_at <= now,
                ),
            )
        )
        or 0
    )
    next_job_at = db.scalar(
        select(func.min(AutomationJob.scheduled_for)).where(
            AutomationJob.workspace_id == workspace_id,
            AutomationJob.status.in_(("queued", "failed")),
        )
    )
    delivery_failed_jobs = int(
        db.scalar(
            select(func.count())
            .select_from(AutomationJob)
            .join(MessageDispatch, MessageDispatch.id == AutomationJob.dispatch_id)
            .where(
                AutomationJob.workspace_id == workspace_id,
                AutomationJob.status == "dispatched",
                MessageDispatch.status == "failed",
            )
        )
        or 0
    )
    stuck_processing = int(
        db.scalar(
            select(func.count())
            .select_from(AutomationJob)
            .where(
                AutomationJob.workspace_id == workspace_id,
                AutomationJob.status == "processing",
                AutomationJob.locked_at.is_not(None),
                AutomationJob.locked_at <= stale_before,
            )
        )
        or 0
    )

    worker_last_seen_at = db.scalar(
        select(func.max(AutomationWorker.last_seen_at)).where(
            AutomationWorker.workspace_id == workspace_id,
            AutomationWorker.status == "active",
        )
    )
    active_worker_count = int(
        db.scalar(
            select(func.count())
            .select_from(AutomationWorker)
            .where(
                AutomationWorker.workspace_id == workspace_id,
                AutomationWorker.status == "active",
            )
        )
        or 0
    )
    if enabled_rules == 0:
        worker_state = "not_required"
    elif active_worker_count == 0:
        worker_state = "missing"
    elif worker_last_seen_at is None or worker_last_seen_at < worker_fresh_after:
        worker_state = "stale"
    else:
        worker_state = "healthy"

    failed_jobs = status_counts.get("failed", 0)
    return AutomationOperationsOverview(
        now=now,
        enabled_rules=enabled_rules,
        queued_jobs=status_counts.get("queued", 0),
        due_jobs=due_jobs,
        processing_jobs=status_counts.get("processing", 0),
        failed_jobs=failed_jobs,
        delivery_failed_jobs=delivery_failed_jobs,
        attention_count=failed_jobs + delivery_failed_jobs + stuck_processing,
        next_job_at=next_job_at,
        worker_state=worker_state,
        worker_last_seen_at=worker_last_seen_at,
        worker_fresh_within_minutes=AUTOMATION_WORKER_FRESH_MINUTES,
    )


def _get_locked_automation_job(
    db: Session, *, workspace_id: UUID, job_id: UUID
) -> AutomationJob:
    job = db.scalar(
        select(AutomationJob)
        .where(
            AutomationJob.workspace_id == workspace_id,
            AutomationJob.id == job_id,
        )
        .with_for_update()
    )
    if job is None:
        raise AutomationError("Automation job not found.")
    return job


def retry_automation_job(
    db: Session,
    *,
    workspace_id: UUID,
    job_id: UUID,
    actor_user_id: UUID | None = None,
    now: datetime | None = None,
) -> tuple[AutomationJob, MessageDispatch | None]:
    """Admin retry that preserves idempotency and never creates a second message."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    job = _get_locked_automation_job(db, workspace_id=workspace_id, job_id=job_id)

    if job.status == "failed":
        job.status = "queued"
        job.locked_at = None
        job.next_attempt_at = None
        job.completed_at = None
        job.last_error = None
        job.result_json = {
            **(job.result_json or {}),
            "manual_retry_requested_at": now.isoformat(),
            "manual_retry_scope": "job_execution",
        }
        record_activity_event(
            db,
            workspace_id=workspace_id,
            actor_type="staff",
            actor_user_id=actor_user_id,
            action="automation.job_retried",
            entity_type="automation_job",
            entity_id=job.id,
            summary="Automation job retry requested",
            metadata={"retry_scope": "job_execution", "job_kind": job.job_kind},
        )
        db.commit()
        db.refresh(job)
        return job, None

    if job.status != "dispatched" or job.dispatch_id is None:
        raise AutomationError(
            "Only failed automation jobs or failed message deliveries can be retried."
        )

    dispatch = db.scalar(
        select(MessageDispatch)
        .where(
            MessageDispatch.workspace_id == workspace_id,
            MessageDispatch.id == job.dispatch_id,
        )
        .with_for_update()
    )
    if dispatch is None:
        raise AutomationError("Automation dispatch not found.")
    if dispatch.status != "failed":
        raise AutomationError(f"Dispatch is '{dispatch.status}', not failed.")
    if dispatch.provider_message_id:
        raise AutomationError(
            "Provider already assigned a message id; automatic resend is blocked to avoid duplicates."
        )

    message = db.get(Message, dispatch.message_id)
    dispatch.status = "queued"
    dispatch.attempts = 0
    dispatch.last_error = None
    dispatch.next_attempt_at = None
    dispatch.locked_at = None
    if message is not None:
        message.delivery_status = "queued"
    job.result_json = {
        **(job.result_json or {}),
        "manual_retry_requested_at": now.isoformat(),
        "manual_retry_scope": "message_delivery",
    }
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=actor_user_id,
        action="automation.job_retried",
        entity_type="automation_job",
        entity_id=job.id,
        summary="Automation delivery retry requested",
        metadata={"retry_scope": "message_delivery", "job_kind": job.job_kind},
    )
    db.commit()
    db.refresh(job)
    db.refresh(dispatch)
    return job, dispatch


def cancel_automation_job(
    db: Session,
    *,
    workspace_id: UUID,
    job_id: UUID,
    actor_user_id: UUID | None = None,
    now: datetime | None = None,
) -> tuple[AutomationJob, MessageDispatch | None]:
    """Cancel only work that has not entered an active provider send lease."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    job = _get_locked_automation_job(db, workspace_id=workspace_id, job_id=job_id)

    if job.status in {"queued", "failed"}:
        job.status = "cancelled"
        job.locked_at = None
        job.next_attempt_at = None
        job.completed_at = now
        job.result_json = {
            **(job.result_json or {}),
            "reason": "cancelled_by_admin",
            "cancelled_at": now.isoformat(),
        }
        record_activity_event(
            db,
            workspace_id=workspace_id,
            actor_type="staff",
            actor_user_id=actor_user_id,
            action="automation.job_cancelled",
            entity_type="automation_job",
            entity_id=job.id,
            summary="Automation job cancelled",
            metadata={"cancel_scope": "job", "job_kind": job.job_kind},
        )
        db.commit()
        db.refresh(job)
        return job, None

    if job.status != "dispatched" or job.dispatch_id is None:
        raise AutomationError(f"Automation job is '{job.status}' and cannot be cancelled.")

    dispatch = db.scalar(
        select(MessageDispatch)
        .where(
            MessageDispatch.workspace_id == workspace_id,
            MessageDispatch.id == job.dispatch_id,
        )
        .with_for_update()
    )
    if dispatch is None:
        raise AutomationError("Automation dispatch not found.")
    if dispatch.status != "queued":
        raise AutomationError(
            f"Dispatch is '{dispatch.status}'; only queued provider sends can be cancelled safely."
        )

    message = db.get(Message, dispatch.message_id)
    dispatch.status = "cancelled"
    dispatch.locked_at = None
    dispatch.next_attempt_at = None
    dispatch.last_error = "Cancelled by admin before provider send."
    if message is not None:
        message.delivery_status = "cancelled"
    job.status = "cancelled"
    job.completed_at = now
    job.result_json = {
        **(job.result_json or {}),
        "reason": "cancelled_by_admin_before_provider_send",
        "cancelled_at": now.isoformat(),
    }
    record_activity_event(
        db,
        workspace_id=workspace_id,
        actor_type="staff",
        actor_user_id=actor_user_id,
        action="automation.job_cancelled",
        entity_type="automation_job",
        entity_id=job.id,
        summary="Queued automation delivery cancelled",
        metadata={"cancel_scope": "message_delivery", "job_kind": job.job_kind},
    )
    db.commit()
    db.refresh(job)
    db.refresh(dispatch)
    return job, dispatch


def execute_job(
    db: Session,
    *,
    workspace_id: UUID,
    job_id: UUID,
    now: datetime | None = None,
) -> ExecutionResult:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    job = db.scalar(
        select(AutomationJob)
        .where(
            AutomationJob.workspace_id == workspace_id,
            AutomationJob.id == job_id,
        )
        .with_for_update()
    )
    if job is None:
        raise AutomationError("Automation job not found.")
    if job.status in {"dispatched", "skipped", "cancelled"}:
        return ExecutionResult(job=job, reason=job.result_json.get("reason"))
    if job.status != "processing":
        raise AutomationError(f"Automation job is '{job.status}', not processing.")

    if job.job_kind == "crm_follow_up":
        return _execute_crm_followup_job(
            db, workspace_id=workspace_id, job=job, now=now
        )
    if job.job_kind != "appointment_rule":
        raise AutomationError(f"Unsupported automation job kind '{job.job_kind}'.")

    rule = db.get(AutomationRule, job.rule_id)
    appointment = db.get(Appointment, job.appointment_id)
    patient = db.get(Patient, job.patient_id)
    workspace = db.get(Workspace, workspace_id)

    if rule is None or appointment is None or patient is None or workspace is None:
        job.status = "skipped"
        job.completed_at = now
        job.locked_at = None
        job.result_json = {"reason": "automation_target_missing"}
        db.commit()
        return ExecutionResult(job=job, reason="automation_target_missing")

    if not rule.enabled or not _eligible_for_rule(appointment, rule):
        job.status = "cancelled"
        job.completed_at = now
        job.locked_at = None
        job.result_json = {"reason": "rule_disabled_or_appointment_no_longer_eligible"}
        db.commit()
        return ExecutionResult(job=job, reason=job.result_json["reason"])

    if patient.status != "active":
        job.status = "skipped"
        job.completed_at = now
        job.locked_at = None
        job.result_json = {"reason": "patient_not_active"}
        db.commit()
        return ExecutionResult(job=job, reason="patient_not_active")

    route = _resolve_external_route(
        db,
        workspace_id=workspace_id,
        patient_id=patient.id,
        channel=rule.channel,
        now=now,
    )
    if route is None:
        job.status = "skipped"
        job.completed_at = now
        job.locked_at = None
        job.result_json = {"reason": "no_active_external_channel_identity"}
        db.commit()
        return ExecutionResult(job=job, reason=job.result_json["reason"])

    conversation, connection = route
    if connection.channel == "whatsapp" and not patient.whatsapp_opt_in:
        job.status = "skipped"
        job.completed_at = now
        job.locked_at = None
        job.result_json = {"reason": "whatsapp_opt_in_required"}
        db.commit()
        return ExecutionResult(job=job, reason="whatsapp_opt_in_required")

    display = _appointment_display_data(
        db,
        workspace=workspace,
        appointment=appointment,
        patient=patient,
    )
    fallback = _fallback_text(rule.key, display)

    template_name, template_language, template_variant_count = _select_rule_template(
        rule, appointment.id
    )
    metadata = {
        "source": "automation_engine",
        "automation_job_id": str(job.id),
        "automation_rule_key": rule.key,
        "appointment_id": str(appointment.id),
        "whatsapp_template": {
            "name": template_name,
            "language_code": template_language,
            "body_parameters": _appointment_template_body_parameters(
                rule.key, display, template_name=template_name
            ),
            "variant_count": template_variant_count,
        },
        "appointment": display,
    }

    message_type = "template" if connection.channel == "whatsapp" else "text"
    message = Message(
        workspace_id=workspace_id,
        conversation_id=conversation.id,
        channel_connection_id=connection.id,
        sender_type="system",
        direction="outbound",
        message_type=message_type,
        content=fallback,
        delivery_status="queued",
        metadata_json=metadata,
    )
    db.add(message)
    db.flush()

    dispatch = MessageDispatch(
        workspace_id=workspace_id,
        channel_connection_id=connection.id,
        message_id=message.id,
        status="queued",
        attempts=0,
        metadata_json={
            "conversation_id": str(conversation.id),
            "sender_type": "system",
            "source": "automation_engine",
            "automation_job_id": str(job.id),
        },
    )
    db.add(dispatch)
    db.flush()

    conversation.last_message_at = now
    job.status = "dispatched"
    job.message_id = message.id
    job.dispatch_id = dispatch.id
    job.completed_at = now
    job.locked_at = None
    job.result_json = {
        "message_id": str(message.id),
        "dispatch_id": str(dispatch.id),
        "channel": connection.channel,
        "channel_connection_id": str(connection.id),
        "message_type": message_type,
    }
    db.commit()
    db.refresh(job)
    return ExecutionResult(job=job, reason=None)


def mark_job_failed(
    db: Session,
    *,
    workspace_id: UUID,
    job_id: UUID,
    error: str,
    retry_after_seconds: int = 60,
) -> AutomationJob:
    job = db.scalar(
        select(AutomationJob)
        .where(
            AutomationJob.workspace_id == workspace_id,
            AutomationJob.id == job_id,
        )
        .with_for_update()
    )
    if job is None:
        raise AutomationError("Automation job not found.")
    now = datetime.now(UTC)
    job.status = "failed"
    job.locked_at = None
    job.last_error = error[:4000]
    job.next_attempt_at = now + timedelta(seconds=max(1, retry_after_seconds))
    db.commit()
    db.refresh(job)
    return job
