from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import app.api.routes.booking as booking_routes
from app.api.dependencies.security import WorkspaceAccess, get_workspace_reader
from app.database.session import SessionLocal, engine
from app.models.appointment import Appointment
from app.models.availability_block import AvailabilityBlock
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.clinic_integration import ClinicIntegration
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.schemas.booking import (
    AppointmentCreate,
    AppointmentReschedule,
    AvailabilityBlockCreate,
    QuickAppointmentCreate,
)


@dataclass(frozen=True)
class BlockFixture:
    workspace_id: UUID
    user_id: UUID
    membership_id: UUID
    branch_a_id: UUID
    branch_b_id: UUID
    other_branch_id: UUID
    other_block_id: UUID
    service_id: UUID
    doctor_id: UUID
    patient_id: UUID
    booking_date: date


def _require_postgres() -> None:
    if engine.dialect.name != "postgresql":
        pytest.fail("Availability-block behavior tests require PostgreSQL.")
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        if os.getenv("CI"):
            raise
        pytest.skip("PostgreSQL test service is not available locally.")


@pytest.fixture
def block_fixture() -> BlockFixture:
    _require_postgres()
    suffix = uuid4().hex
    booking_date = datetime.now(UTC).date() + timedelta(days=2)
    weekday = booking_date.weekday()
    with SessionLocal() as db:
        user = User(email=f"availability-member-{suffix}@example.test", auth_user_id=uuid4())
        workspace = Workspace(name="Availability Clinic", slug=f"availability-{suffix}", timezone="UTC", is_active=True)
        other_workspace = Workspace(name="Other Clinic", slug=f"availability-other-{suffix}", timezone="UTC", is_active=True)
        db.add_all([user, workspace, other_workspace])
        db.flush()
        membership = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="member", is_active=True)
        branch_a = Branch(workspace_id=workspace.id, name="Branch A", code=f"a-{suffix}", timezone="UTC")
        branch_b = Branch(workspace_id=workspace.id, name="Branch B", code=f"b-{suffix}", timezone="UTC")
        other_branch = Branch(workspace_id=other_workspace.id, name="Other Branch", code=f"other-{suffix}", timezone="UTC")
        service = Service(
            workspace_id=workspace.id,
            name="Availability Service",
            slug=f"availability-service-{suffix}",
            category="dermatology",
            duration_minutes=30,
            price_minor=100_000,
            currency="EGP",
        )
        staff = Staff(workspace_id=workspace.id, first_name="Member", last_name="Doctor", email=f"doctor-{suffix}@example.test")
        patient = Patient(
            workspace_id=workspace.id,
            first_name="Availability",
            last_name="Patient",
            phone=f"+2010{suffix[:8]}",
            phone_normalized=f"2010{suffix[:8]}",
            source="other",
            status="active",
        )
        db.add_all([membership, branch_a, branch_b, other_branch, service, staff, patient])
        db.flush()
        doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id, specialization="Dermatology", booking_enabled=True)
        db.add(doctor)
        db.flush()
        db.add_all([
            DoctorBranch(workspace_id=workspace.id, doctor_id=doctor.id, branch_id=branch_a.id, is_primary=True),
            DoctorBranch(workspace_id=workspace.id, doctor_id=doctor.id, branch_id=branch_b.id, is_primary=False),
            DoctorService(workspace_id=workspace.id, doctor_id=doctor.id, service_id=service.id),
            BranchWorkingHour(workspace_id=workspace.id, branch_id=branch_a.id, weekday=weekday, start_time=time(9), end_time=time(18)),
            BranchWorkingHour(workspace_id=workspace.id, branch_id=branch_b.id, weekday=weekday, start_time=time(9), end_time=time(18)),
            DoctorWorkingHour(workspace_id=workspace.id, doctor_id=doctor.id, branch_id=branch_a.id, weekday=weekday, start_time=time(9), end_time=time(18)),
            DoctorWorkingHour(workspace_id=workspace.id, doctor_id=doctor.id, branch_id=branch_b.id, weekday=weekday, start_time=time(9), end_time=time(18)),
            BookingSettings(
                workspace_id=workspace.id,
                slot_interval_minutes=30,
                minimum_notice_minutes=0,
                booking_horizon_days=90,
                cancellation_notice_minutes=60,
                allow_same_day_booking=True,
                require_confirmation=False,
            ),
            ClinicIntegration(workspace_id=workspace.id, mode="tia_native", adapter_key="tia_database", status="active", config_json={}),
        ])
        other_block = AvailabilityBlock(
            workspace_id=other_workspace.id,
            branch_id=other_branch.id,
            start_at=datetime.combine(booking_date, time(12), tzinfo=UTC),
            end_at=datetime.combine(booking_date, time(13), tzinfo=UTC),
            reason="other workspace",
        )
        db.add(other_block)
        db.commit()
        return BlockFixture(
            workspace_id=workspace.id,
            user_id=user.id,
            membership_id=membership.id,
            branch_a_id=branch_a.id,
            branch_b_id=branch_b.id,
            other_branch_id=other_branch.id,
            other_block_id=other_block.id,
            service_id=service.id,
            doctor_id=doctor.id,
            patient_id=patient.id,
            booking_date=booking_date,
        )


def _access(db, fixture: BlockFixture) -> WorkspaceAccess:
    return WorkspaceAccess(
        user=db.get(User, fixture.user_id),
        workspace=db.get(Workspace, fixture.workspace_id),
        membership=db.get(WorkspaceMember, fixture.membership_id),
    )


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


