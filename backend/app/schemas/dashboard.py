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


class DashboardTodayRevenueRead(BaseModel):
    currency: str
    gross_collected_minor: int
    refunds_minor: int
    total_minor: int
    cash_minor: int
    visa_minor: int
    instapay_minor: int
    other_minor: int


class DashboardTodayRead(BaseModel):
    timezone: str
    local_date: str
    appointments: list[DashboardAppointmentRead]
    next_appointment_id: UUID | None


class DashboardSummaryRead(BaseModel):
    timezone: str
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
    today_revenue: DashboardTodayRevenueRead
