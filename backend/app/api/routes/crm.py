from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.api.dependencies.security import WorkspaceAccess, get_workspace_admin, get_workspace_reader
from app.database.session import get_db
from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    require_tia_patient_fields_writable,
)
from app.models.branch import Branch
from app.models.conversation import Conversation
from app.models.crm_campaign import CRMCampaign, CRMCampaignRecipient
from app.models.crm_cohort import CRMCohort, CRMCohortMember
from app.models.crm_task import CRMTask
from app.models.lead import Lead
from app.models.message import Message
from app.models.patient import Patient
from app.models.patient_note import PatientNote
from app.models.patient_tag import PatientTag, PatientTagAssignment
from app.models.service import Service
from app.models.user import User
from app.models.workspace_member import WORKSPACE_ROLE_ADMIN, WorkspaceMember
from app.schemas.analytics_bi import AnalyticsBIPlan
from app.schemas.analytics_composable import AnalyticsAudiencePlan
from app.schemas.crm import (
    ConversationChannel,
    ConversationCreate,
    ConversationRead,
    ConversationStatus,
    ConversationUpdate,
    CRMTaskCreate,
    CRMTaskRead,
    CRMTaskStatus,
    CRMTaskType,
    CRMTaskUpdate,
    LeadCreate,
    LeadRead,
    LeadStatus,
    LeadUpdate,
    MessageCreate,
    MessageRead,
    PatientCreate,
    PatientNoteCreate,
    PatientNoteRead,
    PatientProfileRead,
    PatientRead,
    PatientSource,
    PatientStatus,
    PatientTagCreate,
    PatientTagRead,
    PatientUpdate,
    normalize_phone,
)
from app.schemas.crm_cohort import (
    AnalyticsAudienceActionConfirmRequest,
    AnalyticsAudienceActionResult,
    AnalyticsCohortCreateRequest,
    CohortCampaignConfirmRequest,
    CohortCampaignConfirmResult,
    CohortCampaignPrepareRequest,
    CohortFollowUpCreateRequest,
    CohortFollowUpResult,
    CRMCampaignRead,
    CRMCampaignRecipientRead,
    CRMCohortMemberRead,
    CRMCohortRead,
)
from app.schemas.patient_history import PatientHistoryContextRead
from app.services.conversation_ownership import (
    record_customer_inbound,
    return_to_ai,
)
from app.services.crm_campaigns import (
    CRMCampaignError,
    confirm_cohort_campaign,
    prepare_cohort_campaign,
)
from app.services.crm_cohorts import (
    CRMCohortError,
    create_analytics_cohort,
    create_cohort_follow_up_tasks,
    execute_confirmed_audience_action,
)
from app.services.crm_tasks import (
    ACTIVE_TASK_STATUSES,
    CRMTaskConflict,
    CRMTaskError,
    CRMTaskPermissionError,
    claim_crm_task,
    create_crm_task,
    replace_legacy_lead_follow_up,
    update_crm_task,
)
from app.services.handoff_intelligence import build_handoff_context
from app.services.handoffs import (
    HandoffStateError,
    assign_handoff,
    create_handoff,
    ensure_active_workspace_user,
)
from app.services.patient_history import build_patient_history_context
from app.services.patient_timeline import build_patient_profile

router = APIRouter()


def not_found(entity: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{entity} not found.",
    )


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from exc


def get_patient_or_404(db: Session, workspace_id: UUID, patient_id: UUID) -> Patient:
    patient = db.scalar(
        select(Patient).where(
            Patient.id == patient_id,
            Patient.workspace_id == workspace_id,
        )
    )
    if patient is None:
        raise not_found("Patient")
    return patient


def _ensure_crm_handoff(
    db: Session,
    *,
    access: WorkspaceAccess,
    conversation: Conversation,
    patient: Patient,
    assigned_user_id: UUID | None,
    reason: str,
) -> None:
    try:
        handoff = create_handoff(
            db,
            workspace_id=access.workspace.id,
            conversation=conversation,
            patient=patient,
            reason=reason,
            category="customer_request",
            priority="normal",
            source="staff",
            created_by_user_id=access.user.id,
            handoff_context=build_handoff_context(
                trigger="crm_conversation_ownership",
                semantic_reason=reason,
            ),
            commit=False,
        )
        if assigned_user_id is not None:
            target_user = ensure_active_workspace_user(
                db,
                workspace_id=access.workspace.id,
                user_id=assigned_user_id,
            )
            assign_handoff(
                db,
                handoff=handoff,
                conversation=conversation,
                target_user=target_user,
                actor_user=access.user,
                commit=False,
            )
    except HandoffStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


def ensure_branch(db: Session, workspace_id: UUID, branch_id: UUID | None) -> None:
    if branch_id is None:
        return
    branch = db.scalar(
        select(Branch.id).where(
            Branch.id == branch_id,
            Branch.workspace_id == workspace_id,
            Branch.is_active.is_(True),
        )
    )
    if branch is None:
        raise not_found("Preferred branch")


def ensure_service(db: Session, workspace_id: UUID, service_id: UUID | None) -> None:
    if service_id is None:
        return
    service = db.scalar(
        select(Service.id).where(
            Service.id == service_id,
            Service.workspace_id == workspace_id,
            Service.is_active.is_(True),
        )
    )
    if service is None:
        raise not_found("Service")


