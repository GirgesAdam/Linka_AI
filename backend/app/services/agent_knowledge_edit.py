from __future__ import annotations

from collections import defaultdict
from datetime import time
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.service import Service
from app.models.staff import Staff
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace
from app.schemas.agent_knowledge import (
    KnowledgeEditAction,
    KnowledgeEditApplyResponse,
    KnowledgeEditProposal,
    KnowledgeFieldChange,
)
from app.services.activity import record_activity_event
from app.services.agent_knowledge import (
    agent_knowledge_configuration_fingerprint,
    build_agent_knowledge_snapshot,
)


class KnowledgeEditError(RuntimeError):
    pass


class KnowledgeEditConflictError(KnowledgeEditError):
    pass


_SERVICE_FIELDS = {"name", "category", "description", "duration_minutes", "price_egp", "requires_medical_review", "is_active"}
_BRANCH_FIELDS = {"name", "city", "address_line1", "phone", "timezone", "is_active"}
_DOCTOR_FIELDS = {"first_name", "last_name", "phone", "email", "specialization", "booking_enabled", "is_active"}
_BOOKING_FIELDS = {
    "slot_interval_minutes", "minimum_notice_minutes", "booking_horizon_days",
    "cancellation_notice_minutes", "allow_same_day_booking", "require_confirmation",
}


def _uuid(value: str | None, label: str) -> UUID:
    try:
        return UUID(str(value or ""))
    except ValueError as exc:
        raise KnowledgeEditError(f"Invalid {label} id.") from exc


def _change_value(change: KnowledgeFieldChange):
    if change.text_value is not None:
        return change.text_value.strip()
    if change.number_value is not None:
        return change.number_value
    return change.bool_value


def _validate_changes(action: KnowledgeEditAction, allowed: set[str]) -> None:
    if not action.changes:
        raise KnowledgeEditError(f"{action.kind} requires at least one field change.")
    for change in action.changes:
        if change.field not in allowed:
            raise KnowledgeEditError(f"Field {change.field} is not allowed for {action.kind}.")
        value = _change_value(change)
        if change.field == "duration_minutes" and not (1 <= int(round(float(value))) <= 1440):
            raise KnowledgeEditError("Service duration must be between 1 and 1440 minutes.")
        if change.field == "price_egp" and float(value) < 0:
            raise KnowledgeEditError("Service price cannot be negative.")
        if change.field == "slot_interval_minutes" and not (1 <= int(round(float(value))) <= 240):
            raise KnowledgeEditError("Booking slot interval must be between 1 and 240 minutes.")
        if change.field in {"minimum_notice_minutes", "cancellation_notice_minutes"} and float(value) < 0:
            raise KnowledgeEditError("Booking notice values cannot be negative.")
        if change.field == "booking_horizon_days" and not (1 <= int(round(float(value))) <= 730):
            raise KnowledgeEditError("Booking horizon must be between 1 and 730 days.")


def _validate_schedule(action: KnowledgeEditAction) -> None:
    by_day: dict[int, list[tuple[time, time]]] = defaultdict(list)
    for interval in action.schedule:
        by_day[interval.weekday].append((time.fromisoformat(interval.start_time), time.fromisoformat(interval.end_time)))
    for intervals in by_day.values():
        ordered = sorted(intervals)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current[0] < previous[1]:
                raise KnowledgeEditError("Working-hour intervals cannot overlap on the same weekday.")


def _catalog(snapshot) -> dict:
    return {
        "workspace": {"name": snapshot.workspace_name, "timezone": snapshot.workspace_timezone},
        "services": [
            {"id": str(row.id), "name": row.name, "category": row.category, "duration_minutes": row.duration_minutes,
             "price_egp": row.price_minor / 100, "active": row.is_active}
            for row in snapshot.services
        ],
        "branches": [
            {"id": str(row.id), "name": row.name, "city": row.city, "address": row.address_line1,
             "timezone": row.timezone, "active": row.is_active,
             "working_hours": [item.model_dump(mode="json") for item in row.working_hours]}
            for row in snapshot.branches
        ],
        "doctors": [
            {"id": str(row.id), "name": row.name, "specialization": row.specialization,
             "phone": row.phone, "email": row.email, "booking_enabled": row.booking_enabled, "active": row.is_active,
             "branches": [{"id": str(x.id), "name": x.name, "primary": x.is_primary} for x in row.branches],
             "services": [{"id": str(x.id), "name": x.name} for x in row.services],
             "schedules": [
                 {"branch_id": str(x.branch_id), "branch_name": x.branch_name,
                  "working_hours": [h.model_dump(mode="json") for h in x.working_hours]}
                 for x in row.schedules
             ]}
            for row in snapshot.doctors
        ],
        "booking_settings": snapshot.booking_settings.model_dump(mode="json") if snapshot.booking_settings else None,
    }


