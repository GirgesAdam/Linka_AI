from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies.security import (
    WorkspaceAccess,
    get_current_workspace,
    get_manageable_workspace,
    get_workspace_admin,
    get_workspace_admin_user_id,
)
from app.core.doctor_names import normalize_doctor_name_parts
from app.database.session import get_db
from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    integration_authority_policy,
    normalize_authority_policy,
)
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.clinic_integration import ClinicIntegration, ClinicIntegrationEntityLink
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.service import Service
from app.models.staff import Staff
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace
from app.schemas.agent_knowledge import (
    AgentKnowledgeSnapshot,
    KnowledgeEditApplyRequest,
    KnowledgeEditApplyResponse,
    KnowledgeEditProposal,
    KnowledgeEditProposeRequest,
)
from app.schemas.clinic import (
    BookingSettingsRead,
    BookingSettingsUpsert,
    BranchCreate,
    BranchRead,
    BranchUpdate,
    BranchWorkingHourRead,
    DoctorBranchAssignment,
    DoctorBranchRead,
    DoctorCreate,
    DoctorRead,
    DoctorServiceAssignment,
    DoctorServiceRead,
    DoctorUpdate,
    DoctorWorkingHourRead,
    ServiceCreate,
    ServiceRead,
    ServiceUpdate,
    StaffCreate,
    StaffRead,
    StaffUpdate,
    WorkingHoursReplace,
)
from app.schemas.clinic_integration import (
    ClinicDataIssueListRead,
    ClinicDataIssueResolveRequest,
    ClinicEntityLinkRead,
    ClinicEntityLinkUpsert,
    ClinicIntegrationAuthorityRead,
    ClinicIntegrationAuthorityUpsert,
    ClinicIntegrationRead,
    ClinicIntegrationRuntimeRead,
    ClinicIntegrationUpsert,
    ClinicSyncCycleRead,
    ClinicSyncRunRequest,
    ClinicSyncScheduleRead,
    ClinicSyncScheduleUpsert,
)
from app.services.activity import record_activity_event
from app.services.agent_knowledge import build_agent_knowledge_snapshot
from app.services.agent_knowledge_edit import (
    KnowledgeEditConflictError,
    KnowledgeEditError,
    apply_agent_knowledge_edit,
    propose_agent_knowledge_edit,
)
from app.services.clinic_data_quality import (
    ClinicDataQualityError,
    list_data_issues,
    resolve_data_issue,
)
from app.services.clinic_integration_runtime import (
    ClinicIntegrationRuntimeError,
    build_clinic_integration_runtime,
)
from app.services.clinic_integration_sync_runtime import (
    ClinicSyncRuntimeError,
    read_sync_schedule,
    run_manual_sync,
    update_sync_schedule,
)
from app.services.doctor_categories import sync_service_doctor_assignments

router = APIRouter()


def not_found(entity: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity} not found.")


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc


@router.get("/knowledge", response_model=AgentKnowledgeSnapshot)
def get_agent_knowledge(
    workspace: Workspace = Depends(get_current_workspace),
    db: Session = Depends(get_db),
) -> AgentKnowledgeSnapshot:
    return build_agent_knowledge_snapshot(db, workspace)


@router.post("/knowledge/ai/propose", response_model=KnowledgeEditProposal)
def propose_agent_knowledge_change(
    payload: KnowledgeEditProposeRequest,
    workspace: Workspace = Depends(get_manageable_workspace),
    db: Session = Depends(get_db),
) -> KnowledgeEditProposal:
    try:
        return propose_agent_knowledge_edit(db, workspace, payload.message)
    except KnowledgeEditError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception as exc:
        # Provider/internal diagnostics stay in backend logs. Do not expose model details to the UI.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Linka could not prepare that clinic-data edit. Try rephrasing the request.",
        ) from exc


@router.post("/knowledge/ai/apply", response_model=KnowledgeEditApplyResponse)
def apply_agent_knowledge_change(
    payload: KnowledgeEditApplyRequest,
    access: WorkspaceAccess = Depends(get_workspace_admin),
    db: Session = Depends(get_db),
) -> KnowledgeEditApplyResponse:
    try:
        return apply_agent_knowledge_edit(
            db,
            access.workspace,
            base_fingerprint=payload.base_fingerprint,
            actions=payload.actions,
            actor_user_id=access.user.id,
        )
    except KnowledgeEditConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except KnowledgeEditError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/branches", response_model=BranchRead, status_code=status.HTTP_201_CREATED)