def ensure_assignable_user(db: Session, workspace_id: UUID, user_id: UUID | None) -> None:
    if user_id is None:
        return
    membership = db.scalar(
        select(WorkspaceMember.id).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.is_active.is_(True),
        )
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Assigned user is not an active member of this workspace.",
        )


def apply_patient_contact_fields(patient: Patient, phone: str | None) -> None:
    display_phone, normalized_phone = normalize_phone(phone)
    patient.phone = display_phone
    patient.phone_normalized = normalized_phone


@router.post("/patients", response_model=PatientRead, status_code=status.HTTP_201_CREATED)
def create_patient(
    payload: PatientCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Patient:
    ensure_branch(db, access.workspace.id, payload.preferred_branch_id)
    data = payload.model_dump(exclude={"phone", "marketing_consent", "whatsapp_opt_in"})
    patient = Patient(
        workspace_id=access.workspace.id,
        marketing_consent=payload.marketing_consent,
        whatsapp_opt_in=payload.whatsapp_opt_in,
        **data,
    )
    apply_patient_contact_fields(patient, payload.phone)
    if payload.marketing_consent:
        patient.marketing_consent_at = datetime.now(UTC)
    if payload.whatsapp_opt_in:
        patient.whatsapp_opt_in_at = datetime.now(UTC)
        patient.whatsapp_opt_in_source = "staff_recorded"
    db.add(patient)
    commit_or_conflict(db, "A patient with this phone already exists in this workspace.")
    db.refresh(patient)
    return patient


@router.get("/patients", response_model=list[PatientRead])
def list_patients(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    patient_status: Annotated[PatientStatus | None, Query(alias="status")] = None,
    source: PatientSource | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Patient]:
    stmt = select(Patient).where(Patient.workspace_id == access.workspace.id)
    if patient_status:
        stmt = stmt.where(Patient.status == patient_status)
    if source:
        stmt = stmt.where(Patient.source == source)
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Patient.first_name.ilike(term),
                Patient.last_name.ilike(term),
                Patient.phone.ilike(term),
            )
        )
    stmt = stmt.order_by(Patient.created_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt))


