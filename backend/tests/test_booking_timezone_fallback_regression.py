from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.models.appointment import Appointment
from app.models.automation_job import AutomationJob
from app.models.availability_block import AvailabilityBlock
from app.models.branch import Branch
from app.models.service import Service
from app.models.working_hours import BranchWorkingHour, DoctorAvailabilityWindow, DoctorWorkingHour
from app.services import appointment_creation, appointment_operations, booking
from app.services.booking import EffectiveBookingSettings


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _TimezoneBookingDb:
    def __init__(self, *, branch, service, assignment, branch_hours, doctor_hours, current=None):
        self.branch = branch
        self.service = service
        self.assignment = assignment
        self.current = current
        self.added = []
        self.scalar_batches = {
            BranchWorkingHour: branch_hours,
            Appointment: [],
            AvailabilityBlock: [],
            DoctorWorkingHour: doctor_hours,
            DoctorAvailabilityWindow: [],
            AutomationJob: [],
        }

    def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is Branch:
            return self.branch
        if entity is Service:
            return self.service
        if entity is Appointment:
            return self.current
        return None

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        return list(self.scalar_batches.get(entity, []))

    def execute(self, _statement):
        return _Rows([self.assignment])

    def add(self, value):
        self.added.append(value)

    def flush(self):
        for value in self.added:
            if isinstance(value, Appointment) and value.id is None:
                value.id = uuid4()


def _booking_context(*, current=None):
    workspace_id = uuid4()
    branch_id = uuid4()
    service_id = uuid4()
    doctor_id = uuid4()
    workspace = SimpleNamespace(id=workspace_id, timezone="Asia/Tokyo")
    branch = SimpleNamespace(
        id=branch_id,
        workspace_id=workspace_id,
        is_active=True,
        timezone="Invalid/Branch-Zone",
    )
    service = SimpleNamespace(
        id=service_id,
        workspace_id=workspace_id,
        is_active=True,
        requires_laser_device=False,
        operational_category=None,
        duration_minutes=30,
        price_minor=150_000,
        currency="EGP",
    )
    doctor = SimpleNamespace(
        id=doctor_id,
        doctor_type="regular",
        is_active=True,
        booking_enabled=True,
    )
    assignment = (
        SimpleNamespace(doctor_id=doctor_id, branch_id=branch_id),
        SimpleNamespace(custom_price_minor=None),
        doctor,
    )
    branch_hours = [
        SimpleNamespace(start_time=time(10, 0), end_time=time(12, 0))
    ]
    doctor_hours = [
        SimpleNamespace(
            doctor_id=doctor_id,
            branch_id=branch_id,
            start_time=time(10, 0),
            end_time=time(12, 0),
        )
    ]
    db = _TimezoneBookingDb(
        branch=branch,
        service=service,
        assignment=assignment,
        branch_hours=branch_hours,
        doctor_hours=doctor_hours,
        current=current,
    )
    return workspace, branch, service, doctor_id, db


def _future_tokyo_slot():
    now = datetime.now(UTC)
    tokyo = ZoneInfo("Asia/Tokyo")
    local_date = now.astimezone(tokyo).date() + timedelta(days=10)
    requested_start_at = datetime.combine(local_date, time(10, 0), tzinfo=tokyo)
    return now, local_date, requested_start_at.astimezone(UTC)


def _use_deterministic_booking_settings(monkeypatch):
    settings = EffectiveBookingSettings(
        slot_interval_minutes=15,
        minimum_notice_minutes=0,
        booking_horizon_days=90,
        allow_same_day_booking=True,
    )
    monkeypatch.setattr(booking, "get_effective_booking_settings", lambda *_args: settings)


def test_availability_uses_workspace_timezone_when_branch_timezone_is_invalid(monkeypatch):
    _use_deterministic_booking_settings(monkeypatch)
    workspace, branch, service, doctor_id, db = _booking_context()
    now, local_date, expected_start_at = _future_tokyo_slot()

    timezone_name, slots = booking.calculate_availability(
        db=db,
        workspace=workspace,
        branch_id=branch.id,
        service_id=service.id,
        booking_date=local_date,
        doctor_id=doctor_id,
        now=now,
    )

    assert timezone_name == "Asia/Tokyo"
    assert any(slot.start_at == expected_start_at for slot in slots)
    assert expected_start_at.hour == 1
    assert expected_start_at.minute == 0


def test_appointment_creation_persists_workspace_fallback_slot_as_utc(monkeypatch):
    _use_deterministic_booking_settings(monkeypatch)
    workspace, branch, service, doctor_id, db = _booking_context()
    now, _local_date, expected_start_at = _future_tokyo_slot()
    patient_id = uuid4()

    monkeypatch.setattr(appointment_creation, "configured_device_price", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        appointment_creation,
        "initial_confirmation_decision",
        lambda *_args, **_kwargs: SimpleNamespace(status="confirmed", confirmed_at=now),
    )
    monkeypatch.setattr(appointment_creation, "record_activity_event", lambda *_args, **_kwargs: None)

    appointment = appointment_creation.create_appointment_operation(
        db,
        workspace=workspace,
        patient_id=patient_id,
        branch_id=branch.id,
        doctor_id=doctor_id,
        service_id=service.id,
        requested_start_at=expected_start_at,
        created_by_user_id=None,
        source="staff",
        actor_type="staff",
        now=now,
    )

    assert appointment.start_at == expected_start_at
    assert appointment.end_at == expected_start_at + timedelta(minutes=30)
    assert appointment.start_at.utcoffset() == timedelta(0)


def test_appointment_reschedule_persists_workspace_fallback_slot_as_utc(monkeypatch):
    _use_deterministic_booking_settings(monkeypatch)
    workspace, branch, service, doctor_id, db = _booking_context()
    now, _local_date, expected_start_at = _future_tokyo_slot()
    current_start_at = expected_start_at + timedelta(days=1)
    current = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace.id,
        patient_id=uuid4(),
        branch_id=branch.id,
        doctor_id=doctor_id,
        service_id=service.id,
        status="confirmed",
        start_at=current_start_at,
        end_at=current_start_at + timedelta(minutes=30),
        payment_status="unpaid",
        amount_paid_minor=0,
        payment_method=None,
        billing_context="standard",
        patient_package_id=None,
        package_external_id=None,
        visit_group_id=None,
        lead_id=None,
        source="staff",
        laser_device_key=None,
        customer_note=None,
    )
    db.current = current

    monkeypatch.setattr(
        appointment_operations,
        "initial_confirmation_decision",
        lambda *_args, **_kwargs: SimpleNamespace(status="confirmed", confirmed_at=now),
    )
    for name in (
        "reallocate_appointment_payments_on_reschedule",
        "transfer_package_usage",
        "release_appointment_pulse_usage",
        "transfer_campaign_booking_conversion",
        "record_activity_event",
    ):
        monkeypatch.setattr(appointment_operations, name, lambda *_args, **_kwargs: None)

    replacement, previous = appointment_operations.reschedule_appointment_operation(
        db,
        workspace=workspace,
        appointment_id=current.id,
        requested_start_at=expected_start_at,
        changed_by_user_id=None,
        now=now,
        minimum_notice_minutes_override=0,
    )

    assert previous is current
    assert current.status == "rescheduled"
    assert replacement.start_at == expected_start_at
    assert replacement.end_at == expected_start_at + timedelta(minutes=30)
    assert replacement.start_at.utcoffset() == timedelta(0)