def create_branch(
    payload: BranchCreate,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> Branch:
    branch = Branch(workspace_id=workspace.id, **payload.model_dump())
    db.add(branch)
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.branch_created", entity_type="branch", entity_id=branch.id,
        summary="Branch created.", metadata={"changed_fields": sorted(payload.model_dump().keys())}, flush=False,
    )
    commit_or_conflict(db, "Branch code already exists in this workspace.")
    db.refresh(branch)
    return branch


@router.get("/branches", response_model=list[BranchRead])
def list_branches(
    workspace: Workspace = Depends(get_current_workspace),
    db: Session = Depends(get_db),
) -> list[Branch]:
    return list(
        db.scalars(select(Branch).where(Branch.workspace_id == workspace.id).order_by(Branch.name))
    )


@router.patch("/branches/{branch_id}", response_model=BranchRead)
def update_branch(
    branch_id: UUID,
    payload: BranchUpdate,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> Branch:
    branch = db.scalar(
        select(Branch).where(Branch.id == branch_id, Branch.workspace_id == workspace.id)
    )
    if branch is None:
        raise not_found("Branch")
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(branch, key, value)
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.branch_updated", entity_type="branch", entity_id=branch.id,
        summary="Branch updated.", metadata={"changed_fields": sorted(changes.keys())}, flush=False,
    )
    commit_or_conflict(db, "Could not update branch.")
    db.refresh(branch)
    return branch


@router.post("/staff", response_model=StaffRead, status_code=status.HTTP_201_CREATED)
def create_staff(
    payload: StaffCreate,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> Staff:
    staff = Staff(workspace_id=workspace.id, **payload.model_dump())
    db.add(staff)
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.staff_created", entity_type="staff", entity_id=staff.id,
        summary="Staff member created.", metadata={"changed_fields": sorted(payload.model_dump().keys())}, flush=False,
    )
    commit_or_conflict(
        db, "Staff email is already used in this workspace or user reference is invalid."
    )
    db.refresh(staff)
    return staff


@router.get("/staff", response_model=list[StaffRead])
def list_staff(
    workspace: Workspace = Depends(get_current_workspace),
    db: Session = Depends(get_db),
) -> list[Staff]:
    return list(
        db.scalars(
            select(Staff)
            .where(Staff.workspace_id == workspace.id)
            .order_by(Staff.first_name, Staff.last_name)
        )
    )


@router.patch("/staff/{staff_id}", response_model=StaffRead)
def update_staff(
    staff_id: UUID,
    payload: StaffUpdate,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> Staff:
    staff = db.scalar(select(Staff).where(Staff.id == staff_id, Staff.workspace_id == workspace.id))
    if staff is None:
        raise not_found("Staff member")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(staff, key, value)
    if db.scalar(
        select(Doctor.id).where(
            Doctor.workspace_id == workspace.id,
            Doctor.staff_id == staff.id,
        )
    ) is not None:
        staff.first_name, staff.last_name = normalize_doctor_name_parts(
            staff.first_name, staff.last_name
        )
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.staff_updated", entity_type="staff", entity_id=staff.id,
        summary="Staff member updated.", metadata={"changed_fields": sorted(payload.model_dump(exclude_unset=True).keys())}, flush=False,
    )
    commit_or_conflict(db, "Could not update staff member.")
    db.refresh(staff)
    return staff


@router.post("/doctors", response_model=DoctorRead, status_code=status.HTTP_201_CREATED)
def create_doctor(
    payload: DoctorCreate,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> Doctor:
    staff = db.scalar(
        select(Staff).where(Staff.id == payload.staff_id, Staff.workspace_id == workspace.id)
    )
    if staff is None:
        raise not_found("Staff member")
    staff.first_name, staff.last_name = normalize_doctor_name_parts(
        staff.first_name, staff.last_name
    )
    doctor = Doctor(workspace_id=workspace.id, **payload.model_dump())
    db.add(doctor)
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.doctor_created", entity_type="doctor", entity_id=doctor.id,
        summary="Doctor profile created.", metadata={"changed_fields": sorted(payload.model_dump().keys())}, flush=False,
    )
    commit_or_conflict(db, "This staff member already has a doctor profile.")
    db.refresh(doctor)
    return doctor


@router.get("/doctors", response_model=list[DoctorRead])
def list_doctors(
    workspace: Workspace = Depends(get_current_workspace),
    db: Session = Depends(get_db),
) -> list[Doctor]:
    return list(
        db.scalars(
            select(Doctor).where(Doctor.workspace_id == workspace.id).order_by(Doctor.created_at)
        )
    )


