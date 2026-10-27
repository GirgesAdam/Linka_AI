from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.routes import booking as booking_routes
from app.integrations.clinic.base import CreateAppointmentRequest
from app.integrations.clinic.tia_database import TiaDatabaseClinicAdapter
from app.schemas.booking import AppointmentCreate, AppointmentReschedule
from app.services import appointment_creation, appointment_operations
from app.services.booking import BookingRuleError


def _create_existing(**overrides):
    values = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "patient_id": uuid4(),
        "branch_id": uuid4(),
        "doctor_id": uuid4(),
        "service_id": uuid4(),
        "start_at": datetime(2026, 10, 28, 8, 0, tzinfo=UTC),
        "laser_device_key": "laser-a",
        "patient_package_id": uuid4(),
        "billing_context": "standard",
        "visit_group_id": uuid4(),
        "lead_id": uuid4(),
        "source": "ai",
        "is_quick_booking": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _create_kwargs(existing):
    return {
        "patient_id": existing.patient_id,
        "branch_id": existing.branch_id,
        "doctor_id": existing.doctor_id,
        "service_id": existing.service_id,
        "requested_start_at": existing.start_at.astimezone(timezone(timedelta(hours=2))),
        "patient_package_id": existing.patient_package_id,
        "use_pulse_balance": False,
        "visit_group_id": existing.visit_group_id,
        "lead_id": existing.lead_id,
        "source": existing.source,
        "laser_device_key": existing.laser_device_key,
        "is_quick_booking": False,
    }


def test_create_exact_replay_matches_canonical_instant() -> None:
    existing = _create_existing()
    assert appointment_creation.appointment_matches_create_intent(existing, **_create_kwargs(existing))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("patient_id", uuid4()),
        ("branch_id", uuid4()),
        ("doctor_id", uuid4()),
        ("service_id", uuid4()),
        ("requested_start_at", datetime(2026, 10, 28, 9, 0, tzinfo=UTC)),
        ("laser_device_key", "laser-b"),
        ("patient_package_id", uuid4()),
        ("visit_group_id", uuid4()),
        ("lead_id", uuid4()),
        ("source", "staff"),
        ("is_quick_booking", True),
    ],
)
def test_create_material_mismatch_conflicts(field: str, replacement: object) -> None:
    existing = _create_existing()
    kwargs = _create_kwargs(existing)
    kwargs[field] = replacement
    assert not appointment_creation.appointment_matches_create_intent(existing, **kwargs)


def test_create_pulse_context_mismatch_conflicts() -> None:
    existing = _create_existing(patient_package_id=None, billing_context="standard")
    kwargs = _create_kwargs(existing)
    kwargs["patient_package_id"] = None
    kwargs["use_pulse_balance"] = True
    assert not appointment_creation.appointment_matches_create_intent(existing, **kwargs)


def test_create_service_exact_replay_skips_mutable_validation(monkeypatch) -> None:
    workspace = SimpleNamespace(id=uuid4())
    existing = _create_existing(workspace_id=workspace.id, source="ai")
    db = MagicMock()
    db.scalar.return_value = existing
    configured = MagicMock(side_effect=AssertionError("must not revalidate exact replay"))
    slots = MagicMock(side_effect=AssertionError("must not revalidate exact replay"))
    monkeypatch.setattr(appointment_creation, "configured_device_price", configured)
    monkeypatch.setattr(appointment_creation, "find_exact_slot", slots)

    result = appointment_creation.create_appointment_operation(
        db,
        workspace=workspace,
        patient_id=existing.patient_id,
        branch_id=existing.branch_id,
        doctor_id=existing.doctor_id,
        service_id=existing.service_id,
        requested_start_at=existing.start_at,
        created_by_user_id=None,
        patient_package_id=existing.patient_package_id,
        use_pulse_balance=False,
        visit_group_id=existing.visit_group_id,
        lead=SimpleNamespace(id=existing.lead_id),
        source="ai",
        laser_device_key=existing.laser_device_key,
        idempotency_key="create-replay",
        actor_type="ai",
    )

    assert result is existing
    db.flush.assert_not_called()
    configured.assert_not_called()
    slots.assert_not_called()


