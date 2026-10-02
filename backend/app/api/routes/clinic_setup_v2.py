from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess, get_workspace_admin, get_workspace_reader
from app.database.session import SessionLocal, get_db
from app.models.service import Service
from app.models.workspace import Workspace
from app.schemas.clinic_setup_v2 import (
    BookingPolicyUpdateV2,
    ClinicDoctorCreateV2,
    ClinicDoctorReadV2,
    ClinicDoctorUpdateV2,
    ClinicProfileUpsert,
    ClinicServiceCreateV2,
    ClinicServiceReadV2,
    ClinicServiceUpdateV2,
    ClinicSetupApplyDraftRequest,
    ClinicSetupImportDocument,
    ClinicSetupImportResponse,
    ClinicSetupPreviewResponse,
    ClinicSetupV2Snapshot,
    VisitingWindowsUpdateV2,
    WorkingHoursUpdateV2,
)
from app.schemas.historical_import import (
    HistoricalImportApplyResponse,
    HistoricalImportListResponse,
    HistoricalImportPreviewRequest,
    HistoricalImportPreviewResponse,
)
from app.services.activity import record_activity_event
from app.services.clinic_setup_import import (
    ClinicSetupImportError,
    build_clinic_setup_template,
    clinic_setup_draft_to_workbook_base64,
    import_clinic_setup_workbook,
    preview_clinic_setup_workbook,
)
from app.services.clinic_setup_v2 import (
    ClinicSetupV2Error,
    build_setup_v2_snapshot,
    create_doctor_v2,
    create_service_v2,
    replace_clinic_hours_v2,
    replace_regular_doctor_hours_v2,
    replace_visiting_windows_v2,
    update_booking_policy_v2,
    update_doctor_v2,
    update_service_v2,
    upsert_clinic_profile,
)
from app.services.historical_import import (
    HistoricalImportConflictError,
    HistoricalImportError,
    _batch_read,
    apply_historical_import,
    build_historical_import_template,
    get_historical_import_batch,
    list_historical_import_batches,
    mark_historical_import_failed,
    preview_historical_import,
    start_historical_import,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _setup_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HistoricalImportConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (ClinicSetupV2Error, HistoricalImportError)):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Clinic setup operation failed.")


def _commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Clinic setup conflicts with existing data.") from exc


def _activity(
    db: Session,
    *,
    access: WorkspaceAccess,
    action: str,
    entity_type: str,
    summary: str,
    entity_id: UUID | None = None,
    metadata: dict | None = None,
) -> None:
    record_activity_event(
        db,
        workspace_id=access.workspace.id,
        actor_type="staff",
        actor_user_id=access.user.id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        metadata=metadata or {},
        flush=False,
    )