def _starts(db, fixture: BlockFixture, branch_id: UUID) -> set[datetime]:
    result = booking_routes.get_availability(
        branch_id=branch_id,
        service_id=fixture.service_id,
        booking_date=fixture.booking_date,
        access=_access(db, fixture),
        db=db,
        doctor_id=fixture.doctor_id,
        allow_immediate=True,
    )
    return {slot.start_at for slot in result.slots}


def _create_standard(db, fixture: BlockFixture, hour: int) -> Appointment:
    return booking_routes.create_appointment(
        payload=AppointmentCreate(
            patient_id=fixture.patient_id,
            branch_id=fixture.branch_a_id,
            doctor_id=fixture.doctor_id,
            service_id=fixture.service_id,
            start_at=_at(fixture.booking_date, hour),
            source="staff",
        ),
        access=_access(db, fixture),
        db=db,
        idempotency_key=None,
    )


def _create_block(db, fixture: BlockFixture, start_hour: int, end_hour: int):
    return booking_routes.create_availability_block(
        payload=AvailabilityBlockCreate(
            branch_id=fixture.branch_a_id,
            date=fixture.booking_date,
            start_time=time(start_hour),
            end_time=time(end_hour),
            reason="focused behavior test",
        ),
        access=_access(db, fixture),
        db=db,
    )


def test_member_appointment_role_can_create_and_reopen_blocks_but_not_cross_workspace(block_fixture: BlockFixture) -> None:
    with SessionLocal() as db:
        access = _access(db, block_fixture)
        assert access.membership.role == "member"
        assert get_workspace_reader(access) is access

        appointment = _create_standard(db, block_fixture, 10)
        assert appointment.workspace_id == block_fixture.workspace_id

        block = _create_block(db, block_fixture, 12, 13)
        assert block.workspace_id == block_fixture.workspace_id
        booking_routes.delete_availability_block(block_id=block.id, access=access, db=db)
        assert db.get(AvailabilityBlock, block.id) is None

        with pytest.raises(HTTPException) as foreign_create:
            booking_routes.create_availability_block(
                payload=AvailabilityBlockCreate(
                    branch_id=block_fixture.other_branch_id,
                    date=block_fixture.booking_date,
                    start_time=time(14),
                    end_time=time(15),
                ),
                access=access,
                db=db,
            )
        assert foreign_create.value.status_code == 404

        with pytest.raises(HTTPException) as foreign_delete:
            booking_routes.delete_availability_block(block_id=block_fixture.other_block_id, access=access, db=db)
        assert foreign_delete.value.status_code == 404


def test_block_removes_availability_reopen_restores_it_and_other_branch_is_unaffected(block_fixture: BlockFixture) -> None:
    with SessionLocal() as db:
        target = _at(block_fixture.booking_date, 14)
        assert target in _starts(db, block_fixture, block_fixture.branch_a_id)
        assert target in _starts(db, block_fixture, block_fixture.branch_b_id)

        block = _create_block(db, block_fixture, 14, 15)
        assert target not in _starts(db, block_fixture, block_fixture.branch_a_id)
        assert target in _starts(db, block_fixture, block_fixture.branch_b_id)

        booking_routes.delete_availability_block(block_id=block.id, access=_access(db, block_fixture), db=db)
        assert target in _starts(db, block_fixture, block_fixture.branch_a_id)


def test_stale_exact_write_quick_booking_and_reschedule_are_rejected_after_block(block_fixture: BlockFixture) -> None:
    with SessionLocal() as db:
        target = _at(block_fixture.booking_date, 14)
        assert target in _starts(db, block_fixture, block_fixture.branch_a_id)
        current = _create_standard(db, block_fixture, 10)
        _create_block(db, block_fixture, 14, 15)

        with pytest.raises(HTTPException) as standard_error:
            _create_standard(db, block_fixture, 14)
        assert standard_error.value.status_code == 409
        assert "no longer available" in str(standard_error.value.detail)

        with pytest.raises(HTTPException) as quick_error:
            booking_routes.create_quick_appointment(
                payload=QuickAppointmentCreate(
                    patient_id=block_fixture.patient_id,
                    branch_id=block_fixture.branch_a_id,
                    doctor_id=block_fixture.doctor_id,
                    service_id=block_fixture.service_id,
                    start_at=target,
                ),
                access=_access(db, block_fixture),
                db=db,
                idempotency_key=None,
            )
        assert quick_error.value.status_code == 409
        assert "closed for new bookings" in str(quick_error.value.detail)

        with pytest.raises(HTTPException) as reschedule_error:
            booking_routes.reschedule_appointment(
                appointment_id=current.id,
                payload=AppointmentReschedule(start_at=target),
                access=_access(db, block_fixture),
                db=db,
                idempotency_key=None,
            )
        assert reschedule_error.value.status_code == 409
        assert "no longer available" in str(reschedule_error.value.detail)


def test_existing_appointment_inside_new_block_is_unchanged(block_fixture: BlockFixture) -> None:
    with SessionLocal() as db:
        appointment = _create_standard(db, block_fixture, 11)
        original = (appointment.id, appointment.start_at, appointment.end_at, appointment.status)
        block = booking_routes.create_availability_block(
            payload=AvailabilityBlockCreate(
                branch_id=block_fixture.branch_a_id,
                date=block_fixture.booking_date,
                start_time=time(10, 30),
                end_time=time(11, 30),
                reason="clinic meeting",
            ),
            access=_access(db, block_fixture),
            db=db,
        )
        assert block.overlapping_appointments == 1
        db.refresh(appointment)
        assert (appointment.id, appointment.start_at, appointment.end_at, appointment.status) == original
