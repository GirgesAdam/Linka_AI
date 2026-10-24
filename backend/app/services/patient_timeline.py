from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session, aliased

from app.models.appointment import Appointment
from app.models.appointment_status_history import AppointmentStatusHistory
from app.models.branch import Branch
from app.models.conversation import Conversation
from app.models.crm_task import CRMTask
from app.models.doctor import Doctor
from app.models.handoff_event import HandoffEvent
from app.models.handoff_request import HandoffRequest
from app.models.lead import Lead
from app.models.message import Message
from app.models.patient import Patient
from app.models.patient_note import PatientNote
from app.models.patient_tag import PatientTag, PatientTagAssignment
from app.models.payment_transaction import PaymentTransaction
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.schemas.crm import (
    PatientCRMStats,
    PatientNoteRead,
    PatientProfilePatientRead,
    PatientProfileRead,
    PatientTagRead,
    PatientTimelineAppointment,
    PatientTimelineEvent,
    PatientTimelineHandoff,
    PatientTimelineMessage,
    PatientTimelineNote,
    PatientTimelinePayment,
    PatientTimelineTask,
)

_ACTIVE_APPOINTMENT_STATUSES = ("pending", "confirmed", "checked_in", "in_progress")
_ACTIVE_LEAD_STATUSES = ("new", "contacted", "qualified", "booked")
_ACTIVE_HANDOFF_STATUSES = ("pending", "claimed")


def _display_user_name(user: User | None) -> str | None:
    if user is None:
        return None
    return user.full_name or user.email


def _display_doctor_name(first_name: str | None, last_name: str | None) -> str:
    name = " ".join(part for part in (first_name, last_name) if part).strip()
    return name or "Doctor"


def merge_timeline_events(
    groups: list[list[PatientTimelineEvent]],
    *,
    limit: int,
) -> list[PatientTimelineEvent]:
    events = [event for group in groups for event in group]
    events.sort(key=lambda event: (event.occurred_at, event.id), reverse=True)
    return events[:limit]


def _appointment_context(
    appointment: Appointment,
    *,
    service_name: str,
    branch_name: str,
    doctor_name: str,
    from_status: str | None = None,
    to_status: str | None = None,
    reason: str | None = None,
) -> PatientTimelineAppointment:
    return PatientTimelineAppointment(
        id=appointment.id,
        status=appointment.status,
        start_at=appointment.start_at,
        end_at=appointment.end_at,
        service_name=service_name,
        branch_name=branch_name,
        doctor_name=doctor_name,
        price_minor=appointment.price_minor,
        currency=appointment.currency,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
    )


