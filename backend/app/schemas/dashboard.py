from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class DashboardAppointmentRead(BaseModel):
    id: UUID
    patient_id: UUID
    patient_name: str
    service_name: str
    branch_name: str
    doctor_name: str
    status: str
    start_at: datetime
    end_at: datetime
    price_minor: int
    currency: str


class DashboardSummaryRead(BaseModel):
    active_patients: int
    appointments_today: int
    upcoming_appointments: int
    appointments_after_today: int
    open_handoffs: int
    active_channels: int
    failed_automation_jobs: int
    recent_appointments: list[DashboardAppointmentRead]
    today_appointments: list[DashboardAppointmentRead]
    next_appointments: list[DashboardAppointmentRead]
