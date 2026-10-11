from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.clinic_integration import ClinicIntegration
from app.models.clinic_inventory import ClinicLaserDevice, ServiceDevicePrice
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace
from app.services.agent_v2 import multi_service_booking
from app.services.agent_v2.planner import PlanStep, WriteIntent
from app.services.agent_v2.state import BookingProposal
from app.services.agent_v2.write_executor import execute_write_ready_step
from app.services.appointment_commerce import AppointmentCommerceError

CAIRO = ZoneInfo("Africa/Cairo")


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


def _fixture(db: Session):
    suffix = uuid4().hex[:10]
    workspace = Workspace(
        name=f"Multi Single {suffix}",
        slug=f"multi-single-{suffix}",
        timezone="Africa/Cairo",
        is_active=True,
        is_demo=True,
    )
    db.add(workspace)
    db.flush()
    branch = Branch(
        workspace_id=workspace.id,
        name="Main",
        code=f"MS-{suffix}",
        city="Cairo",
        country_code="EG",
        timezone="Africa/Cairo",
        is_active=True,
    )
    staff = Staff(
        workspace_id=workspace.id,
        first_name="Multi",
        last_name="Doctor",
        is_active=True,
    )
    main = Service(
        workspace_id=workspace.id,
        name="Combo",
        slug=f"combo-{suffix}",
        operational_category="dermatology",
        duration_minutes=45,
        buffer_before_minutes=0,
        buffer_after_minutes=0,
        price_minor=110_000,
        currency="EGP",
        is_active=True,
    )
    additional = Service(
        workspace_id=workspace.id,
        name="Full Arms",
        slug=f"arms-{suffix}",
        operational_category="dermatology",
        duration_minutes=30,
        buffer_before_minutes=0,
        buffer_after_minutes=0,
        price_minor=120_000,
        currency="EGP",
        is_active=True,
    )
    incompatible = Service(
        workspace_id=workspace.id,
        name="Other Doctor Service",
        slug=f"other-{suffix}",
        operational_category="dermatology",
        duration_minutes=20,
        price_minor=90_000,
        currency="EGP",
        is_active=True,
    )
    laser_incompatible = Service(
        workspace_id=workspace.id,
        name="Laser Device Service",
        slug=f"laser-{suffix}",
        operational_category="laser",
        duration_minutes=15,
        price_minor=80_000,
        currency="EGP",
        requires_laser_device=True,
        is_active=True,
    )
    patient = Patient(
        workspace_id=workspace.id,
        first_name="Multi",
        last_name="Patient",
        preferred_language="ar",
        source="other",
        status="active",
    )
    db.add_all([branch, staff, main, additional, incompatible, laser_incompatible, patient])
    db.flush()
    workspace.primary_branch_id = branch.id
    doctor = Doctor(
        workspace_id=workspace.id,
        staff_id=staff.id,
        specialization="Dermatology",
        booking_enabled=True,
        is_active=True,
    )
    db.add(doctor)
    db.flush()

    target_date = datetime.now(CAIRO).date() + timedelta(days=7)
    weekday = target_date.weekday()
    start_local = datetime.combine(target_date, time(10), tzinfo=CAIRO)
    db.add_all(
        [
            DoctorBranch(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                branch_id=branch.id,
                is_primary=True,
                is_active=True,
            ),
            DoctorService(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                service_id=main.id,
                is_active=True,
            ),
            DoctorService(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                service_id=additional.id,
                custom_price_minor=125_000,
                is_active=True,
            ),
            DoctorService(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                service_id=laser_incompatible.id,
                is_active=True,
            ),
            BranchWorkingHour(
                workspace_id=workspace.id,
                branch_id=branch.id,
                weekday=weekday,
                start_time=time(9),
                end_time=time(18),
            ),
            DoctorWorkingHour(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                branch_id=branch.id,
                weekday=weekday,
                start_time=time(9),
                end_time=time(18),
            ),
            BookingSettings(
                workspace_id=workspace.id,
                slot_interval_minutes=15,
                minimum_notice_minutes=0,
                booking_horizon_days=90,
                cancellation_notice_minutes=60,
                allow_same_day_booking=True,
                require_confirmation=False,
            ),
            ClinicIntegration(
                workspace_id=workspace.id,
                mode="tia_native",
                adapter_key="tia_database",
                status="active",
                config_json={},
                authority_policy_json={},
            ),
        ]
    )
    db.flush()
    return workspace, patient, branch, doctor, main, additional, incompatible, laser_incompatible, start_local