@router.patch("/doctors/{doctor_id}", response_model=DoctorRead)
def update_doctor(
    doctor_id: UUID,
    payload: DoctorUpdate,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> Doctor:
    doctor = db.scalar(
        select(Doctor).where(Doctor.id == doctor_id, Doctor.workspace_id == workspace.id)
    )
    if doctor is None:
        raise not_found("Doctor")
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(doctor, key, value)
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.doctor_updated", entity_type="doctor", entity_id=doctor.id,
        summary="Doctor profile updated.", metadata={"changed_fields": sorted(changes.keys())}, flush=False,
    )
    db.commit()
    db.refresh(doctor)
    return doctor


@router.put("/doctors/{doctor_id}/branches/{branch_id}", response_model=DoctorBranchRead)
def assign_doctor_to_branch(
    doctor_id: UUID,
    branch_id: UUID,
    payload: DoctorBranchAssignment,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> DoctorBranch:
    doctor = db.scalar(
        select(Doctor).where(Doctor.id == doctor_id, Doctor.workspace_id == workspace.id)
    )
    branch = db.scalar(
        select(Branch).where(Branch.id == branch_id, Branch.workspace_id == workspace.id)
    )
    if doctor is None:
        raise not_found("Doctor")
    if branch is None:
        raise not_found("Branch")

    assignment = db.scalar(
        select(DoctorBranch).where(
            DoctorBranch.workspace_id == workspace.id,
            DoctorBranch.doctor_id == doctor_id,
            DoctorBranch.branch_id == branch_id,
        )
    )
    if assignment is None:
        assignment = DoctorBranch(
            workspace_id=workspace.id,
            doctor_id=doctor_id,
            branch_id=branch_id,
            **payload.model_dump(),
        )
        db.add(assignment)
    else:
        assignment.is_primary = payload.is_primary
        assignment.is_active = payload.is_active

    if payload.is_primary:
        other_primary = list(
            db.scalars(
                select(DoctorBranch).where(
                    DoctorBranch.workspace_id == workspace.id,
                    DoctorBranch.doctor_id == doctor_id,
                    DoctorBranch.branch_id != branch_id,
                    DoctorBranch.is_primary.is_(True),
                )
            )
        )
        for item in other_primary:
            item.is_primary = False

    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.doctor_branch_assignment_updated", entity_type="doctor", entity_id=doctor_id,
        summary="Doctor branch assignment updated.",
        metadata={"branch_id": branch_id, "is_primary": payload.is_primary, "is_active": payload.is_active}, flush=False,
    )
    db.commit()
    db.refresh(assignment)
    return assignment


@router.post("/services", response_model=ServiceRead, status_code=status.HTTP_201_CREATED)
def create_service(
    payload: ServiceCreate,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> Service:
    service = Service(workspace_id=workspace.id, **payload.model_dump())
    if service.requires_laser_device and service.operational_category != "laser":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Services that require a laser device must use the laser category.",
        )
    db.add(service)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Service slug already exists in this workspace.",
        ) from exc
    sync_service_doctor_assignments(db, workspace_id=workspace.id, service=service)
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.service_created", entity_type="service", entity_id=service.id,
        summary="Service created.", metadata={"changed_fields": sorted(payload.model_dump().keys())}, flush=False,
    )
    commit_or_conflict(db, "Service slug already exists in this workspace.")
    db.refresh(service)
    return service


@router.get("/services", response_model=list[ServiceRead])
def list_services(
    workspace: Workspace = Depends(get_current_workspace),
    db: Session = Depends(get_db),
) -> list[Service]:
    return list(
        db.scalars(
            select(Service).where(Service.workspace_id == workspace.id).order_by(Service.name)
        )
    )


@router.patch("/services/{service_id}", response_model=ServiceRead)
def update_service(
    service_id: UUID,
    payload: ServiceUpdate,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> Service:
    service = db.scalar(
        select(Service).where(Service.id == service_id, Service.workspace_id == workspace.id)
    )
    if service is None:
        raise not_found("Service")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("category") is None:
        changes.pop("category", None)
    final_category = changes.get("operational_category", service.operational_category)
    final_requires_laser = changes.get("requires_laser_device", service.requires_laser_device)
    if final_requires_laser and final_category != "laser":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Services that require a laser device must use the laser category.",
        )
    for key, value in changes.items():
        setattr(service, key, value)
    sync_service_doctor_assignments(db, workspace_id=workspace.id, service=service)
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.service_updated", entity_type="service", entity_id=service.id,
        summary="Service updated.", metadata={"changed_fields": sorted(changes.keys())}, flush=False,
    )
    db.commit()
    db.refresh(service)
    return service