def test_create_service_changed_time_conflicts_before_any_write(monkeypatch) -> None:
    workspace = SimpleNamespace(id=uuid4())
    existing = _create_existing(workspace_id=workspace.id, source="ai")
    db = MagicMock()
    db.scalar.return_value = existing
    monkeypatch.setattr(appointment_creation, "configured_device_price", MagicMock())
    monkeypatch.setattr(appointment_creation, "find_exact_slot", MagicMock())

    with pytest.raises(appointment_operations.AppointmentOperationError, match="different appointment operation"):
        appointment_creation.create_appointment_operation(
            db,
            workspace=workspace,
            patient_id=existing.patient_id,
            branch_id=existing.branch_id,
            doctor_id=existing.doctor_id,
            service_id=existing.service_id,
            requested_start_at=existing.start_at + timedelta(hours=2),
            created_by_user_id=None,
            patient_package_id=existing.patient_package_id,
            visit_group_id=existing.visit_group_id,
            lead=SimpleNamespace(id=existing.lead_id),
            source="ai",
            laser_device_key=existing.laser_device_key,
            idempotency_key="create-replay",
            actor_type="ai",
        )

    db.flush.assert_not_called()


def _reschedule_pair():
    previous = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        patient_id=uuid4(),
        branch_id=uuid4(),
        doctor_id=uuid4(),
        service_id=uuid4(),
        laser_device_key="laser-a",
    )
    replacement = SimpleNamespace(
        id=uuid4(),
        workspace_id=previous.workspace_id,
        patient_id=previous.patient_id,
        branch_id=previous.branch_id,
        doctor_id=previous.doctor_id,
        service_id=previous.service_id,
        laser_device_key=previous.laser_device_key,
        start_at=datetime(2026, 10, 29, 9, 0, tzinfo=UTC),
        rescheduled_from_appointment_id=previous.id,
    )
    return previous, replacement


def _reschedule_call_kwargs(previous, replacement):
    return {
        "appointment_id": previous.id,
        "requested_start_at": replacement.start_at.astimezone(timezone(timedelta(hours=2))),
        "branch_id": None,
        "doctor_id": None,
        "service_id": None,
        "laser_device_key": None,
        "patient_id": previous.patient_id,
    }


def test_reschedule_exact_replay_uses_effective_optional_target() -> None:
    previous, replacement = _reschedule_pair()
    assert appointment_operations._reschedule_replay_matches(
        replacement=replacement,
        previous=previous,
        **_reschedule_call_kwargs(previous, replacement),
    )


@pytest.mark.parametrize("field", ["appointment_id", "requested_start_at", "branch_id", "doctor_id", "service_id", "laser_device_key"])
def test_reschedule_material_mismatch_conflicts(field: str) -> None:
    previous, replacement = _reschedule_pair()
    kwargs = _reschedule_call_kwargs(previous, replacement)
    if field == "requested_start_at":
        kwargs[field] = replacement.start_at + timedelta(hours=1)
    elif field == "laser_device_key":
        kwargs[field] = "laser-b"
    else:
        kwargs[field] = uuid4()
    assert not appointment_operations._reschedule_replay_matches(
        replacement=replacement,
        previous=previous,
        **kwargs,
    )


def test_reschedule_exact_replay_and_mismatch_have_zero_side_effects(monkeypatch) -> None:
    previous, replacement = _reschedule_pair()
    workspace = SimpleNamespace(id=previous.workspace_id)
    db = MagicMock()
    db.scalar.side_effect = [replacement, previous]
    side_effects = [
        "reallocate_appointment_payments_on_reschedule",
        "transfer_package_usage",
        "release_appointment_pulse_usage",
        "transfer_campaign_booking_conversion",
        "add_appointment_history",
        "record_activity_event",
    ]
    mocks = {}
    for name in side_effects:
        mocks[name] = MagicMock()
        monkeypatch.setattr(appointment_operations, name, mocks[name])

    replay, original = appointment_operations.reschedule_appointment_operation(
        db,
        workspace=workspace,
        appointment_id=previous.id,
        requested_start_at=replacement.start_at,
        changed_by_user_id=None,
        patient_id=previous.patient_id,
        idempotency_key="reschedule-replay",
    )
    assert replay is replacement
    assert original is previous
    db.flush.assert_not_called()
    for mock in mocks.values():
        mock.assert_not_called()

    db.reset_mock()
    db.scalar.side_effect = [replacement, previous]
    with pytest.raises(appointment_operations.AppointmentOperationError, match="different appointment reschedule"):
        appointment_operations.reschedule_appointment_operation(
            db,
            workspace=workspace,
            appointment_id=previous.id,
            requested_start_at=replacement.start_at + timedelta(hours=1),
            changed_by_user_id=None,
            patient_id=previous.patient_id,
            idempotency_key="reschedule-replay",
        )
    db.flush.assert_not_called()
    for mock in mocks.values():
        mock.assert_not_called()