def _build_stats_and_latest_conversation(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    now: datetime,
) -> tuple[PatientCRMStats, UUID | None]:
    appointment_stats = (
        select(
            func.count(Appointment.id).label("total_appointments"),
            func.coalesce(
                func.sum(case((Appointment.status == "completed", 1), else_=0)),
                0,
            ).label("completed_appointments"),
            func.coalesce(
                func.sum(case((Appointment.status == "no_show", 1), else_=0)),
                0,
            ).label("no_show_appointments"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                Appointment.status.in_(_ACTIVE_APPOINTMENT_STATUSES),
                                Appointment.start_at >= now,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("upcoming_appointments"),
            func.min(
                case(
                    (
                        and_(
                            Appointment.status.in_(_ACTIVE_APPOINTMENT_STATUSES),
                            Appointment.start_at >= now,
                        ),
                        Appointment.start_at,
                    ),
                    else_=None,
                )
            ).label("next_appointment_at"),
            func.max(
                case(
                    (Appointment.start_at < now, Appointment.start_at),
                    else_=None,
                )
            ).label("last_appointment_at"),
        )
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.patient_id == patient_id,
        )
        .subquery()
    )
    conversation_stats = (
        select(
            func.count(Conversation.id).label("total_conversations"),
            func.coalesce(
                func.sum(case((Conversation.status != "closed", 1), else_=0)),
                0,
            ).label("open_conversations"),
        )
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.patient_id == patient_id,
        )
        .subquery()
    )
    active_handoffs = (
        select(func.count(HandoffRequest.id))
        .where(
            HandoffRequest.workspace_id == workspace_id,
            HandoffRequest.patient_id == patient_id,
            HandoffRequest.status.in_(_ACTIVE_HANDOFF_STATUSES),
        )
        .scalar_subquery()
    )
    active_leads = (
        select(func.count(Lead.id))
        .where(
            Lead.workspace_id == workspace_id,
            Lead.patient_id == patient_id,
            Lead.status.in_(_ACTIVE_LEAD_STATUSES),
        )
        .scalar_subquery()
    )
    open_tasks = (
        select(func.count(CRMTask.id))
        .where(
            CRMTask.workspace_id == workspace_id,
            CRMTask.patient_id == patient_id,
            CRMTask.status.in_(("pending", "in_progress")),
        )
        .scalar_subquery()
    )
    overdue_tasks = (
        select(func.count(CRMTask.id))
        .where(
            CRMTask.workspace_id == workspace_id,
            CRMTask.patient_id == patient_id,
            CRMTask.status.in_(("pending", "in_progress")),
            CRMTask.due_at < now,
        )
        .scalar_subquery()
    )
    next_task_at = (
        select(func.min(CRMTask.due_at))
        .where(
            CRMTask.workspace_id == workspace_id,
            CRMTask.patient_id == patient_id,
            CRMTask.status.in_(("pending", "in_progress")),
        )
        .scalar_subquery()
    )
    latest_conversation_id = (
        select(Conversation.id)
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.patient_id == patient_id,
        )
        .order_by(
            func.coalesce(Conversation.last_message_at, Conversation.started_at).desc(),
            Conversation.created_at.desc(),
        )
        .limit(1)
        .scalar_subquery()
    )

    row = db.execute(
        select(
            appointment_stats.c.total_appointments,
            appointment_stats.c.completed_appointments,
            appointment_stats.c.no_show_appointments,
            appointment_stats.c.upcoming_appointments,
            appointment_stats.c.next_appointment_at,
            appointment_stats.c.last_appointment_at,
            conversation_stats.c.total_conversations,
            conversation_stats.c.open_conversations,
            active_handoffs.label("active_handoffs"),
            active_leads.label("active_leads"),
            open_tasks.label("open_tasks"),
            overdue_tasks.label("overdue_tasks"),
            next_task_at.label("next_task_at"),
            latest_conversation_id.label("latest_conversation_id"),
        )
    ).one()

    return (
        PatientCRMStats(
            total_appointments=int(row.total_appointments or 0),
            completed_appointments=int(row.completed_appointments or 0),
            no_show_appointments=int(row.no_show_appointments or 0),
            upcoming_appointments=int(row.upcoming_appointments or 0),
            next_appointment_at=row.next_appointment_at,
            last_appointment_at=row.last_appointment_at,
            total_conversations=int(row.total_conversations or 0),
            open_conversations=int(row.open_conversations or 0),
            active_handoffs=int(row.active_handoffs or 0),
            active_leads=int(row.active_leads or 0),
            open_tasks=int(row.open_tasks or 0),
            overdue_tasks=int(row.overdue_tasks or 0),
            next_task_at=row.next_task_at,
        ),
        row.latest_conversation_id,
    )


def _load_note_rows(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    limit: int,
) -> list[tuple[PatientNote, User | None]]:
    author = aliased(User)
    return list(
        db.execute(
            select(PatientNote, author)
            .outerjoin(author, author.id == PatientNote.author_user_id)
            .where(
                PatientNote.workspace_id == workspace_id,
                PatientNote.patient_id == patient_id,
            )
            .order_by(PatientNote.created_at.desc())
            .limit(limit)
        ).all()
    )