@router.put("/doctors/{doctor_id}/services/{service_id}", response_model=DoctorServiceRead)
def assign_service_to_doctor(
    doctor_id: UUID,
    service_id: UUID,
    payload: DoctorServiceAssignment,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> DoctorService:
    doctor = db.scalar(
        select(Doctor).where(Doctor.id == doctor_id, Doctor.workspace_id == workspace.id)
    )
    service = db.scalar(
        select(Service).where(Service.id == service_id, Service.workspace_id == workspace.id)
    )
    if doctor is None:
        raise not_found("Doctor")
    if service is None:
        raise not_found("Service")

    assignment = db.scalar(
        select(DoctorService).where(
            DoctorService.workspace_id == workspace.id,
            DoctorService.doctor_id == doctor_id,
            DoctorService.service_id == service_id,
        )
    )
    if assignment is None:
        assignment = DoctorService(
            workspace_id=workspace.id,
            doctor_id=doctor_id,
            service_id=service_id,
            **payload.model_dump(),
        )
        db.add(assignment)
    else:
        for key, value in payload.model_dump().items():
            setattr(assignment, key, value)

    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.doctor_service_assignment_updated", entity_type="doctor", entity_id=doctor_id,
        summary="Doctor service assignment updated.",
        metadata={"service_id": service_id, "changed_fields": sorted(payload.model_dump().keys())}, flush=False,
    )
    db.commit()
    db.refresh(assignment)
    return assignment


@router.put("/branches/{branch_id}/working-hours", response_model=list[BranchWorkingHourRead])
def replace_branch_working_hours(
    branch_id: UUID,
    payload: WorkingHoursReplace,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> list[BranchWorkingHour]:
    branch = db.scalar(
        select(Branch).where(Branch.id == branch_id, Branch.workspace_id == workspace.id)
    )
    if branch is None:
        raise not_found("Branch")

    db.execute(
        delete(BranchWorkingHour).where(
            BranchWorkingHour.workspace_id == workspace.id,
            BranchWorkingHour.branch_id == branch_id,
        )
    )
    rows = [
        BranchWorkingHour(workspace_id=workspace.id, branch_id=branch_id, **item.model_dump())
        for item in payload.intervals
    ]
    db.add_all(rows)
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.branch_working_hours_replaced", entity_type="branch", entity_id=branch_id,
        summary="Branch working hours replaced.", metadata={"interval_count": len(rows)}, flush=False,
    )
    commit_or_conflict(db, "Working-hour intervals must be unique and valid.")
    return rows


@router.put(
    "/doctors/{doctor_id}/branches/{branch_id}/working-hours",
    response_model=list[DoctorWorkingHourRead],
)
def replace_doctor_working_hours(
    doctor_id: UUID,
    branch_id: UUID,
    payload: WorkingHoursReplace,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> list[DoctorWorkingHour]:
    assignment = db.scalar(
        select(DoctorBranch).where(
            DoctorBranch.workspace_id == workspace.id,
            DoctorBranch.doctor_id == doctor_id,
            DoctorBranch.branch_id == branch_id,
            DoctorBranch.is_active.is_(True),
        )
    )
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Doctor must be actively assigned to the branch before setting working hours.",
        )

    db.execute(
        delete(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == workspace.id,
            DoctorWorkingHour.doctor_id == doctor_id,
            DoctorWorkingHour.branch_id == branch_id,
        )
    )
    rows = [
        DoctorWorkingHour(
            workspace_id=workspace.id,
            doctor_id=doctor_id,
            branch_id=branch_id,
            **item.model_dump(),
        )
        for item in payload.intervals
    ]
    db.add_all(rows)
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.doctor_working_hours_replaced", entity_type="doctor", entity_id=doctor_id,
        summary="Doctor working hours replaced.", metadata={"branch_id": branch_id, "interval_count": len(rows)}, flush=False,
    )
    commit_or_conflict(db, "Doctor working-hour intervals must be unique and valid.")
    return rows