def _name_maps(snapshot):
    return (
        {str(x.id): x.name for x in snapshot.services},
        {str(x.id): x.name for x in snapshot.branches},
        {str(x.id): x.name for x in snapshot.doctors},
    )


def _validate_actions(snapshot, actions: list[KnowledgeEditAction]) -> list[str]:
    services = {str(x.id) for x in snapshot.services}
    branches = {str(x.id) for x in snapshot.branches}
    doctors = {str(x.id) for x in snapshot.doctors}
    service_names, branch_names, doctor_names = _name_maps(snapshot)
    previews: list[str] = []

    for action in actions:
        if action.kind == "update_service":
            _validate_changes(action, _SERVICE_FIELDS)
            if str(action.entity_id) not in services:
                raise KnowledgeEditError("The requested service is not in this workspace.")
            label = service_names[str(action.entity_id)]
            previews.append(f"تعديل خدمة «{label}»: " + "، ".join(f"{c.field} = {_change_value(c)}" for c in action.changes))
        elif action.kind == "update_branch":
            _validate_changes(action, _BRANCH_FIELDS)
            if str(action.entity_id) not in branches:
                raise KnowledgeEditError("The requested branch is not in this workspace.")
            label = branch_names[str(action.entity_id)]
            previews.append(f"تعديل فرع «{label}»: " + "، ".join(f"{c.field} = {_change_value(c)}" for c in action.changes))
        elif action.kind == "update_doctor":
            _validate_changes(action, _DOCTOR_FIELDS)
            if str(action.entity_id) not in doctors:
                raise KnowledgeEditError("The requested doctor is not in this workspace.")
            label = doctor_names[str(action.entity_id)]
            previews.append(f"تعديل د. «{label}»: " + "، ".join(f"{c.field} = {_change_value(c)}" for c in action.changes))
        elif action.kind == "set_branch_hours":
            _validate_schedule(action)
            if str(action.entity_id) not in branches:
                raise KnowledgeEditError("The requested branch is not in this workspace.")
            previews.append(f"استبدال مواعيد فرع «{branch_names[str(action.entity_id)]}» بالأسبوع المقترح.")
        elif action.kind == "set_doctor_hours":
            _validate_schedule(action)
            if str(action.entity_id) not in doctors or str(action.branch_id) not in branches:
                raise KnowledgeEditError("The requested doctor or branch is not in this workspace.")
            previews.append(f"استبدال مواعيد د. «{doctor_names[str(action.entity_id)]}» في «{branch_names[str(action.branch_id)]}».")
        elif action.kind == "set_doctor_services":
            if str(action.entity_id) not in doctors or any(str(x) not in services for x in action.related_ids):
                raise KnowledgeEditError("The requested doctor/service relationship is invalid.")
            related = "، ".join(service_names[str(x)] for x in action.related_ids) or "بدون خدمات"
            previews.append(f"خدمات د. «{doctor_names[str(action.entity_id)]}» تصبح: {related}.")
        elif action.kind == "set_doctor_branches":
            if str(action.entity_id) not in doctors or any(str(x) not in branches for x in action.related_ids):
                raise KnowledgeEditError("The requested doctor/branch relationship is invalid.")
            if action.primary_branch_id and action.primary_branch_id not in action.related_ids:
                raise KnowledgeEditError("Primary branch must be included in the doctor branches.")
            related = "، ".join(branch_names[str(x)] for x in action.related_ids) or "بدون فروع"
            previews.append(f"فروع د. «{doctor_names[str(action.entity_id)]}» تصبح: {related}.")
        elif action.kind == "update_booking_settings":
            _validate_changes(action, _BOOKING_FIELDS)
            previews.append("تعديل إعدادات الحجز: " + "، ".join(f"{c.field} = {_change_value(c)}" for c in action.changes))
        else:
            raise KnowledgeEditError("Unsupported knowledge edit action.")
    return previews