def _build_note_events(
    rows: list[tuple[PatientNote, User | None]],
) -> list[PatientTimelineEvent]:
    return [
        PatientTimelineEvent(
            id=f"note:{note.id}",
            kind="note",
            occurred_at=note.created_at,
            actor_type="staff" if note.author_user_id else "system",
            actor_user_id=note.author_user_id,
            actor_name=_display_user_name(user),
            note=PatientTimelineNote(
                id=note.id,
                note_type=note.note_type,
                content=note.content,
                is_pinned=note.is_pinned,
            ),
        )
        for note, user in rows
    ]


def _build_appointment_events(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    limit: int,
) -> list[PatientTimelineEvent]:
    creator = aliased(User)
    rows = db.execute(
        select(
            Appointment,
            Service.name,
            Branch.name,
            Staff.first_name,
            Staff.last_name,
            creator,
        )
        .join(Service, Service.id == Appointment.service_id)
        .join(Branch, Branch.id == Appointment.branch_id)
        .join(Doctor, Doctor.id == Appointment.doctor_id)
        .join(Staff, Staff.id == Doctor.staff_id)
        .outerjoin(creator, creator.id == Appointment.created_by_user_id)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.patient_id == patient_id,
        )
        .order_by(Appointment.created_at.desc())
        .limit(limit)
    ).all()

    events: list[PatientTimelineEvent] = []
    for appointment, service_name, branch_name, first_name, last_name, user in rows:
        actor_type = "ai" if appointment.source == "ai" else (
            "staff" if appointment.created_by_user_id else "system"
        )
        events.append(
            PatientTimelineEvent(
                id=f"appointment:{appointment.id}:created",
                kind="appointment",
                occurred_at=appointment.created_at,
                actor_type=actor_type,
                actor_user_id=appointment.created_by_user_id,
                actor_name=_display_user_name(user),
                appointment=_appointment_context(
                    appointment,
                    service_name=service_name,
                    branch_name=branch_name,
                    doctor_name=_display_doctor_name(first_name, last_name),
                ),
            )
        )
    return events


def _build_appointment_status_events(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    limit: int,
) -> list[PatientTimelineEvent]:
    actor = aliased(User)
    rows = db.execute(
        select(
            AppointmentStatusHistory,
            Appointment,
            Service.name,
            Branch.name,
            Staff.first_name,
            Staff.last_name,
            actor,
        )
        .join(Appointment, Appointment.id == AppointmentStatusHistory.appointment_id)
        .join(Service, Service.id == Appointment.service_id)
        .join(Branch, Branch.id == Appointment.branch_id)
        .join(Doctor, Doctor.id == Appointment.doctor_id)
        .join(Staff, Staff.id == Doctor.staff_id)
        .outerjoin(actor, actor.id == AppointmentStatusHistory.changed_by_user_id)
        .where(
            AppointmentStatusHistory.workspace_id == workspace_id,
            Appointment.workspace_id == workspace_id,
            Appointment.patient_id == patient_id,
            AppointmentStatusHistory.from_status.is_not(None),
        )
        .order_by(AppointmentStatusHistory.created_at.desc())
        .limit(limit)
    ).all()

    events: list[PatientTimelineEvent] = []
    for history, appointment, service_name, branch_name, first_name, last_name, user in rows:
        events.append(
            PatientTimelineEvent(
                id=f"appointment-history:{history.id}",
                kind="appointment_status",
                occurred_at=history.created_at,
                actor_type="staff" if history.changed_by_user_id else "system",
                actor_user_id=history.changed_by_user_id,
                actor_name=_display_user_name(user),
                appointment=_appointment_context(
                    appointment,
                    service_name=service_name,
                    branch_name=branch_name,
                    doctor_name=_display_doctor_name(first_name, last_name),
                    from_status=history.from_status,
                    to_status=history.to_status,
                    reason=history.reason,
                ),
            )
        )
    return events


