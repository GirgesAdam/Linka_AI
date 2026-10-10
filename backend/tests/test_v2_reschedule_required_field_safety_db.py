from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.workspace import Workspace
from app.services.agent_v2 import write_executor
from app.services.agent_v2.planner import PlanStep, WriteIntent


@contextmanager
def _db_session():
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        yield db
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
        engine.dispose()


def test_unresolved_reschedule_write_has_zero_appointment_delta(monkeypatch) -> None:
    with _db_session() as db:
        suffix = uuid4().hex[:10]
        workspace = Workspace(
            name=f"P1 Reschedule {suffix}",
            slug=f"p1-reschedule-{suffix}",
            timezone="Africa/Cairo",
            is_active=True,
            is_demo=True,
        )
        db.add(workspace)
        db.flush()
        branch = Branch(
            workspace_id=workspace.id,
            name="Main",
            code=f"P1-{suffix}",
            city="Cairo",
            country_code="EG",
            timezone="Africa/Cairo",
            is_active=True,
        )
        staff = Staff(
            workspace_id=workspace.id,
            first_name="P1",
            last_name="Doctor",
            is_active=True,
        )
        service = Service(
            workspace_id=workspace.id,
            name=f"P1 Service {suffix}",
            slug=f"p1-service-{suffix}",
            operational_category="dermatology",
            duration_minutes=45,
            price_minor=200_000,
            currency="EGP",
            is_active=True,
        )
        patient = Patient(
            workspace_id=workspace.id,
            first_name="P1",
            preferred_language="ar",
            source="other",
            status="active",
        )
        db.add_all([branch, staff, service, patient])
        db.flush()
        workspace.primary_branch_id = branch.id
        doctor = Doctor(
            workspace_id=workspace.id,
            staff_id=staff.id,
            doctor_type="regular",
            booking_enabled=True,
            is_active=True,
        )
        db.add(doctor)
        db.flush()
        start = datetime.now(UTC) + timedelta(days=1)
        appointment = Appointment(
            workspace_id=workspace.id,
            patient_id=patient.id,
            branch_id=branch.id,
            doctor_id=doctor.id,
            doctor_assignment_known=True,
            service_id=service.id,
            status="confirmed",
            source="ai",
            start_at=start,
            end_at=start + timedelta(minutes=45),
            busy_start_at=start,
            busy_end_at=start + timedelta(minutes=45),
            duration_minutes=45,
            price_minor=200_000,
            discount_minor=0,
            currency="EGP",
            payment_status="unpaid",
            payment_method="unknown",
            billing_context="standard",
        )
        db.add(appointment)
        db.flush()

        before_count = db.scalar(
            select(func.count()).select_from(Appointment).where(Appointment.workspace_id == workspace.id)
        )
        before_status = appointment.status
        monkeypatch.setattr(
            write_executor, "reschedule_appointment_operation", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("write must not execute"))
        )
        monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)
        step = PlanStep(
            operation_index=0,
            operation_type="reschedule",
            disposition="write_ready",
            write_intent=WriteIntent(
                kind="reschedule",
                authorized=True,
                parameters={
                    "appointment_id": str(appointment.id),
                    "branch_id": str(branch.id),
                    "doctor_id": str(doctor.id),
                    "service_id": str(service.id),
                    "start_at": (start + timedelta(hours=1)).isoformat(),
                },
            ),
        )

        result = write_executor.execute_write_ready_step(
            db,
            workspace=workspace,
            patient=patient,
            step=step,
            commit=False,
        )
        db.flush()
        db.refresh(appointment)
        after_count = db.scalar(
            select(func.count()).select_from(Appointment).where(Appointment.workspace_id == workspace.id)
        )

        assert result["ok"] is False
        assert result["error_code"] == "reschedule_target_unresolved"
        assert appointment.status == before_status == "confirmed"
        assert after_count == before_count == 1