@router.get("/patients/{patient_id}", response_model=PatientRead)
def get_patient(
    patient_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Patient:
    return get_patient_or_404(db, access.workspace.id, patient_id)


@router.get("/patients/{patient_id}/profile", response_model=PatientProfileRead)
def get_patient_profile(
    patient_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    timeline_limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PatientProfileRead:
    patient = get_patient_or_404(db, access.workspace.id, patient_id)
    return build_patient_profile(
        db,
        workspace_id=access.workspace.id,
        patient=patient,
        timeline_limit=timeline_limit,
    )


@router.get("/patients/{patient_id}/history-context", response_model=PatientHistoryContextRead)
def get_patient_history_context(
    patient_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    recent_limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> PatientHistoryContextRead:
    patient = get_patient_or_404(db, access.workspace.id, patient_id)
    return build_patient_history_context(
        db,
        workspace_id=access.workspace.id,
        patient=patient,
        recent_limit=recent_limit,
    )


@router.patch("/patients/{patient_id}", response_model=PatientRead)
def update_patient(
    patient_id: UUID,
    payload: PatientUpdate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Patient:
    patient = get_patient_or_404(db, access.workspace.id, patient_id)
    updates = payload.model_dump(exclude_unset=True)
    try:
        require_tia_patient_fields_writable(
            db,
            workspace_id=access.workspace.id,
            patient_id=patient.id,
            fields=set(updates),
        )
    except ClinicIntegrationAuthorityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    required_fields = {"first_name", "preferred_language", "source", "status", "marketing_consent", "whatsapp_opt_in"}
    if any(field in updates and updates[field] is None for field in required_fields):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Required patient fields cannot be null.",
        )

    if "preferred_branch_id" in updates:
        ensure_branch(db, access.workspace.id, updates["preferred_branch_id"])
    if "phone" in updates:
        display_phone, normalized_phone = normalize_phone(updates.pop("phone"))
        patient.phone = display_phone
        patient.phone_normalized = normalized_phone
    if "marketing_consent" in updates:
        new_consent = updates["marketing_consent"]
        if new_consent and not patient.marketing_consent:
            patient.marketing_consent_at = datetime.now(UTC)
        elif not new_consent:
            patient.marketing_consent_at = None
    if "whatsapp_opt_in" in updates:
        new_whatsapp_opt_in = updates["whatsapp_opt_in"]
        if new_whatsapp_opt_in and not patient.whatsapp_opt_in:
            patient.whatsapp_opt_in_at = datetime.now(UTC)
            patient.whatsapp_opt_in_source = "staff_recorded"
        elif not new_whatsapp_opt_in:
            patient.whatsapp_opt_in_at = None
            patient.whatsapp_opt_in_source = None

    for key, value in updates.items():
        setattr(patient, key, value)

    commit_or_conflict(db, "A patient with this phone already exists in this workspace.")
    db.refresh(patient)
    return patient


@router.post(
    "/patients/{patient_id}/notes",
    response_model=PatientNoteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_patient_note(
    patient_id: UUID,
    payload: PatientNoteCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> PatientNote:
    get_patient_or_404(db, access.workspace.id, patient_id)
    note = PatientNote(
        workspace_id=access.workspace.id,
        patient_id=patient_id,
        author_user_id=access.user.id,
        **payload.model_dump(),
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


@router.get("/patients/{patient_id}/notes", response_model=list[PatientNoteRead])
def list_patient_notes(
    patient_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[PatientNote]:
    get_patient_or_404(db, access.workspace.id, patient_id)
    return list(
        db.scalars(
            select(PatientNote)
            .where(
                PatientNote.workspace_id == access.workspace.id,
                PatientNote.patient_id == patient_id,
            )
            .order_by(PatientNote.is_pinned.desc(), PatientNote.created_at.desc())
        )
    )


@router.post("/tags", response_model=PatientTagRead, status_code=status.HTTP_201_CREATED)
def create_tag(
    payload: PatientTagCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> PatientTag:
    tag = PatientTag(
        workspace_id=access.workspace.id,
        name=payload.name,
        normalized_name=payload.name.casefold(),
        color=payload.color,
    )
    db.add(tag)
    commit_or_conflict(db, "A tag with this name already exists in this workspace.")
    db.refresh(tag)
    return tag


@router.get("/tags", response_model=list[PatientTagRead])
def list_tags(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[PatientTag]:
    return list(
        db.scalars(
            select(PatientTag)
            .where(
                PatientTag.workspace_id == access.workspace.id,
                PatientTag.is_active.is_(True),
            )
            .order_by(PatientTag.name)
        )
    )


@router.put(
    "/patients/{patient_id}/tags/{tag_id}",
    response_model=PatientTagRead,
)
def assign_tag_to_patient(
    patient_id: UUID,
    tag_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> PatientTag:
    get_patient_or_404(db, access.workspace.id, patient_id)
    tag = db.scalar(
        select(PatientTag).where(
            PatientTag.id == tag_id,
            PatientTag.workspace_id == access.workspace.id,
            PatientTag.is_active.is_(True),
        )
    )
    if tag is None:
        raise not_found("Tag")

    assignment = db.scalar(
        select(PatientTagAssignment).where(
            PatientTagAssignment.workspace_id == access.workspace.id,
            PatientTagAssignment.patient_id == patient_id,
            PatientTagAssignment.tag_id == tag_id,
        )
    )
    if assignment is None:
        db.add(
            PatientTagAssignment(
                workspace_id=access.workspace.id,
                patient_id=patient_id,
                tag_id=tag_id,
                created_by_user_id=access.user.id,
            )
        )
        db.commit()
    return tag


@router.get("/patients/{patient_id}/tags", response_model=list[PatientTagRead])
def list_patient_tags(
    patient_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[PatientTag]:
    get_patient_or_404(db, access.workspace.id, patient_id)
    stmt = (
        select(PatientTag)
        .join(
            PatientTagAssignment,
            PatientTagAssignment.tag_id == PatientTag.id,
        )
        .where(
            PatientTagAssignment.workspace_id == access.workspace.id,
            PatientTagAssignment.patient_id == patient_id,
            PatientTag.workspace_id == access.workspace.id,
        )
        .order_by(PatientTag.name)
    )
    return list(db.scalars(stmt))


@router.delete(
    "/patients/{patient_id}/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_tag_from_patient(
    patient_id: UUID,
    tag_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    assignment = db.scalar(
        select(PatientTagAssignment).where(
            PatientTagAssignment.workspace_id == access.workspace.id,
            PatientTagAssignment.patient_id == patient_id,
            PatientTagAssignment.tag_id == tag_id,
        )
    )
    if assignment is not None:
        db.delete(assignment)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _workspace_local_datetime(value: datetime | None, timezone_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=_workspace_timezone(timezone_name))
    return value


@router.post("/leads", response_model=LeadRead, status_code=status.HTTP_201_CREATED)
def create_lead(
    payload: LeadCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Lead:
    patient = get_patient_or_404(db, access.workspace.id, payload.patient_id)
    ensure_service(db, access.workspace.id, payload.service_id)
    ensure_assignable_user(db, access.workspace.id, payload.assigned_user_id)
    requested_follow_up = _workspace_local_datetime(
        payload.next_follow_up_at, access.workspace.timezone
    )
    data = payload.model_dump(exclude={"source", "next_follow_up_at"})
    lead = Lead(
        workspace_id=access.workspace.id,
        source=payload.source or patient.source,
        next_follow_up_at=None,
        **data,
    )
    db.add(lead)
    try:
        db.flush()
        if requested_follow_up is not None:
            replace_legacy_lead_follow_up(
                db,
                lead=lead,
                due_at=requested_follow_up,
                actor_user_id=access.user.id,
            )
        db.commit()
    except CRMTaskError as exc:
        db.rollback()
        raise _task_error(exc) from exc
    db.refresh(lead)
    return lead


@router.get("/leads", response_model=list[LeadRead])
def list_leads(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    lead_status: Annotated[LeadStatus | None, Query(alias="status")] = None,
    patient_id: UUID | None = None,
    assigned_user_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Lead]:
    stmt = select(Lead).where(Lead.workspace_id == access.workspace.id)
    if lead_status:
        stmt = stmt.where(Lead.status == lead_status)
    if patient_id:
        stmt = stmt.where(Lead.patient_id == patient_id)
    if assigned_user_id:
        stmt = stmt.where(Lead.assigned_user_id == assigned_user_id)
    stmt = stmt.order_by(Lead.created_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt))


@router.patch("/leads/{lead_id}", response_model=LeadRead)
def update_lead(
    lead_id: UUID,
    payload: LeadUpdate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Lead:
    lead = db.scalar(
        select(Lead).where(
            Lead.id == lead_id,
            Lead.workspace_id == access.workspace.id,
        )
    )
    if lead is None:
        raise not_found("Lead")
    updates = payload.model_dump(exclude_unset=True)
    legacy_follow_up_requested = "next_follow_up_at" in updates
    requested_follow_up = _workspace_local_datetime(
        updates.pop("next_follow_up_at", None), access.workspace.timezone
    )
    required_fields = {"source", "status", "currency"}
    if any(field in updates and updates[field] is None for field in required_fields):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Required lead fields cannot be null.",
        )
    if "service_id" in updates:
        ensure_service(db, access.workspace.id, updates["service_id"])
    if "assigned_user_id" in updates:
        ensure_assignable_user(db, access.workspace.id, updates["assigned_user_id"])
    for key, value in updates.items():
        setattr(lead, key, value)
    if lead.status != "lost":
        lead.lost_reason = None
    try:
        if legacy_follow_up_requested:
            replace_legacy_lead_follow_up(
                db,
                lead=lead,
                due_at=requested_follow_up,
                actor_user_id=access.user.id,
            )
        db.commit()
    except CRMTaskError as exc:
        db.rollback()
        raise _task_error(exc) from exc
    db.refresh(lead)
    return lead


def _task_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CRMTaskPermissionError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, CRMTaskConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def _workspace_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Africa/Cairo")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Africa/Cairo")


def _task_read(
    task: CRMTask,
    *,
    patient_first_name: str,
    patient_last_name: str | None,
    patient_phone: str | None,
    assigned_user: User | None,
    now: datetime,
) -> CRMTaskRead:
    patient_name = " ".join(
        part for part in (patient_first_name, patient_last_name) if part
    ).strip()
    return CRMTaskRead(
        id=task.id,
        workspace_id=task.workspace_id,
        patient_id=task.patient_id,
        lead_id=task.lead_id,
        conversation_id=task.conversation_id,
        assigned_user_id=task.assigned_user_id,
        created_by_user_id=task.created_by_user_id,
        completed_by_user_id=task.completed_by_user_id,
        task_type=task.task_type,
        source=task.source,
        execution_mode=task.execution_mode,
        status=task.status,
        priority=task.priority,
        title=task.title,
        description=task.description,
        due_at=task.due_at,
        completed_at=task.completed_at,
        patient_name=patient_name or patient_first_name,
        patient_phone=patient_phone,
        assigned_user_name=(assigned_user.full_name if assigned_user else None),
        assigned_user_email=(assigned_user.email if assigned_user else None),
        is_overdue=(task.status in ACTIVE_TASK_STATUSES and task.due_at < now),
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _load_task_read(
    db: Session,
    *,
    workspace_id: UUID,
    task_id: UUID,
    for_update: bool = False,
) -> tuple[CRMTask, Patient, User | None] | None:
    assignee = aliased(User)
    stmt = (
        select(CRMTask, Patient, assignee)
        .join(
            Patient,
            (Patient.workspace_id == CRMTask.workspace_id)
            & (Patient.id == CRMTask.patient_id),
        )
        .outerjoin(assignee, assignee.id == CRMTask.assigned_user_id)
        .where(
            CRMTask.workspace_id == workspace_id,
            CRMTask.id == task_id,
        )
    )
    if for_update:
        stmt = stmt.with_for_update(of=CRMTask)
    return db.execute(stmt).one_or_none()


@router.post("/tasks", response_model=CRMTaskRead, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: CRMTaskCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> CRMTaskRead:
    if (
        payload.assigned_user_id is not None
        and payload.assigned_user_id != access.user.id
        and access.membership.role != WORKSPACE_ROLE_ADMIN
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can assign a task to another team member.",
        )
    try:
        task = create_crm_task(
            db,
            workspace_id=access.workspace.id,
            patient_id=payload.patient_id,
            lead_id=payload.lead_id,
            conversation_id=payload.conversation_id,
            assigned_user_id=payload.assigned_user_id,
            created_by_user_id=access.user.id,
            task_type=payload.task_type,
            execution_mode=payload.execution_mode,
            priority=payload.priority,
            title=payload.title,
            description=payload.description,
            due_at=(
                payload.due_at.replace(tzinfo=_workspace_timezone(access.workspace.timezone))
                if payload.due_at.tzinfo is None or payload.due_at.utcoffset() is None
                else payload.due_at
            ),
            source="manual",
        )
    except (CRMTaskError, CRMTaskPermissionError) as exc:
        raise _task_error(exc) from exc
    row = _load_task_read(
        db,
        workspace_id=access.workspace.id,
        task_id=task.id,
    )
    assert row is not None
    stored, patient, assigned_user = row
    return _task_read(
        stored,
        patient_first_name=patient.first_name,
        patient_last_name=patient.last_name,
        patient_phone=patient.phone,
        assigned_user=assigned_user,
        now=datetime.now(UTC),
    )


@router.get("/tasks", response_model=list[CRMTaskRead])
def list_tasks(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    scope: Literal["all", "overdue", "today", "upcoming", "due"] = "all",
    task_status: Annotated[CRMTaskStatus | None, Query(alias="status")] = None,
    task_type: CRMTaskType | None = None,
    patient_id: UUID | None = None,
    assigned_user_id: UUID | None = None,
    assigned_to_me: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CRMTaskRead]:
    now = datetime.now(UTC)
    assignee = aliased(User)
    stmt = (
        select(CRMTask, Patient, assignee)
        .join(
            Patient,
            (Patient.workspace_id == CRMTask.workspace_id)
            & (Patient.id == CRMTask.patient_id),
        )
        .outerjoin(assignee, assignee.id == CRMTask.assigned_user_id)
        .where(CRMTask.workspace_id == access.workspace.id)
    )
    if task_status is not None:
        stmt = stmt.where(CRMTask.status == task_status)
    if task_type is not None:
        stmt = stmt.where(CRMTask.task_type == task_type)
    if patient_id is not None:
        stmt = stmt.where(CRMTask.patient_id == patient_id)
    if assigned_to_me:
        stmt = stmt.where(CRMTask.assigned_user_id == access.user.id)
    elif assigned_user_id is not None:
        stmt = stmt.where(CRMTask.assigned_user_id == assigned_user_id)

    if scope != "all" and task_status is None:
        stmt = stmt.where(CRMTask.status.in_(ACTIVE_TASK_STATUSES))
    if scope == "overdue":
        stmt = stmt.where(CRMTask.due_at < now)
    elif scope in {"today", "upcoming", "due"}:
        tz = _workspace_timezone(access.workspace.timezone)
        local_now = now.astimezone(tz)
        start_local = datetime.combine(local_now.date(), time.min, tzinfo=tz)
        tomorrow_utc = (start_local + timedelta(days=1)).astimezone(UTC)
        start_utc = start_local.astimezone(UTC)
        if scope == "today":
            stmt = stmt.where(CRMTask.due_at >= start_utc, CRMTask.due_at < tomorrow_utc)
        elif scope == "due":
            stmt = stmt.where(CRMTask.due_at < tomorrow_utc)
        else:
            stmt = stmt.where(CRMTask.due_at >= tomorrow_utc)

    terminal_rank = case((CRMTask.status.in_(("completed", "cancelled")), 1), else_=0)
    rows = db.execute(
        stmt.order_by(terminal_rank, CRMTask.due_at, CRMTask.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        _task_read(
            task,
            patient_first_name=patient.first_name,
            patient_last_name=patient.last_name,
            patient_phone=patient.phone,
            assigned_user=user,
            now=now,
        )
        for task, patient, user in rows
    ]


@router.patch("/tasks/{task_id}", response_model=CRMTaskRead)
def update_task(
    task_id: UUID,
    payload: CRMTaskUpdate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> CRMTaskRead:
    row = _load_task_read(
        db,
        workspace_id=access.workspace.id,
        task_id=task_id,
        for_update=True,
    )
    if row is None:
        raise not_found("Task")
    task, _, _ = row
    try:
        updates = payload.model_dump(exclude_unset=True)
        if "due_at" in updates and updates["due_at"] is not None:
            due_at = updates["due_at"]
            if due_at.tzinfo is None or due_at.utcoffset() is None:
                updates["due_at"] = due_at.replace(
                    tzinfo=_workspace_timezone(access.workspace.timezone)
                )
        update_crm_task(
            db,
            task=task,
            actor_user_id=access.user.id,
            actor_is_admin=access.membership.role == WORKSPACE_ROLE_ADMIN,
            updates=updates,
        )
    except (CRMTaskError, CRMTaskPermissionError) as exc:
        raise _task_error(exc) from exc
    refreshed = _load_task_read(
        db,
        workspace_id=access.workspace.id,
        task_id=task_id,
    )
    assert refreshed is not None
    stored, patient, assigned_user = refreshed
    return _task_read(
        stored,
        patient_first_name=patient.first_name,
        patient_last_name=patient.last_name,
        patient_phone=patient.phone,
        assigned_user=assigned_user,
        now=datetime.now(UTC),
    )


@router.post("/tasks/{task_id}/claim", response_model=CRMTaskRead)
def claim_task(
    task_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> CRMTaskRead:
    row = _load_task_read(
        db,
        workspace_id=access.workspace.id,
        task_id=task_id,
        for_update=True,
    )
    if row is None:
        raise not_found("Task")
    task, _, _ = row
    try:
        claim_crm_task(db, task=task, actor_user_id=access.user.id)
    except (CRMTaskError, CRMTaskPermissionError) as exc:
        raise _task_error(exc) from exc
    refreshed = _load_task_read(
        db,
        workspace_id=access.workspace.id,
        task_id=task_id,
    )
    assert refreshed is not None
    stored, patient, assigned_user = refreshed
    return _task_read(
        stored,
        patient_first_name=patient.first_name,
        patient_last_name=patient.last_name,
        patient_phone=patient.phone,
        assigned_user=assigned_user,
        now=datetime.now(UTC),
    )


@router.post(
    "/conversations",
    response_model=ConversationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(
    payload: ConversationCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Conversation:
    patient = get_patient_or_404(db, access.workspace.id, payload.patient_id)
    ensure_assignable_user(db, access.workspace.id, payload.assigned_user_id)
    now = datetime.now(UTC)
    needs_human_ownership = payload.assigned_user_id is not None or payload.status == "pending"
    conversation = Conversation(
        workspace_id=access.workspace.id,
        started_at=now,
        owner_type="ai",
        unread_count=0,
        ownership_changed_at=now,
        **payload.model_dump(),
    )
    if payload.status == "closed":
        conversation.closed_at = now
    db.add(conversation)
    db.flush()
    if needs_human_ownership:
        _ensure_crm_handoff(
            db,
            access=access,
            conversation=conversation,
            patient=patient,
            assigned_user_id=payload.assigned_user_id,
            reason=(
                "CRM conversation assigned to a team member."
                if payload.assigned_user_id is not None
                else "CRM conversation moved to pending human review."
            ),
        )
    commit_or_conflict(db, "This external conversation already exists in the workspace.")
    db.refresh(conversation)
    return conversation


@router.get("/conversations", response_model=list[ConversationRead])
def list_conversations(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    conversation_status: Annotated[ConversationStatus | None, Query(alias="status")] = None,
    patient_id: UUID | None = None,
    channel: ConversationChannel | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Conversation]:
    stmt = select(Conversation).where(Conversation.workspace_id == access.workspace.id)
    if conversation_status:
        stmt = stmt.where(Conversation.status == conversation_status)
    if patient_id:
        stmt = stmt.where(Conversation.patient_id == patient_id)
    if channel:
        stmt = stmt.where(Conversation.channel == channel)
    stmt = (
        stmt.order_by(
            Conversation.last_message_at.desc().nullslast(),
            Conversation.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt))


@router.patch("/conversations/{conversation_id}", response_model=ConversationRead)
def update_conversation(
    conversation_id: UUID,
    payload: ConversationUpdate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Conversation:
    conversation = db.scalar(
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == access.workspace.id,
        )
        .with_for_update()
    )
    if conversation is None:
        raise not_found("Conversation")
    updates = payload.model_dump(exclude_unset=True)
    if "status" in updates and updates["status"] is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Conversation status cannot be null.",
        )
    if "assigned_user_id" in updates:
        ensure_assignable_user(db, access.workspace.id, updates["assigned_user_id"])
    ownership_changes_to_human = (
        updates.get("assigned_user_id") is not None
        or updates.get("status") == "pending"
    )
    if ownership_changes_to_human:
        patient = get_patient_or_404(db, access.workspace.id, conversation.patient_id)
        target_assignee_id = updates.get("assigned_user_id", conversation.assigned_user_id)
        _ensure_crm_handoff(
            db,
            access=access,
            conversation=conversation,
            patient=patient,
            assigned_user_id=target_assignee_id,
            reason=(
                "CRM conversation assigned to a team member."
                if target_assignee_id is not None
                else "CRM conversation moved to pending human review."
            ),
        )
    if "status" in updates:
        if updates["status"] == "closed" and conversation.status != "closed":
            conversation.closed_at = datetime.now(UTC)
        elif updates["status"] != "closed":
            conversation.closed_at = None
    for key, value in updates.items():
        setattr(conversation, key, value)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageRead,
    status_code=status.HTTP_201_CREATED,
)
def create_message(
    conversation_id: UUID,
    payload: MessageCreate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Message:
    conversation = db.scalar(
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == access.workspace.id,
        )
        .with_for_update()
    )
    if conversation is None:
        raise not_found("Conversation")

    if payload.sender_type == "ai":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI messages can only be created by the agent runtime.",
        )
    if payload.sender_type == "staff" and payload.direction == "outbound":
        if (
            conversation.owner_type != "human"
            or conversation.assigned_user_id != access.user.id
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Take ownership of this conversation before sending a staff reply.",
            )

    if payload.delivery_status is not None:
        delivery_status = payload.delivery_status
    elif payload.direction == "inbound":
        delivery_status = "received"
    elif payload.direction == "internal":
        delivery_status = "sent"
    else:
        delivery_status = "queued"

    sent_by_user_id = access.user.id if payload.sender_type == "staff" else None
    message = Message(
        workspace_id=access.workspace.id,
        conversation_id=conversation.id,
        channel_connection_id=conversation.channel_connection_id,
        sender_type=payload.sender_type,
        direction=payload.direction,
        message_type=payload.message_type,
        content=payload.content.strip() if payload.content else None,
        external_message_id=payload.external_message_id,
        delivery_status=delivery_status,
        sent_by_user_id=sent_by_user_id,
        metadata_json=payload.metadata,
    )
    db.add(message)

    now = datetime.now(UTC)
    if payload.sender_type == "patient" and payload.direction == "inbound":
        if conversation.status == "closed":
            return_to_ai(conversation, now=now)
        record_customer_inbound(conversation, now=now)
    else:
        conversation.last_message_at = now
        if conversation.status == "closed":
            conversation.status = "open"
            conversation.closed_at = None
    if payload.direction != "internal":
        patient = get_patient_or_404(db, access.workspace.id, conversation.patient_id)
        patient.last_contact_at = now
    db.commit()
    db.refresh(message)
    return message


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageRead],
)
def list_messages(
    conversation_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    before: datetime | None = None,
) -> list[Message]:
    conversation = db.scalar(
        select(Conversation.id).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == access.workspace.id,
        )
    )
    if conversation is None:
        raise not_found("Conversation")

    stmt = select(Message).where(
        Message.workspace_id == access.workspace.id,
        Message.conversation_id == conversation_id,
    )
    if before:
        stmt = stmt.where(Message.created_at < before)
    stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
    messages = list(db.scalars(stmt))
    messages.reverse()
    return messages


def _cohort_read(
    db: Session,
    *,
    cohort: CRMCohort,
    include_members: bool = True,
) -> CRMCohortRead:
    members: list[CRMCohortMemberRead] = []
    if include_members:
        rows = db.execute(
            select(CRMCohortMember, Patient)
            .join(
                Patient,
                (Patient.workspace_id == CRMCohortMember.workspace_id)
                & (Patient.id == CRMCohortMember.patient_id),
            )
            .where(
                CRMCohortMember.workspace_id == cohort.workspace_id,
                CRMCohortMember.cohort_id == cohort.id,
            )
            .order_by(CRMCohortMember.rank, CRMCohortMember.id)
        ).all()
        for member, patient in rows:
            members.append(
                CRMCohortMemberRead(
                    patient_id=patient.id,
                    rank=member.rank,
                    patient_name=" ".join(
                        part for part in (patient.first_name, patient.last_name) if part
                    ).strip()
                    or patient.first_name,
                    patient_phone=patient.phone,
                    snapshot_metrics=list(member.snapshot_metrics_json or []),
                )
            )
    return CRMCohortRead(
        id=cohort.id,
        workspace_id=cohort.workspace_id,
        created_by_user_id=cohort.created_by_user_id,
        name=cohort.name,
        request_id=UUID(cohort.request_key),
        source="analytics_bi",
        status=cohort.status,
        analytics_operation=cohort.analytics_operation,
        question=cohort.question,
        plan=(
            AnalyticsAudiencePlan.model_validate(cohort.plan_json or {})
            if cohort.analytics_operation == "patient_audience"
            else AnalyticsBIPlan.model_validate(cohort.plan_json or {})
        ),
        period_label=cohort.period_label,
        member_count=cohort.member_count,
        created_at=cohort.created_at,
        updated_at=cohort.updated_at,
        members=members,
    )


@router.post(
    "/cohorts/from-analytics",
    response_model=CRMCohortRead,
    status_code=status.HTTP_201_CREATED,
)
def create_cohort_from_analytics(
    payload: AnalyticsCohortCreateRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> CRMCohortRead:
    try:
        cohort = create_analytics_cohort(
            db,
            workspace_id=access.workspace.id,
            created_by_user_id=access.user.id,
            request_id=payload.request_id,
            name=payload.name,
            question=payload.question,
            plan=payload.plan,
        )
    except CRMCohortError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return _cohort_read(db, cohort=cohort)


@router.get("/cohorts", response_model=list[CRMCohortRead])
def list_crm_cohorts(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[CRMCohortRead]:
    cohorts = list(
        db.scalars(
            select(CRMCohort)
            .where(CRMCohort.workspace_id == access.workspace.id)
            .order_by(CRMCohort.created_at.desc(), CRMCohort.id.desc())
            .limit(limit)
        ).all()
    )
    return [_cohort_read(db, cohort=cohort, include_members=False) for cohort in cohorts]


@router.get("/cohorts/{cohort_id}", response_model=CRMCohortRead)
def get_crm_cohort(
    cohort_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> CRMCohortRead:
    cohort = db.scalar(
        select(CRMCohort).where(
            CRMCohort.workspace_id == access.workspace.id,
            CRMCohort.id == cohort_id,
        )
    )
    if cohort is None:
        raise not_found("CRM cohort")
    return _cohort_read(db, cohort=cohort)


@router.post(
    "/cohorts/{cohort_id}/follow-up-tasks",
    response_model=CohortFollowUpResult,
)
def create_follow_up_tasks_for_cohort(
    cohort_id: UUID,
    payload: CohortFollowUpCreateRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> CohortFollowUpResult:
    if (
        payload.assigned_user_id is not None
        and payload.assigned_user_id != access.user.id
        and access.membership.role != WORKSPACE_ROLE_ADMIN
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can assign cohort tasks to another team member.",
        )
    due_at = (
        payload.due_at.replace(tzinfo=_workspace_timezone(access.workspace.timezone))
        if payload.due_at.tzinfo is None or payload.due_at.utcoffset() is None
        else payload.due_at
    )
    try:
        return create_cohort_follow_up_tasks(
            db,
            workspace_id=access.workspace.id,
            cohort_id=cohort_id,
            request_id=payload.request_id,
            actor_user_id=access.user.id,
            assigned_user_id=payload.assigned_user_id,
            title=payload.title,
            description=payload.description,
            priority=payload.priority,
            due_at=due_at,
        )
    except CRMCohortError as exc:
        db.rollback()
        message = str(exc)
        if "not found" in message.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message) from exc
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=message,
        ) from exc


@router.post(
    "/audiences/actions/confirm",
    response_model=AnalyticsAudienceActionResult,
)
def confirm_analytics_audience_action(
    payload: AnalyticsAudienceActionConfirmRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> AnalyticsAudienceActionResult:
    """Materialize a typed patient audience, then run only the confirmed action.

    The browser supplies filters, not trusted patient IDs. Membership is
    re-executed and snapshotted by the backend before any CRM action.
    """
    if (
        payload.assigned_user_id is not None
        and payload.assigned_user_id != access.user.id
        and access.membership.role != WORKSPACE_ROLE_ADMIN
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can assign audience tasks to another team member.",
        )

    due_at = payload.due_at
    if due_at is not None and (due_at.tzinfo is None or due_at.utcoffset() is None):
        due_at = due_at.replace(tzinfo=_workspace_timezone(access.workspace.timezone))
    try:
        audience, follow_up, next_step = execute_confirmed_audience_action(
            db,
            workspace_id=access.workspace.id,
            actor_user_id=access.user.id,
            audience_request_id=payload.audience_request_id,
            action_request_id=payload.request_id,
            name=payload.name,
            question=payload.question,
            plan=payload.plan,
            action_kind=payload.action_kind,
            assigned_user_id=payload.assigned_user_id,
            priority=payload.priority,
            title=payload.title,
            description=payload.description,
            due_at=due_at,
        )
        return AnalyticsAudienceActionResult(
            audience=_cohort_read(db, cohort=audience),
            action_kind=payload.action_kind,
            follow_up=follow_up,
            next_step=next_step,
        )
    except CRMCohortError as exc:
        db.rollback()
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message.lower() else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(status_code=code, detail=message) from exc


def _campaign_read(
    db: Session,
    *,
    campaign: CRMCampaign,
    include_recipients: bool = True,
) -> CRMCampaignRead:
    recipients: list[CRMCampaignRecipientRead] = []
    if include_recipients:
        rows = db.execute(
            select(CRMCampaignRecipient, Patient)
            .join(
                Patient,
                (Patient.workspace_id == CRMCampaignRecipient.workspace_id)
                & (Patient.id == CRMCampaignRecipient.patient_id),
            )
            .where(
                CRMCampaignRecipient.workspace_id == campaign.workspace_id,
                CRMCampaignRecipient.campaign_id == campaign.id,
            )
            .order_by(CRMCampaignRecipient.rank, CRMCampaignRecipient.id)
        ).all()
        for recipient, patient in rows:
            recipients.append(
                CRMCampaignRecipientRead(
                    id=recipient.id,
                    patient_id=patient.id,
                    rank=recipient.rank,
                    patient_name=" ".join(
                        part for part in (patient.first_name, patient.last_name) if part
                    ).strip() or patient.first_name,
                    patient_phone=patient.phone,
                    status=recipient.status,
                    reason=recipient.reason,
                    message_id=recipient.message_id,
                    dispatch_id=recipient.dispatch_id,
                    scheduled_at=recipient.scheduled_at,
                )
            )
    return CRMCampaignRead(
        id=campaign.id,
        workspace_id=campaign.workspace_id,
        cohort_id=campaign.cohort_id,
        channel_connection_id=campaign.channel_connection_id,
        created_by_user_id=campaign.created_by_user_id,
        confirmed_by_user_id=campaign.confirmed_by_user_id,
        request_id=UUID(campaign.request_key),
        confirmation_id=UUID(campaign.confirmation_key) if campaign.confirmation_key else None,
        name=campaign.name,
        status=campaign.status,
        template_name=campaign.template_name,
        template_language=campaign.template_language,
        body_parameter_keys=list(campaign.body_parameter_keys_json or []),
        rate_limit_per_minute=campaign.rate_limit_per_minute,
        recipient_count=campaign.recipient_count,
        eligible_count=campaign.eligible_count,
        confirmed_at=campaign.confirmed_at,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
        recipients=recipients,
    )


@router.post(
    "/cohorts/{cohort_id}/campaigns",
    response_model=CRMCampaignRead,
    status_code=status.HTTP_201_CREATED,
)
def prepare_campaign_for_cohort(
    cohort_id: UUID,
    payload: CohortCampaignPrepareRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> CRMCampaignRead:
    try:
        campaign = prepare_cohort_campaign(
            db,
            workspace_id=access.workspace.id,
            cohort_id=cohort_id,
            created_by_user_id=access.user.id,
            request_id=payload.request_id,
            name=payload.name,
            channel_connection_id=payload.channel_connection_id,
            template_name=payload.template_name,
            template_language=payload.template_language,
            body_parameter_keys=list(payload.body_parameter_keys),
            rate_limit_per_minute=payload.rate_limit_per_minute,
        )
    except CRMCampaignError as exc:
        db.rollback()
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message.lower() else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(status_code=code, detail=message) from exc
    return _campaign_read(db, campaign=campaign)


@router.get("/cohorts/{cohort_id}/campaigns", response_model=list[CRMCampaignRead])
def list_campaigns_for_cohort(
    cohort_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> list[CRMCampaignRead]:
    rows = list(
        db.scalars(
            select(CRMCampaign)
            .where(
                CRMCampaign.workspace_id == access.workspace.id,
                CRMCampaign.cohort_id == cohort_id,
            )
            .order_by(CRMCampaign.created_at.desc(), CRMCampaign.id.desc())
        ).all()
    )
    return [_campaign_read(db, campaign=row, include_recipients=False) for row in rows]


@router.get("/campaigns/{campaign_id}", response_model=CRMCampaignRead)
def get_crm_campaign(
    campaign_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> CRMCampaignRead:
    campaign = db.scalar(
        select(CRMCampaign).where(
            CRMCampaign.workspace_id == access.workspace.id,
            CRMCampaign.id == campaign_id,
        )
    )
    if campaign is None:
        raise not_found("CRM campaign")
    return _campaign_read(db, campaign=campaign)


@router.post(
    "/campaigns/{campaign_id}/confirm",
    response_model=CohortCampaignConfirmResult,
)
def confirm_crm_campaign(
    campaign_id: UUID,
    payload: CohortCampaignConfirmRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> CohortCampaignConfirmResult:
    try:
        result = confirm_cohort_campaign(
            db,
            workspace_id=access.workspace.id,
            campaign_id=campaign_id,
            confirmation_id=payload.confirmation_id,
            actor_user_id=access.user.id,
        )
        return CohortCampaignConfirmResult.model_validate(result)
    except CRMCampaignError as exc:
        db.rollback()
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message.lower() else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(status_code=code, detail=message) from exc