def propose_agent_knowledge_edit(db: Session, workspace: Workspace, message: str) -> KnowledgeEditProposal:
    from app.agents.clinic_knowledge_editor import propose_knowledge_edit

    snapshot = build_agent_knowledge_snapshot(db, workspace)
    decision = propose_knowledge_edit(message=message, catalog=_catalog(snapshot))
    if decision.needs_clarification or not decision.understood or not decision.actions:
        return KnowledgeEditProposal(
            base_fingerprint=agent_knowledge_configuration_fingerprint(snapshot),
            assistant_message=decision.assistant_message,
            preview_lines=[],
            actions=[],
            requires_confirmation=False,
            clarification_question=decision.clarification_question or decision.assistant_message,
        )
    previews = _validate_actions(snapshot, decision.actions)
    return KnowledgeEditProposal(
        base_fingerprint=agent_knowledge_configuration_fingerprint(snapshot),
        assistant_message=decision.assistant_message,
        preview_lines=previews,
        actions=decision.actions,
        requires_confirmation=True,
        clarification_question=None,
    )


def _apply_field_changes(obj, changes: list[KnowledgeFieldChange], *, kind: str) -> None:
    for change in changes:
        value = _change_value(change)
        field = change.field
        if kind == "service" and field == "price_egp":
            value = int(round(float(value) * 100))
            field = "price_minor"
        elif field in {"duration_minutes", "slot_interval_minutes", "minimum_notice_minutes", "booking_horizon_days", "cancellation_notice_minutes"}:
            value = int(round(float(value)))
        setattr(obj, field, value)