@router.get("/booking-settings", response_model=BookingSettingsRead)
def get_booking_settings(
    workspace: Workspace = Depends(get_current_workspace),
    db: Session = Depends(get_db),
) -> BookingSettings:
    settings = db.scalar(
        select(BookingSettings).where(BookingSettings.workspace_id == workspace.id)
    )
    if settings is None:
        raise not_found("Booking settings")
    return settings


@router.put("/booking-settings", response_model=BookingSettingsRead)
def upsert_booking_settings(
    payload: BookingSettingsUpsert,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> BookingSettings:
    settings = db.scalar(
        select(BookingSettings).where(BookingSettings.workspace_id == workspace.id)
    )
    if settings is None:
        settings = BookingSettings(workspace_id=workspace.id, **payload.model_dump())
        db.add(settings)
    else:
        for key, value in payload.model_dump().items():
            setattr(settings, key, value)
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.booking_settings_updated", entity_type="booking_settings", entity_id=settings.id,
        summary="Booking settings updated.", metadata={"changed_fields": sorted(payload.model_dump().keys())}, flush=False,
    )
    db.commit()
    db.refresh(settings)
    return settings


@router.get("/integration", response_model=ClinicIntegrationRead)
def get_clinic_integration(
    workspace: Workspace = Depends(get_current_workspace),
    db: Session = Depends(get_db),
) -> ClinicIntegration:
    integration = db.get(ClinicIntegration, workspace.id)
    if integration is None:
        # Alembic backfills all existing workspaces. This guard makes a missing
        # row explicit instead of silently returning configuration that cannot
        # be administered.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Clinic integration configuration is missing. Run database migrations.",
        )
    return integration


@router.put("/integration", response_model=ClinicIntegrationRead)
def upsert_clinic_integration(
    payload: ClinicIntegrationUpsert,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> ClinicIntegration:
    integration = db.get(ClinicIntegration, workspace.id)
    if integration is None:
        integration = ClinicIntegration(workspace_id=workspace.id)
        db.add(integration)

    integration.mode = payload.mode
    integration.adapter_key = payload.adapter_key
    integration.status = payload.status
    integration.external_clinic_id = payload.external_clinic_id
    integration.secret_ref = payload.secret_ref
    integration.config_json = payload.config
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.integration_updated", entity_type="clinic_integration",
        summary="Clinic integration configuration updated.",
        metadata={"changed_fields": ["mode", "adapter_key", "status", "external_clinic_id", "secret_ref", "config"]}, flush=False,
    )
    commit_or_conflict(db, "Could not update clinic integration configuration.")
    db.refresh(integration)
    return integration


@router.get("/integration/authority", response_model=ClinicIntegrationAuthorityRead)
def get_clinic_integration_authority(
    workspace: Workspace = Depends(get_manageable_workspace),
    db: Session = Depends(get_db),
) -> ClinicIntegrationAuthorityRead:
    integration = db.get(ClinicIntegration, workspace.id)
    if integration is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Clinic integration configuration is missing. Run database migrations.",
        )
    try:
        return ClinicIntegrationAuthorityRead.model_validate(integration_authority_policy(integration))
    except ClinicIntegrationAuthorityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.put("/integration/authority", response_model=ClinicIntegrationAuthorityRead)
def update_clinic_integration_authority(
    payload: ClinicIntegrationAuthorityUpsert,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> ClinicIntegrationAuthorityRead:
    integration = db.get(ClinicIntegration, workspace.id)
    if integration is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Clinic integration configuration is missing. Run database migrations.",
        )
    try:
        policy = normalize_authority_policy(payload.model_dump(), mode=integration.mode)
    except ClinicIntegrationAuthorityError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    integration.authority_policy_json = policy
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.integration_authority_updated", entity_type="clinic_integration",
        summary="Clinic integration authority policy updated.",
        metadata={"domains": sorted(policy.keys())}, flush=False,
    )
    commit_or_conflict(db, "Could not update clinic integration authority policy.")
    return ClinicIntegrationAuthorityRead.model_validate(policy)


@router.get("/integration/sync/schedule", response_model=ClinicSyncScheduleRead)
def get_clinic_sync_schedule(
    workspace: Workspace = Depends(get_manageable_workspace),
    db: Session = Depends(get_db),
) -> ClinicSyncScheduleRead:
    return read_sync_schedule(db, workspace_id=workspace.id)


