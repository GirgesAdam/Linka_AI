from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.schemas.booking import AppointmentLaserUsageUpdate, QuickAppointmentCreate
from app.services.booking import _candidate_start_times


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_back_to_back_candidate_includes_exact_previous_end_between_grid_boundaries() -> None:
    start = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
    end = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    previous_end = start + timedelta(minutes=37)
    candidates = _candidate_start_times(
        interval_start=start,
        interval_end=end,
        interval_minutes=15,
        release_times=[previous_end],
    )
    assert previous_end in candidates
    assert start + timedelta(minutes=45) in candidates


def test_quick_booking_and_laser_usage_schemas_validate_staff_input() -> None:
    payload = QuickAppointmentCreate(
        patient_id=uuid4(),
        branch_id=uuid4(),
        doctor_id=uuid4(),
        service_id=uuid4(),
        start_at=datetime(2026, 9, 22, 17, 43, tzinfo=UTC),
        laser_device_key="prime_lase",
    )
    assert payload.start_at.minute == 43
    assert AppointmentLaserUsageUpdate(pulses_used=2350).pulses_used == 2350
    with pytest.raises(ValueError):
        AppointmentLaserUsageUpdate(pulses_used=-1)


def test_quick_booking_backend_is_an_audited_override() -> None:
    root = _root()
    route = (root / "backend/app/api/routes/booking.py").read_text(encoding="utf-8")
    schema = (root / "backend/app/schemas/booking.py").read_text(encoding="utf-8")
    model = (root / "backend/app/models/appointment.py").read_text(encoding="utf-8")
    migration = (root / "backend/alembic/versions/0079_quick_booking_laser_usage.py").read_text(encoding="utf-8")
    device_override = (
        root / "backend/alembic/versions/0080_quick_booking_device_override.py"
    ).read_text(encoding="utf-8")
    assert '@router.post(\n    "/appointments/quick"' in route
    assert "is_quick_booking=True" in route
    appointment_read = schema.split("class AppointmentRead(BaseModel):", 1)[1].split(
        "class AppointmentStatusHistoryRead", 1
    )[0]
    appointment_create = schema.split("class AppointmentCreate(BaseModel):", 1)[1].split(
        "class AppointmentCancel", 1
    )[0]
    assert "is_quick_booking: bool = False" in appointment_read
    assert "is_quick_booking" not in appointment_create
    assert "doctor_assignment_known=True" in route
    assert 'action="appointment.quick_created"' in route
    assert "quick_booking_integrity_detail" in route
    assert "except PackageOperationError as exc:" in route
    assert "excl_appointments_doctor_busy_time" in model
    assert "excl_appointments_laser_device_busy_time" in model
    assert model.count("NOT is_quick_booking") >= 2
    assert "NOT is_quick_booking" in migration
    assert "NOT is_quick_booking" in device_override
    availability = (root / "backend/app/services/booking.py").read_text(encoding="utf-8")
    assert availability.count("Appointment.is_quick_booking.is_(False)") >= 2


def test_quick_popup_closes_after_success_and_allows_minute_precision() -> None:
    root = _root()
    form = (root / "frontend/src/app/(dashboard)/appointments/manual-appointment-form.tsx").read_text(encoding="utf-8")
    dialog = (root / "frontend/src/app/(dashboard)/appointments/quick-appointment-dialog.tsx").read_text(encoding="utf-8")
    actions = (root / "frontend/src/app/(dashboard)/appointments/actions.ts").read_text(encoding="utf-8")
    assert "router.replace(successHref)" in form
    assert 'schedulingMode={column === "quick" ? "quick" : "standard"}' in dialog
    assert 'type="time"' in form
    assert 'step="60"' in form
    assert '"/booking/appointments/quick"' in actions
    page = (
        root / "frontend/src/app/(dashboard)/appointments/page.tsx"
    ).read_text(encoding="utf-8")
    assert 'column.id !== "quick"' in page
    assert "function appointmentsForColumn(" in page
    assert 'if (column === "quick")' in page
    assert "appointment.is_quick_booking === true" in page
    assert "appointment.is_quick_booking !== true" in page
    assert 'return "dermatology";' in page
    assert '{ id: "other", label: "أخرى" }' not in page
    assert "لا توجد حجوزات سريعة" in page
    assert "حجز سريع" in page
    assert "scheduling override could not be applied" in actions
    assert "التداخل الزمني مسموح في الحجز السريع" in actions


def test_laser_pulses_support_post_session_wallet_settlement() -> None:
    root = _root()
    detail = (root / "frontend/src/app/(dashboard)/appointments/[appointmentId]/page.tsx").read_text(encoding="utf-8")
    actions = (root / "frontend/src/app/(dashboard)/appointments/[appointmentId]/actions.ts").read_text(encoding="utf-8")
    route = (root / "backend/app/api/routes/booking.py").read_text(encoding="utf-8")
    pulse_service = (root / "backend/app/services/pulse_billing.py").read_text(encoding="utf-8")
    assert "عدد الـPulses المستخدمة فعليًا" in detail
    assert "الخصم الفعلي يتم بعد الجلسة" in (
        root / "frontend/src/app/(dashboard)/appointments/manual-appointment-form.tsx"
    ).read_text(encoding="utf-8")
    assert "/laser-usage" in actions
    assert "settle_appointment_pulses" in route
    assert "deficit_pulses" in pulse_service
    assert "resolve_pulse_deficit_with_pack" in pulse_service
    assert "resolve_pulse_deficit_with_overage" in pulse_service



def test_quick_booking_is_deterministically_human_only() -> None:
    root = _root()
    model = (root / "backend/app/models/appointment.py").read_text(encoding="utf-8")
    migration = (
        root / "backend/alembic/versions/0081_quick_booking_staff_only.py"
    ).read_text(encoding="utf-8")
    capability_policy = (
        root / "backend/app/agents/capability_policy.py"
    ).read_text(encoding="utf-8")
    write_executor = (
        root / "backend/app/services/agent_v2/write_executor.py"
    ).read_text(encoding="utf-8")

    assert "appointment_quick_booking_staff_only" in model
    assert "source = 'staff' AND created_by_user_id IS NOT NULL" in model
    assert "appointment_quick_booking_staff_only" in migration
    assert "quick_book_appointment" not in capability_policy
    assert "/appointments/quick" not in write_executor
    assert "is_quick_booking" not in write_executor