def apply_agent_knowledge_edit(
    db: Session,
    workspace: Workspace,
    *,
    base_fingerprint: str,
    actions: list[KnowledgeEditAction],
    actor_user_id: UUID | None = None,
) -> KnowledgeEditApplyResponse:
    snapshot = build_agent_knowledge_snapshot(db, workspace)
    if agent_knowledge_configuration_fingerprint(snapshot) != base_fingerprint:
        raise KnowledgeEditConflictError("Clinic knowledge changed after the proposal. Ask Linka to propose the edit again.")
    _validate_actions(snapshot, actions)
    wid = workspace.id

    try:
        for action in actions:
            if action.kind == "update_service":
                row = db.scalar(select(Service).where(Service.workspace_id == wid, Service.id == _uuid(action.entity_id, "service")))
                if row is None:
                    raise KnowledgeEditConflictError("Service no longer exists.")
                _apply_field_changes(row, action.changes, kind="service")
            elif action.kind == "update_branch":
                row = db.scalar(select(Branch).where(Branch.workspace_id == wid, Branch.id == _uuid(action.entity_id, "branch")))
                if row is None:
                    raise KnowledgeEditConflictError("Branch no longer exists.")
                _apply_field_changes(row, action.changes, kind="branch")
            elif action.kind == "update_doctor":
                doctor = db.scalar(select(Doctor).where(Doctor.workspace_id == wid, Doctor.id == _uuid(action.entity_id, "doctor")))
                if doctor is None:
                    raise KnowledgeEditConflictError("Doctor no longer exists.")
                staff = db.scalar(select(Staff).where(Staff.workspace_id == wid, Staff.id == doctor.staff_id))
                if staff is None:
                    raise KnowledgeEditConflictError(
                        "Doctor staff record no longer exists."
                    )
                staff_changes = [c for c in action.changes if c.field in {"first_name", "last_name", "phone", "email"}]
                doctor_changes = [c for c in action.changes if c.field not in {"first_name", "last_name", "phone", "email"}]
                _apply_field_changes(staff, staff_changes, kind="staff")
                _apply_field_changes(doctor, doctor_changes, kind="doctor")
            elif action.kind == "set_branch_hours":
                branch_id = _uuid(action.entity_id, "branch")
                db.execute(delete(BranchWorkingHour).where(BranchWorkingHour.workspace_id == wid, BranchWorkingHour.branch_id == branch_id))
                db.add_all([BranchWorkingHour(workspace_id=wid, branch_id=branch_id, weekday=x.weekday, start_time=time.fromisoformat(x.start_time), end_time=time.fromisoformat(x.end_time)) for x in action.schedule])
            elif action.kind == "set_doctor_hours":
                doctor_id, branch_id = _uuid(action.entity_id, "doctor"), _uuid(action.branch_id, "branch")
                assignment = db.scalar(select(DoctorBranch).where(DoctorBranch.workspace_id == wid, DoctorBranch.doctor_id == doctor_id, DoctorBranch.branch_id == branch_id, DoctorBranch.is_active.is_(True)))
                if assignment is None:
                    raise KnowledgeEditConflictError(
                        "Doctor must be assigned to the branch before setting hours."
                    )
                db.execute(delete(DoctorWorkingHour).where(DoctorWorkingHour.workspace_id == wid, DoctorWorkingHour.doctor_id == doctor_id, DoctorWorkingHour.branch_id == branch_id))
                db.add_all([DoctorWorkingHour(workspace_id=wid, doctor_id=doctor_id, branch_id=branch_id, weekday=x.weekday, start_time=time.fromisoformat(x.start_time), end_time=time.fromisoformat(x.end_time)) for x in action.schedule])
            elif action.kind == "set_doctor_services":
                doctor_id = _uuid(action.entity_id, "doctor")
                desired = {_uuid(x, "service") for x in action.related_ids}
                existing = list(db.scalars(select(DoctorService).where(DoctorService.workspace_id == wid, DoctorService.doctor_id == doctor_id)))
                by_service = {x.service_id: x for x in existing}
                for service_id, row in by_service.items():
                    row.is_active = service_id in desired
                for service_id in desired - set(by_service):
                    db.add(DoctorService(workspace_id=wid, doctor_id=doctor_id, service_id=service_id, custom_duration_minutes=None, custom_price_minor=None, is_active=True))
            elif action.kind == "set_doctor_branches":
                doctor_id = _uuid(action.entity_id, "doctor")
                desired = {_uuid(x, "branch") for x in action.related_ids}
                primary = _uuid(action.primary_branch_id, "branch") if action.primary_branch_id else None
                existing = list(db.scalars(select(DoctorBranch).where(DoctorBranch.workspace_id == wid, DoctorBranch.doctor_id == doctor_id)))
                by_branch = {x.branch_id: x for x in existing}
                for branch_id, row in by_branch.items():
                    row.is_active = branch_id in desired
                    row.is_primary = branch_id == primary if row.is_active else False
                for branch_id in desired - set(by_branch):
                    db.add(DoctorBranch(workspace_id=wid, doctor_id=doctor_id, branch_id=branch_id, is_active=True, is_primary=branch_id == primary))
            elif action.kind == "update_booking_settings":
                settings = db.scalar(select(BookingSettings).where(BookingSettings.workspace_id == wid))
                if settings is None:
                    settings = BookingSettings(workspace_id=wid)
                    db.add(settings)
                _apply_field_changes(settings, action.changes, kind="booking")
        record_activity_event(
            db,
            workspace_id=wid,
            actor_type="staff",
            actor_user_id=actor_user_id,
            action="clinic.knowledge_applied",
            entity_type="clinic_knowledge",
            entity_id=None,
            summary="Clinic knowledge changes applied",
            metadata={
                "action_count": len(actions),
                "action_kinds": [action.kind for action in actions],
                "changed_fields": sorted(
                    {change.field for action in actions for change in action.changes}
                ),
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise KnowledgeEditConflictError("The proposed change conflicts with the current clinic data.") from exc
    except Exception:
        db.rollback()
        raise

    return KnowledgeEditApplyResponse(
        assistant_message="تمام، طبقت التعديلات بعد تأكيدك. بيانات الـAgent اتحدثت.",
        applied_actions=len(actions),
    )