def _build_message_events(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    limit: int,
) -> list[PatientTimelineEvent]:
    sender = aliased(User)
    rows = db.execute(
        select(Message, Conversation.channel, sender)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .outerjoin(sender, sender.id == Message.sent_by_user_id)
        .where(
            Message.workspace_id == workspace_id,
            Conversation.workspace_id == workspace_id,
            Conversation.patient_id == patient_id,
            Message.direction != "internal",
        )
        .order_by(Message.created_at.desc())
        .limit(limit)
    ).all()

    return [
        PatientTimelineEvent(
            id=f"message:{message.id}",
            kind="message",
            occurred_at=message.created_at,
            actor_type=message.sender_type,
            actor_user_id=message.sent_by_user_id,
            actor_name=_display_user_name(user),
            message=PatientTimelineMessage(
                id=message.id,
                conversation_id=message.conversation_id,
                sender_type=message.sender_type,
                direction=message.direction,
                message_type=message.message_type,
                content=message.content,
                delivery_status=message.delivery_status,
                channel=channel,
            ),
        )
        for message, channel, user in rows
    ]


def _build_handoff_events(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    limit: int,
) -> list[PatientTimelineEvent]:
    actor = aliased(User)
    rows = db.execute(
        select(HandoffEvent, HandoffRequest, actor)
        .join(HandoffRequest, HandoffRequest.id == HandoffEvent.handoff_request_id)
        .outerjoin(actor, actor.id == HandoffEvent.actor_user_id)
        .where(
            HandoffEvent.workspace_id == workspace_id,
            HandoffRequest.workspace_id == workspace_id,
            HandoffRequest.patient_id == patient_id,
        )
        .order_by(HandoffEvent.created_at.desc())
        .limit(limit)
    ).all()

    return [
        PatientTimelineEvent(
            id=f"handoff-event:{event.id}",
            kind="handoff",
            occurred_at=event.created_at,
            actor_type=event.actor_type,
            actor_user_id=event.actor_user_id,
            actor_name=_display_user_name(user),
            handoff=PatientTimelineHandoff(
                id=handoff.id,
                conversation_id=handoff.conversation_id,
                event_type=event.event_type,
                status=handoff.status,
                category=handoff.category,
                priority=handoff.priority,
                reason=handoff.reason,
            ),
        )
        for event, handoff, user in rows
    ]


def _build_task_events(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    limit: int,
) -> list[PatientTimelineEvent]:
    creator = aliased(User)
    completer = aliased(User)
    rows = db.execute(
        select(CRMTask, creator, completer)
        .outerjoin(creator, creator.id == CRMTask.created_by_user_id)
        .outerjoin(completer, completer.id == CRMTask.completed_by_user_id)
        .where(
            CRMTask.workspace_id == workspace_id,
            CRMTask.patient_id == patient_id,
        )
        .order_by(CRMTask.created_at.desc())
        .limit(limit)
    ).all()

    events: list[PatientTimelineEvent] = []
    for task, created_by, completed_by in rows:
        created_actor_type = "ai" if task.source == "ai" else (
            "staff" if task.created_by_user_id else "system"
        )
        events.append(
            PatientTimelineEvent(
                id=f"task:{task.id}:created",
                kind="task",
                occurred_at=task.created_at,
                actor_type=created_actor_type,
                actor_user_id=task.created_by_user_id,
                actor_name=_display_user_name(created_by),
                task=PatientTimelineTask(
                    id=task.id,
                    event_type="created",
                    status=task.status,
                    priority=task.priority,
                    task_type=task.task_type,
                    title=task.title,
                    due_at=task.due_at,
                    assigned_user_id=task.assigned_user_id,
                ),
            )
        )
        if task.completed_at is not None:
            events.append(
                PatientTimelineEvent(
                    id=f"task:{task.id}:completed",
                    kind="task",
                    occurred_at=task.completed_at,
                    actor_type="staff" if task.completed_by_user_id else "system",
                    actor_user_id=task.completed_by_user_id,
                    actor_name=_display_user_name(completed_by),
                    task=PatientTimelineTask(
                        id=task.id,
                        event_type="completed",
                        status=task.status,
                        priority=task.priority,
                        task_type=task.task_type,
                        title=task.title,
                        due_at=task.due_at,
                        assigned_user_id=task.assigned_user_id,
                    ),
                )
            )
    return events