def _proposal(patient, branch, doctor, main, additional, start_local) -> BookingProposal:
    start_at = start_local.astimezone(UTC)
    return BookingProposal(
        proposal_id=str(uuid4()),
        patient_id=str(patient.id),
        service_ids=[str(main.id), str(additional.id)],
        main_service_id=str(main.id),
        additional_service_ids=[str(additional.id)],
        branch_id=str(branch.id),
        doctor_id=str(doctor.id),
        start_at=start_at,
        end_at=start_at + timedelta(minutes=45),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )


def _step(proposal: BookingProposal) -> PlanStep:
    return PlanStep(
        operation_index=0,
        operation_type="confirm_booking_proposal",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="booking_proposal",
            authorized=True,
            parameters={
                "proposal_id": proposal.proposal_id,
                "proposal": proposal.model_dump(mode="json"),
            },
            requires_verification=False,
        ),
        response_goal="booking_completed",
    )


def _counts(db: Session, workspace_id):
    appointments = db.scalar(
        select(func.count()).select_from(Appointment).where(Appointment.workspace_id == workspace_id)
    )
    additionals = db.scalar(
        select(func.count())
        .select_from(AppointmentAdditionalService)
        .where(AppointmentAdditionalService.workspace_id == workspace_id)
    )
    return int(appointments or 0), int(additionals or 0)


def test_multi_service_proposal_writes_one_appointment_and_one_additional() -> None:
    with _db_session() as db:
        workspace, patient, branch, doctor, main, additional, _, _, start_local = _fixture(db)
        proposal = _proposal(patient, branch, doctor, main, additional, start_local)
        result = execute_write_ready_step(
            db,
            workspace=workspace,
            patient=patient,
            step=_step(proposal),
            idempotency_key=f"proposal:{proposal.proposal_id}",
            commit=False,
        )
        assert result["ok"] is True
        assert _counts(db, workspace.id) == (1, 1)
        appointment = db.scalar(select(Appointment).where(Appointment.workspace_id == workspace.id))
        assert appointment is not None
        assert appointment.service_id == main.id
        assert appointment.duration_minutes == 45
        assert appointment.end_at - appointment.start_at == timedelta(minutes=45)
        assert appointment.branch_id == branch.id
        assert appointment.doctor_id == doctor.id
        assert appointment.source == "ai"
        assert appointment.visit_group_id is None
        line = db.scalar(
            select(AppointmentAdditionalService).where(
                AppointmentAdditionalService.workspace_id == workspace.id
            )
        )
        assert line is not None
        assert line.appointment_id == appointment.id
        assert line.service_id == additional.id
        assert line.service_name == additional.name
        assert line.unit_price_minor == 125_000
        assert line.billing_context == "standard"


def test_incompatible_additional_doctor_fails_before_any_write() -> None:
    with _db_session() as db:
        workspace, patient, branch, doctor, main, _, incompatible, _, start_local = _fixture(db)
        proposal = _proposal(patient, branch, doctor, main, incompatible, start_local)
        result = execute_write_ready_step(
            db,
            workspace=workspace,
            patient=patient,
            step=_step(proposal),
            commit=False,
        )
        assert result["ok"] is False
        assert _counts(db, workspace.id) == (0, 0)


def test_incompatible_additional_device_fails_before_any_write() -> None:
    with _db_session() as db:
        workspace, patient, branch, doctor, main, _, _, laser, start_local = _fixture(db)
        proposal = _proposal(patient, branch, doctor, main, laser, start_local)
        result = execute_write_ready_step(
            db,
            workspace=workspace,
            patient=patient,
            step=_step(proposal),
            commit=False,
        )
        assert result["ok"] is False
        assert _counts(db, workspace.id) == (0, 0)


def test_additional_failure_rolls_back_main_appointment_savepoint(monkeypatch) -> None:
    with _db_session() as db:
        workspace, patient, branch, doctor, main, additional, _, _, start_local = _fixture(db)
        proposal = _proposal(patient, branch, doctor, main, additional, start_local)

        def fail_additional(*_args, **_kwargs):
            raise AppointmentCommerceError("synthetic additional failure")

        monkeypatch.setattr(multi_service_booking, "add_additional_service", fail_additional)
        result = execute_write_ready_step(
            db,
            workspace=workspace,
            patient=patient,
            step=_step(proposal),
            commit=False,
        )
        assert result["ok"] is False
        assert _counts(db, workspace.id) == (0, 0)



