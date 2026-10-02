from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess, get_workspace_reader
from app.database.session import get_db
from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    require_tia_workspace_domain_write,
)
from app.schemas.booking import AppointmentRead
from app.services.staff_appointment_edits import (
    StaffAppointmentEditError,
    StaffAppointmentEditNotFound,
    change_appointment_service,
)

router = APIRouter()


class AppointmentServiceUpdate(BaseModel):
    service_id: UUID
    doctor_id: UUID | None = None
    laser_device_key: str | None = None
    start_at: datetime | None = None


def _require_local_appointment_write(db: Session, workspace_id: UUID) -> None:
    try:
        require_tia_workspace_domain_write(
            db,
            workspace_id=workspace_id,
            domain="appointments",
        )
    except ClinicIntegrationAuthorityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/appointments/{appointment_id}/service", response_model=AppointmentRead)
def update_appointment_service(
    appointment_id: UUID,
    payload: AppointmentServiceUpdate,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_reader)],
    db: Annotated[Session, Depends(get_db)],
):
    """Reception/staff-only dashboard write; this operation is not an Agent tool."""
    _require_local_appointment_write(db, access.workspace.id)
    try:
        appointment = change_appointment_service(
            db,
            workspace=access.workspace,
            appointment_id=appointment_id,
            service_id=payload.service_id,
            doctor_id=payload.doctor_id,
            laser_device_key=payload.laser_device_key,
            start_at=payload.start_at,
            changed_by_user_id=access.user.id,
        )
        db.commit()
        db.refresh(appointment)
        return appointment
    except StaffAppointmentEditNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found.") from exc
    except StaffAppointmentEditError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