def _build_payment_events(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    limit: int,
) -> list[PatientTimelineEvent]:
    actor = aliased(User)
    rows = db.execute(
        select(PaymentTransaction, actor)
        .outerjoin(actor, actor.id == PaymentTransaction.created_by_user_id)
        .where(
            PaymentTransaction.workspace_id == workspace_id,
            PaymentTransaction.patient_id == patient_id,
        )
        .order_by(PaymentTransaction.created_at.desc())
        .limit(limit)
    ).all()
    return [
        PatientTimelineEvent(
            id=f"payment:{transaction.id}",
            kind="payment",
            occurred_at=transaction.created_at,
            actor_type=(
                "staff"
                if transaction.created_by_user_id
                else "system"
            ),
            actor_user_id=transaction.created_by_user_id,
            actor_name=_display_user_name(user),
            payment=PatientTimelinePayment(
                id=transaction.id,
                appointment_id=transaction.appointment_id,
                transaction_type=transaction.transaction_type,
                amount_minor=transaction.amount_minor,
                currency=transaction.currency,
                payment_method=transaction.payment_method,
                reference_transaction_id=transaction.reference_transaction_id,
                reason=transaction.reason,
            ),
        )
        for transaction, user in rows
    ]

def build_patient_profile(
    db: Session,
    *,
    workspace_id: UUID,
    patient: Patient,
    timeline_limit: int = 50,
) -> PatientProfileRead:
    now = datetime.now(UTC)
    per_source_limit = max(timeline_limit, 1)

    tags = list(
        db.scalars(
            select(PatientTag)
            .join(PatientTagAssignment, PatientTagAssignment.tag_id == PatientTag.id)
            .where(
                PatientTagAssignment.workspace_id == workspace_id,
                PatientTagAssignment.patient_id == patient.id,
                PatientTag.workspace_id == workspace_id,
                PatientTag.is_active.is_(True),
            )
            .order_by(PatientTag.name)
        )
    )

    note_rows = _load_note_rows(
        db,
        workspace_id=workspace_id,
        patient_id=patient.id,
        limit=max(12, per_source_limit),
    )
    notes = [note for note, _ in note_rows[:12]]
    stats, latest_conversation_id = _build_stats_and_latest_conversation(
        db,
        workspace_id=workspace_id,
        patient_id=patient.id,
        now=now,
    )

    patient_created = PatientTimelineEvent(
        id=f"patient:{patient.id}:created",
        kind="patient_created",
        occurred_at=patient.created_at,
        actor_type="system",
    )
    timeline = merge_timeline_events(
        [
            [patient_created],
            _build_note_events(note_rows[:per_source_limit]),
            _build_appointment_events(
                db,
                workspace_id=workspace_id,
                patient_id=patient.id,
                limit=per_source_limit,
            ),
            _build_appointment_status_events(
                db,
                workspace_id=workspace_id,
                patient_id=patient.id,
                limit=per_source_limit,
            ),
            _build_message_events(
                db,
                workspace_id=workspace_id,
                patient_id=patient.id,
                limit=per_source_limit,
            ),
            _build_handoff_events(
                db,
                workspace_id=workspace_id,
                patient_id=patient.id,
                limit=per_source_limit,
            ),
            _build_task_events(
                db,
                workspace_id=workspace_id,
                patient_id=patient.id,
                limit=per_source_limit,
            ),
            _build_payment_events(
                db,
                workspace_id=workspace_id,
                patient_id=patient.id,
                limit=per_source_limit,
            ),
        ],
        limit=timeline_limit,
    )

    return PatientProfileRead(
        patient=PatientProfilePatientRead.model_validate(patient),
        stats=stats,
        tags=[PatientTagRead.model_validate(tag) for tag in tags],
        notes=[PatientNoteRead.model_validate(note) for note in notes],
        timeline=timeline,
        latest_conversation_id=latest_conversation_id,
    )