@router.get("/setup-v2", response_model=ClinicSetupV2Snapshot)
def read_setup_v2(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicSetupV2Snapshot:
    return build_setup_v2_snapshot(db, workspace=access.workspace)


@router.get("/setup-v2/template")
def download_setup_v2_template(
    _access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
) -> Response:
    return Response(
        content=build_clinic_setup_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="Linka_Clinic_Setup_Template_v1.xlsx"'},
    )


@router.post("/setup-v2/preview", response_model=ClinicSetupPreviewResponse)
def preview_setup_v2(
    payload: ClinicSetupImportDocument,
    _access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
) -> ClinicSetupPreviewResponse:
    try:
        return preview_clinic_setup_workbook(filename=payload.name, content_base64=payload.content_base64)
    except ClinicSetupImportError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/setup-v2/apply-draft", response_model=ClinicSetupImportResponse)
def apply_setup_draft_v2(
    payload: ClinicSetupApplyDraftRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicSetupImportResponse:
    try:
        content_base64 = clinic_setup_draft_to_workbook_base64(payload.draft)
        result = import_clinic_setup_workbook(
            db,
            workspace=access.workspace,
            filename="Linka_Clinic_Setup_Edited_Draft.xlsx",
            content_base64=content_base64,
        )
        _activity(
            db,
            access=access,
            action="clinic.setup_draft_applied",
            entity_type="workspace",
            entity_id=access.workspace.id,
            summary="Clinic setup draft reviewed and applied.",
            metadata={
                "imported_counts": result.imported_counts,
                "skipped_counts": result.skipped_counts,
                "issue_count": len(result.issues),
            },
        )
        _commit(db)
        db.refresh(access.workspace)
        result.snapshot = build_setup_v2_snapshot(db, workspace=access.workspace)
        return result
    except ClinicSetupImportError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except ClinicSetupV2Error as exc:
        db.rollback()
        raise _setup_error(exc) from exc


@router.post("/setup-v2/import", response_model=ClinicSetupImportResponse)
def import_setup_v2(
    payload: ClinicSetupImportDocument,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicSetupImportResponse:
    try:
        result = import_clinic_setup_workbook(
            db,
            workspace=access.workspace,
            filename=payload.name,
            content_base64=payload.content_base64,
        )
        _activity(
            db,
            access=access,
            action="clinic.setup_imported",
            entity_type="workspace",
            entity_id=access.workspace.id,
            summary="Clinic setup imported from Excel.",
            metadata={
                "filename": payload.name,
                "imported_counts": result.imported_counts,
                "skipped_counts": result.skipped_counts,
                "issue_count": len(result.issues),
            },
        )
        _commit(db)
        db.refresh(access.workspace)
        result.snapshot = build_setup_v2_snapshot(db, workspace=access.workspace)
        return result
    except ClinicSetupImportError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except ClinicSetupV2Error as exc:
        db.rollback()
        raise _setup_error(exc) from exc


@router.put("/setup-v2/profile", response_model=ClinicSetupV2Snapshot)
def save_profile_v2(
    payload: ClinicProfileUpsert,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicSetupV2Snapshot:
    try:
        branch = upsert_clinic_profile(db, workspace=access.workspace, payload=payload)
        _activity(db, access=access, action="clinic.profile_updated", entity_type="branch", entity_id=branch.id, summary="Clinic profile updated.", metadata={"changed_fields": ["name", "phone", "address", "city"]})
        _commit(db)
        db.refresh(access.workspace)
        return build_setup_v2_snapshot(db, workspace=access.workspace)
    except ClinicSetupV2Error as exc:
        db.rollback()
        raise _setup_error(exc) from exc


@router.post("/setup-v2/services", response_model=ClinicServiceReadV2, status_code=status.HTTP_201_CREATED)
def add_service_v2(
    payload: ClinicServiceCreateV2,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicServiceReadV2:
    try:
        service = create_service_v2(db, workspace=access.workspace, payload=payload)
        _activity(db, access=access, action="clinic.service_created", entity_type="service", entity_id=service.id, summary="Clinic service created.", metadata={"changed_fields": ["name", "category", "duration_minutes", "price"]})
        _commit(db)
        db.refresh(service)
        snapshot = build_setup_v2_snapshot(db, workspace=access.workspace)
        return next(item for item in snapshot.services if item.id == service.id)
    except ClinicSetupV2Error as exc:
        db.rollback()
        raise _setup_error(exc) from exc


@router.put("/setup-v2/services/{service_id}", response_model=ClinicServiceReadV2)
def edit_service_v2(
    service_id: UUID,
    payload: ClinicServiceUpdateV2,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicServiceReadV2:
    try:
        service = update_service_v2(db, workspace=access.workspace, service_id=service_id, payload=payload)
        _activity(db, access=access, action="clinic.service_updated", entity_type="service", entity_id=service.id, summary="Clinic service updated.", metadata={"changed_fields": ["name", "category", "duration_minutes", "price"]})
        _commit(db)
        db.refresh(service)
        snapshot = build_setup_v2_snapshot(db, workspace=access.workspace)
        return next(item for item in snapshot.services if item.id == service.id)
    except ClinicSetupV2Error as exc:
        db.rollback()
        raise _setup_error(exc) from exc


@router.post("/setup-v2/doctors", response_model=ClinicDoctorReadV2, status_code=status.HTTP_201_CREATED)
def add_doctor_v2(
    payload: ClinicDoctorCreateV2,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicDoctorReadV2:
    try:
        doctor = create_doctor_v2(db, workspace=access.workspace, payload=payload)
        _activity(db, access=access, action="clinic.doctor_created", entity_type="doctor", entity_id=doctor.id, summary="Clinic doctor configured.", metadata={"doctor_type": payload.doctor_type, "service_count": len(payload.service_ids)})
        _commit(db)
        snapshot = build_setup_v2_snapshot(db, workspace=access.workspace)
        return next(item for item in snapshot.doctors if item.id == doctor.id)
    except ClinicSetupV2Error as exc:
        db.rollback()
        raise _setup_error(exc) from exc


@router.put("/setup-v2/doctors/{doctor_id}", response_model=ClinicDoctorReadV2)
def edit_doctor_v2(
    doctor_id: UUID,
    payload: ClinicDoctorUpdateV2,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicDoctorReadV2:
    try:
        doctor = update_doctor_v2(db, workspace=access.workspace, doctor_id=doctor_id, payload=payload)
        _activity(db, access=access, action="clinic.doctor_updated", entity_type="doctor", entity_id=doctor.id, summary="Clinic doctor updated.", metadata={"doctor_type": payload.doctor_type, "service_count": len(payload.service_ids)})
        _commit(db)
        snapshot = build_setup_v2_snapshot(db, workspace=access.workspace)
        return next(item for item in snapshot.doctors if item.id == doctor.id)
    except ClinicSetupV2Error as exc:
        db.rollback()
        raise _setup_error(exc) from exc


@router.put("/setup-v2/hours", response_model=ClinicSetupV2Snapshot)
def save_clinic_hours_v2(
    payload: WorkingHoursUpdateV2,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicSetupV2Snapshot:
    try:
        replace_clinic_hours_v2(db, workspace=access.workspace, payload=payload)
        _activity(db, access=access, action="clinic.hours_updated", entity_type="branch", entity_id=access.workspace.primary_branch_id, summary="Clinic working hours updated.", metadata={"interval_count": len(payload.intervals)})
        _commit(db)
        return build_setup_v2_snapshot(db, workspace=access.workspace)
    except ClinicSetupV2Error as exc:
        db.rollback()
        raise _setup_error(exc) from exc


@router.put("/setup-v2/doctors/{doctor_id}/weekly-hours", response_model=ClinicSetupV2Snapshot)
def save_regular_doctor_hours_v2(
    doctor_id: UUID,
    payload: WorkingHoursUpdateV2,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicSetupV2Snapshot:
    try:
        replace_regular_doctor_hours_v2(db, workspace=access.workspace, doctor_id=doctor_id, payload=payload)
        _activity(db, access=access, action="clinic.doctor_hours_updated", entity_type="doctor", entity_id=doctor_id, summary="Regular doctor weekly hours updated.", metadata={"interval_count": len(payload.intervals)})
        _commit(db)
        return build_setup_v2_snapshot(db, workspace=access.workspace)
    except ClinicSetupV2Error as exc:
        db.rollback()
        raise _setup_error(exc) from exc


@router.put("/setup-v2/doctors/{doctor_id}/visiting-windows", response_model=ClinicSetupV2Snapshot)
def save_visiting_doctor_windows_v2(
    doctor_id: UUID,
    payload: VisitingWindowsUpdateV2,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicSetupV2Snapshot:
    try:
        replace_visiting_windows_v2(db, workspace=access.workspace, doctor_id=doctor_id, payload=payload)
        _activity(db, access=access, action="clinic.doctor_visiting_windows_updated", entity_type="doctor", entity_id=doctor_id, summary="Visiting doctor dated availability updated.", metadata={"window_count": len(payload.windows)})
        _commit(db)
        return build_setup_v2_snapshot(db, workspace=access.workspace)
    except ClinicSetupV2Error as exc:
        db.rollback()
        raise _setup_error(exc) from exc


@router.put("/setup-v2/booking-policy", response_model=ClinicSetupV2Snapshot)
def save_booking_policy_v2(
    payload: BookingPolicyUpdateV2,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ClinicSetupV2Snapshot:
    update_booking_policy_v2(db, workspace=access.workspace, payload=payload)
    _activity(db, access=access, action="clinic.booking_policy_updated", entity_type="workspace", entity_id=access.workspace.id, summary="Clinic booking policy updated.", metadata={"changed_fields": sorted(payload.model_dump().keys())})
    _commit(db)
    return build_setup_v2_snapshot(db, workspace=access.workspace)


@router.get("/history/template")
def download_history_template(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    service_names = list(
        db.scalars(
            select(Service.name)
            .where(
                Service.workspace_id == access.workspace.id,
                Service.is_active.is_(True),
            )
            .order_by(Service.name)
        ).all()
    )
    content = build_historical_import_template(service_names=service_names)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="Linka_Active_Packages_Import_v2.xlsx"'
        },
    )


@router.post("/history/preview", response_model=HistoricalImportPreviewResponse)
def preview_history(
    payload: HistoricalImportPreviewRequest,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> HistoricalImportPreviewResponse:
    try:
        return preview_historical_import(
            db,
            workspace=access.workspace,
            user_id=access.user.id,
            documents=payload.documents,
            mode=payload.mode,
        )
    except HistoricalImportError as exc:
        db.rollback()
        raise _setup_error(exc) from exc


@router.get("/history/batches", response_model=HistoricalImportListResponse)
def read_history_batches(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> HistoricalImportListResponse:
    rows = list_historical_import_batches(db, workspace_id=access.workspace.id)
    return HistoricalImportListResponse(batches=[_batch_read(row) for row in rows])


@router.get("/history/batches/{batch_id}", response_model=HistoricalImportApplyResponse)
def read_history_batch(
    batch_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
) -> HistoricalImportApplyResponse:
    batch = get_historical_import_batch(db, workspace_id=access.workspace.id, batch_id=batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Historical import batch not found.")
    return HistoricalImportApplyResponse(batch=_batch_read(batch), import_started=batch.status == "importing")


def _run_history_import_job(*, workspace_id: UUID, batch_id: UUID) -> None:
    db = SessionLocal()
    try:
        workspace = db.get(Workspace, workspace_id)
        batch = get_historical_import_batch(db, workspace_id=workspace_id, batch_id=batch_id)
        if workspace is None or batch is None:
            return
        apply_historical_import(db, workspace=workspace, batch=batch)
    except Exception as exc:  # background boundary: persist concise failure for UI polling
        db.rollback()
        logger.exception("Historical clinic import failed", extra={"workspace_id": str(workspace_id), "batch_id": str(batch_id)})
        try:
            mark_historical_import_failed(db, workspace_id=workspace_id, batch_id=batch_id, message=str(exc))
        except Exception:
            db.rollback()
            logger.exception("Could not persist historical import failure state")
    finally:
        db.close()


@router.post(
    "/history/batches/{batch_id}/apply",
    response_model=HistoricalImportApplyResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def apply_history(
    batch_id: UUID,
    background_tasks: BackgroundTasks,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> HistoricalImportApplyResponse:
    batch = get_historical_import_batch(db, workspace_id=access.workspace.id, batch_id=batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Historical import batch not found.")
    try:
        batch = start_historical_import(db, batch=batch)
    except HistoricalImportError as exc:
        db.rollback()
        raise _setup_error(exc) from exc
    if batch.status == "importing":
        background_tasks.add_task(_run_history_import_job, workspace_id=access.workspace.id, batch_id=batch.id)
    return HistoricalImportApplyResponse(batch=_batch_read(batch), import_started=batch.status == "importing")
