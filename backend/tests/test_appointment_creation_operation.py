from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services import appointment_creation


def test_create_appointment_operation_keeps_transaction_with_caller(monkeypatch) -> None:
    db = MagicMock()
    workspace = SimpleNamespace(id=uuid4())
    patient_id = uuid4()
    branch_id = uuid4()
    doctor_id = uuid4()
    service_id = uuid4()
    start_at = datetime.now(UTC) + timedelta(days=1)
    slot = SimpleNamespace(
        start_at=start_at,
        end_at=start_at + timedelta(minutes=30),
        busy_start_at=start_at,
        busy_end_at=start_at + timedelta(minutes=30),
        duration_minutes=30,
        price_minor=150000,
        currency="EGP",
        laser_device_key=None,
        laser_device_name=None,
    )
    monkeypatch.setattr(appointment_creation, "configured_device_price", MagicMock(return_value=None))
    monkeypatch.setattr(appointment_creation, "find_exact_slot", MagicMock(return_value=slot))
    monkeypatch.setattr(
        appointment_creation,
        "initial_confirmation_decision",
        MagicMock(return_value=SimpleNamespace(status="confirmed", confirmed_at=datetime.now(UTC))),
    )
    activity = MagicMock()
    monkeypatch.setattr(appointment_creation, "record_activity_event", activity)

    appointment = appointment_creation.create_appointment_operation(
        db,
        workspace=workspace,
        patient_id=patient_id,
        branch_id=branch_id,
        doctor_id=doctor_id,
        service_id=service_id,
        requested_start_at=start_at,
        created_by_user_id=None,
        source="ai",
        actor_type="ai",
    )

    assert appointment.workspace_id == workspace.id
    assert appointment.patient_id == patient_id
    assert appointment.branch_id == branch_id
    assert appointment.doctor_id == doctor_id
    assert appointment.service_id == service_id
    assert appointment.status == "confirmed"
    assert appointment.source == "ai"
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    assert db.flush.call_count >= 2
    activity.assert_called_once()