def test_create_route_mismatch_returns_http_409(monkeypatch) -> None:
    workspace_id = uuid4()
    existing = _create_existing(
        workspace_id=workspace_id,
        source="staff",
        patient_package_id=None,
        visit_group_id=None,
        lead_id=None,
        laser_device_key=None,
    )
    db = MagicMock()
    db.scalar.return_value = existing
    access = SimpleNamespace(workspace=SimpleNamespace(id=workspace_id), user=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(booking_routes, "require_local_appointment_write", MagicMock())
    payload = AppointmentCreate(
        patient_id=existing.patient_id,
        branch_id=existing.branch_id,
        doctor_id=existing.doctor_id,
        service_id=existing.service_id,
        start_at=existing.start_at + timedelta(hours=1),
        source="staff",
    )

    with pytest.raises(HTTPException) as exc_info:
        booking_routes.create_appointment(payload, access, db, idempotency_key="route-create")
    assert exc_info.value.status_code == 409


def test_reschedule_route_mismatch_returns_http_409(monkeypatch) -> None:
    appointment_id = uuid4()
    access = SimpleNamespace(
        workspace=SimpleNamespace(id=uuid4()),
        user=SimpleNamespace(id=uuid4()),
    )
    db = MagicMock()
    monkeypatch.setattr(booking_routes, "require_local_appointment_write", MagicMock())
    monkeypatch.setattr(
        booking_routes,
        "reschedule_appointment_operation",
        MagicMock(side_effect=appointment_operations.AppointmentOperationError(
            appointment_operations.RESCHEDULE_IDEMPOTENCY_CONFLICT
        )),
    )
    payload = AppointmentReschedule(start_at=datetime(2026, 10, 29, 9, 0, tzinfo=UTC))

    with pytest.raises(HTTPException) as exc_info:
        booking_routes.reschedule_appointment(
            appointment_id, payload, access, db, idempotency_key="route-reschedule"
        )
    assert exc_info.value.status_code == 409
    db.rollback.assert_called_once()


def test_staff_and_agent_key_lifecycle_contracts_are_stable() -> None:
    root = Path(__file__).resolve().parents[1]
    create_action = (root / "../frontend/src/app/(dashboard)/appointments/actions.ts").resolve().read_text(encoding="utf-8")
    create_form = (root / "../frontend/src/app/(dashboard)/appointments/manual-appointment-form.tsx").resolve().read_text(encoding="utf-8")
    detail_action = (root / "../frontend/src/app/(dashboard)/appointments/[appointmentId]/actions.ts").resolve().read_text(encoding="utf-8")
    reschedule_page = (root / "../frontend/src/app/(dashboard)/appointments/[appointmentId]/reschedule/page.tsx").resolve().read_text(encoding="utf-8")
    live_chat = (root / "app/services/agent_v2/live_chat.py").read_text(encoding="utf-8")

    assert "operationIdempotencyKey(formData, `dashboard-${bookingMode}-appointment`)" in create_action
    assert "randomUUID" not in create_action
    assert 'formData.set("operation_token", token)' in create_form
    assert "window.sessionStorage" in create_form
    assert "createClientRequestId" in create_form

    assert "dashboard-reschedule:${appointmentId}:${new Date(startAt).toISOString()}" in detail_action
    assert "randomUUID" not in detail_action
    assert "PendingSubmitButton" in reschedule_page

    assert 'idempotency_key=f"v2:{inbound.id}:{step.operation_index}"' in live_chat


def test_reschedule_different_target_gets_different_staff_key() -> None:
    appointment_id = "11111111-1111-4111-8111-111111111111"
    first = f"dashboard-reschedule:{appointment_id}:{datetime(2026, 10, 29, 9, 0, tzinfo=UTC).isoformat().replace('+00:00', 'Z')}"
    second = f"dashboard-reschedule:{appointment_id}:{datetime(2026, 10, 29, 10, 0, tzinfo=UTC).isoformat().replace('+00:00', 'Z')}"
    assert first != second
    assert len(first) <= 128
    assert len(second) <= 128



def test_legacy_agent_create_exact_replay_precedes_availability(monkeypatch) -> None:
    workspace = SimpleNamespace(id=uuid4())
    existing = _create_existing(
        workspace_id=workspace.id,
        source="ai",
        patient_package_id=None,
        visit_group_id=None,
        lead_id=None,
    )
    db = MagicMock()
    db.scalar.return_value = existing
    adapter = TiaDatabaseClinicAdapter(db=db, workspace=workspace)
    monkeypatch.setattr(adapter, "_require_local_appointment_write", MagicMock())
    monkeypatch.setattr(adapter, "require_capability", MagicMock())
    record = SimpleNamespace(id=str(existing.id))
    monkeypatch.setattr(adapter, "_appointment_record", MagicMock(return_value=record))
    slot_lookup = MagicMock(side_effect=AssertionError("exact replay must not revalidate availability"))
    monkeypatch.setattr("app.integrations.clinic.tia_database.find_exact_slot", slot_lookup)

    result = adapter.create_appointment(
        CreateAppointmentRequest(
            patient_id=str(existing.patient_id),
            branch_id=str(existing.branch_id),
            service_id=str(existing.service_id),
            doctor_id=str(existing.doctor_id),
            start_at=existing.start_at.astimezone(timezone(timedelta(hours=2))),
            operation_id="legacy-create-replay",
            laser_device_key=existing.laser_device_key,
        )
    )

    assert result.appointment is record
    slot_lookup.assert_not_called()
    db.add.assert_not_called()
    db.flush.assert_not_called()


def test_legacy_agent_create_changed_service_conflicts_before_availability(monkeypatch) -> None:
    workspace = SimpleNamespace(id=uuid4())
    existing = _create_existing(
        workspace_id=workspace.id,
        source="ai",
        patient_package_id=None,
        visit_group_id=None,
        lead_id=None,
    )
    db = MagicMock()
    db.scalar.return_value = existing
    adapter = TiaDatabaseClinicAdapter(db=db, workspace=workspace)
    monkeypatch.setattr(adapter, "_require_local_appointment_write", MagicMock())
    monkeypatch.setattr(adapter, "require_capability", MagicMock())
    slot_lookup = MagicMock(side_effect=AssertionError("mismatch must conflict before availability"))
    monkeypatch.setattr("app.integrations.clinic.tia_database.find_exact_slot", slot_lookup)

    with pytest.raises(BookingRuleError, match="different appointment operation"):
        adapter.create_appointment(
            CreateAppointmentRequest(
                patient_id=str(existing.patient_id),
                branch_id=str(existing.branch_id),
                service_id=str(uuid4()),
                doctor_id=str(existing.doctor_id),
                start_at=existing.start_at,
                operation_id="legacy-create-replay",
                laser_device_key=existing.laser_device_key,
            )
        )

    slot_lookup.assert_not_called()
    db.add.assert_not_called()
    db.flush.assert_not_called()



def test_create_legitimate_new_key_allows_new_operation(monkeypatch) -> None:
    workspace = SimpleNamespace(id=uuid4())
    patient_id = uuid4()
    branch_id = uuid4()
    doctor_id = uuid4()
    service_id = uuid4()
    start_at = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
    db = MagicMock()
    db.scalar.return_value = None
    slot = SimpleNamespace(
        start_at=start_at,
        end_at=start_at + timedelta(minutes=30),
        busy_start_at=start_at,
        busy_end_at=start_at + timedelta(minutes=30),
        duration_minutes=30,
        price_minor=10000,
        currency="EGP",
        laser_device_key=None,
        laser_device_name=None,
    )
    monkeypatch.setattr(appointment_creation, "configured_device_price", MagicMock(return_value=None))
    monkeypatch.setattr(appointment_creation, "find_exact_slot", MagicMock(return_value=slot))
    monkeypatch.setattr(
        appointment_creation,
        "initial_confirmation_decision",
        MagicMock(return_value=SimpleNamespace(status="confirmed", confirmed_at=start_at)),
    )
    monkeypatch.setattr(appointment_creation, "record_activity_event", MagicMock())
    monkeypatch.setattr(appointment_creation, "add_appointment_history", MagicMock())

    created = appointment_creation.create_appointment_operation(
        db,
        workspace=workspace,
        patient_id=patient_id,
        branch_id=branch_id,
        doctor_id=doctor_id,
        service_id=service_id,
        requested_start_at=start_at,
        created_by_user_id=None,
        idempotency_key="legitimate-new-key",
    )

    assert created.idempotency_key == "legitimate-new-key"
    assert created.start_at == start_at
    assert db.add.called
    assert db.flush.called


def test_booking_idempotency_lookups_remain_workspace_scoped() -> None:
    creation_source = Path(appointment_creation.__file__).read_text(encoding="utf-8")
    operations_source = Path(appointment_operations.__file__).read_text(encoding="utf-8")
    route_source = Path(booking_routes.__file__).read_text(encoding="utf-8")

    assert "Appointment.workspace_id == workspace.id" in creation_source
    assert "Appointment.workspace_id == workspace.id" in operations_source
    assert "Appointment.workspace_id == access.workspace.id" in route_source