@router.put("/integration/sync/schedule", response_model=ClinicSyncScheduleRead)
def put_clinic_sync_schedule(
    payload: ClinicSyncScheduleUpsert,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> ClinicSyncScheduleRead:
    try:
        result = update_sync_schedule(db, workspace=workspace, payload=payload)
        record_activity_event(
            db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
            action="clinic.integration_sync_schedule_updated", entity_type="clinic_integration",
            summary="Clinic integration sync schedule updated.",
            metadata={"changed_fields": sorted(payload.model_dump().keys())}, flush=False,
        )
        db.commit()
        return result
    except ClinicSyncRuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/integration/sync/run", response_model=ClinicSyncCycleRead)
def run_clinic_sync_now(
    payload: ClinicSyncRunRequest,
    workspace: Workspace = Depends(get_manageable_workspace),
    db: Session = Depends(get_db),
) -> ClinicSyncCycleRead:
    try:
        return run_manual_sync(
            db,
            workspace=workspace,
            domains=payload.domains,
            page_size=payload.page_size,
            max_pages_per_domain=payload.max_pages_per_domain,
        )
    except ClinicSyncRuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/integration/runtime", response_model=ClinicIntegrationRuntimeRead)
def get_clinic_integration_runtime(
    workspace: Workspace = Depends(get_manageable_workspace),
    db: Session = Depends(get_db),
) -> ClinicIntegrationRuntimeRead:
    try:
        return build_clinic_integration_runtime(db, workspace)
    except ClinicIntegrationRuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.get("/integration/data-issues", response_model=ClinicDataIssueListRead)
def get_clinic_data_issues(
    workspace: Workspace = Depends(get_manageable_workspace),
    db: Session = Depends(get_db),
) -> ClinicDataIssueListRead:
    return list_data_issues(db, workspace_id=workspace.id, limit=100)


@router.post("/integration/data-issues/{issue_id}/resolve", response_model=ClinicDataIssueListRead)
def resolve_clinic_data_issue(
    issue_id: UUID,
    payload: ClinicDataIssueResolveRequest,
    workspace: Workspace = Depends(get_manageable_workspace),
    db: Session = Depends(get_db),
) -> ClinicDataIssueListRead:
    try:
        return resolve_data_issue(
            db,
            workspace_id=workspace.id,
            issue_id=issue_id,
            option_index=payload.option_index,
        )
    except ClinicDataQualityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/integration/entity-links", response_model=list[ClinicEntityLinkRead])
def list_clinic_entity_links(
    workspace: Workspace = Depends(get_current_workspace),
    db: Session = Depends(get_db),
) -> list[ClinicIntegrationEntityLink]:
    return list(
        db.scalars(
            select(ClinicIntegrationEntityLink)
            .where(ClinicIntegrationEntityLink.workspace_id == workspace.id)
            .order_by(
                ClinicIntegrationEntityLink.entity_type,
                ClinicIntegrationEntityLink.canonical_id,
            )
        )
    )


@router.put("/integration/entity-links", response_model=ClinicEntityLinkRead)
def upsert_clinic_entity_link(
    payload: ClinicEntityLinkUpsert,
    workspace: Workspace = Depends(get_manageable_workspace),
    actor_user_id: UUID = Depends(get_workspace_admin_user_id),
    db: Session = Depends(get_db),
) -> ClinicIntegrationEntityLink:
    integration = db.get(ClinicIntegration, workspace.id)
    if integration is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Clinic integration configuration is missing. Run database migrations.",
        )

    link = db.scalar(
        select(ClinicIntegrationEntityLink).where(
            ClinicIntegrationEntityLink.workspace_id == workspace.id,
            ClinicIntegrationEntityLink.entity_type == payload.entity_type,
            ClinicIntegrationEntityLink.canonical_id == payload.canonical_id,
        )
    )
    if link is None:
        link = ClinicIntegrationEntityLink(
            workspace_id=workspace.id,
            entity_type=payload.entity_type,
            canonical_id=payload.canonical_id,
            external_id=payload.external_id,
            metadata_json=payload.metadata,
        )
        db.add(link)
    else:
        link.external_id = payload.external_id
        link.metadata_json = payload.metadata

    record_activity_event(
        db, workspace_id=workspace.id, actor_type="staff", actor_user_id=actor_user_id,
        action="clinic.integration_entity_link_updated", entity_type=payload.entity_type, entity_id=payload.canonical_id,
        summary="Clinic integration entity link updated.",
        metadata={"entity_type": payload.entity_type, "canonical_id": payload.canonical_id}, flush=False,
    )
    commit_or_conflict(
        db,
        "This canonical or external entity id is already linked in the workspace.",
    )
    db.refresh(link)
    return link