def _add_device_price(
    db: Session,
    *,
    workspace: Workspace,
    service: Service,
    device_key: str,
    duration_minutes: int,
) -> None:
    existing = db.scalar(
        select(ClinicLaserDevice).where(
            ClinicLaserDevice.workspace_id == workspace.id,
            ClinicLaserDevice.device_key == device_key,
        )
    )
    if existing is None:
        db.add(
            ClinicLaserDevice(
                workspace_id=workspace.id,
                device_key=device_key,
                name=device_key.title(),
                is_active=True,
            )
        )
        db.flush()
    service.requires_laser_device = True
    service.operational_category = "laser"
    db.add(
        ServiceDevicePrice(
            workspace_id=workspace.id,
            service_id=service.id,
            device_key=device_key,
            device_name=device_key.title(),
            price_minor=service.price_minor,
            duration_minutes=duration_minutes,
            currency="EGP",
            is_active=True,
        )
    )
    db.flush()


def test_same_visit_laser_services_persist_one_shared_device() -> None:
    with _db_session() as db:
        workspace, patient, branch, doctor, main, additional, _, _, start_local = _fixture(db)
        _add_device_price(
            db,
            workspace=workspace,
            service=main,
            device_key="candela",
            duration_minutes=45,
        )
        _add_device_price(
            db,
            workspace=workspace,
            service=additional,
            device_key="candela",
            duration_minutes=30,
        )
        proposal = _proposal(patient, branch, doctor, main, additional, start_local).model_copy(
            update={
                "device_key": "candela",
                "additional_service_device_keys": {str(additional.id): "candela"},
            }
        )

        result = execute_write_ready_step(
            db,
            workspace=workspace,
            patient=patient,
            step=_step(proposal),
            idempotency_key=f"proposal:{proposal.proposal_id}",
            commit=False,
        )

        assert result["ok"] is True
        assert _counts(db, workspace.id) == (1, 1)
        appointment = db.scalar(select(Appointment).where(Appointment.workspace_id == workspace.id))
        line = db.scalar(
            select(AppointmentAdditionalService).where(
                AppointmentAdditionalService.workspace_id == workspace.id
            )
        )
        assert appointment is not None and line is not None
        assert appointment.laser_device_key == "candela"
        assert line.laser_device_key == "candela"
        assert {appointment.laser_device_key, line.laser_device_key} == {"candela"}


def test_additional_laser_cannot_auto_switch_to_its_sole_other_device() -> None:
    with _db_session() as db:
        workspace, patient, branch, doctor, main, additional, _, _, start_local = _fixture(db)
        _add_device_price(
            db,
            workspace=workspace,
            service=main,
            device_key="candela",
            duration_minutes=45,
        )
        _add_device_price(
            db,
            workspace=workspace,
            service=additional,
            device_key="deka",
            duration_minutes=30,
        )
        proposal = _proposal(patient, branch, doctor, main, additional, start_local).model_copy(
            update={"device_key": "candela", "additional_service_device_keys": {}}
        )

        result = execute_write_ready_step(
            db,
            workspace=workspace,
            patient=patient,
            step=_step(proposal),
            commit=False,
        )

        assert result["ok"] is False
        assert _counts(db, workspace.id) == (0, 0)


def test_non_laser_additional_keeps_device_null_on_shared_laser_visit() -> None:
    with _db_session() as db:
        workspace, patient, branch, doctor, main, additional, _, _, start_local = _fixture(db)
        _add_device_price(
            db,
            workspace=workspace,
            service=main,
            device_key="candela",
            duration_minutes=45,
        )
        proposal = _proposal(patient, branch, doctor, main, additional, start_local).model_copy(
            update={"device_key": "candela", "additional_service_device_keys": {}}
        )

        result = execute_write_ready_step(
            db,
            workspace=workspace,
            patient=patient,
            step=_step(proposal),
            commit=False,
        )

        assert result["ok"] is True
        appointment = db.scalar(select(Appointment).where(Appointment.workspace_id == workspace.id))
        line = db.scalar(
            select(AppointmentAdditionalService).where(
                AppointmentAdditionalService.workspace_id == workspace.id
            )
        )
        assert appointment is not None and line is not None
        assert appointment.laser_device_key == "candela"
        assert line.laser_device_key is None
