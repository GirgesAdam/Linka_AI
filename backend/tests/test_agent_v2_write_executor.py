from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.integrations.clinic.authority import ClinicIntegrationAuthorityError
from app.services.agent_v2 import write_executor
from app.services.agent_v2.planner import PlanStep, WriteIntent
from app.services.appointment_operations import (
    AppointmentCancellationOverrideRequired,
    AppointmentOperationError,
)


def _step(kind: str, parameters: dict[str, object], *, disposition: str = "write_ready") -> PlanStep:
    return PlanStep(
        operation_index=0,
        operation_type=kind,
        disposition=disposition,
        write_intent=WriteIntent(kind=kind, authorized=True, parameters=parameters),
    )


def _context() -> tuple[MagicMock, SimpleNamespace, SimpleNamespace]:
    return (
        MagicMock(),
        SimpleNamespace(id=uuid4()),
        SimpleNamespace(
            id=uuid4(),
            status="active",
            marketing_consent=False,
            marketing_consent_at=None,
        ),
    )


def _booking_parameters() -> dict[str, object]:
    return {
        "branch_id": str(uuid4()),
        "doctor_id": str(uuid4()),
        "service_id": str(uuid4()),
        "start_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }


def test_write_executor_does_not_accept_raw_customer_text() -> None:
    parameters = set(inspect.signature(write_executor.execute_write_ready_step).parameters)
    assert parameters.isdisjoint({"message", "raw_text", "customer_text", "customer_message"})


def test_write_executor_rejects_step_before_write_ready(monkeypatch) -> None:
    db, workspace, patient = _context()
    create = MagicMock()
    monkeypatch.setattr(write_executor, "create_appointment_operation", create)

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_step("booking", _booking_parameters(), disposition="read"),
    )

    assert result["ok"] is False
    assert result["error_code"] == "write_not_ready"
    create.assert_not_called()
    db.commit.assert_not_called()


def test_verified_booking_executes_once_and_commits(monkeypatch) -> None:
    db, workspace, patient = _context()
    appointment = SimpleNamespace(id=uuid4(), status="confirmed")
    create = MagicMock(return_value=appointment)
    authority = MagicMock()
    monkeypatch.setattr(write_executor, "create_appointment_operation", create)
    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", authority)
    parameters = _booking_parameters()
    key = "v2-action-1"

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_step("booking", parameters),
        idempotency_key=key,
    )

    assert result == {
        "ok": True,
        "write_kind": "booking",
        "appointment_id": str(appointment.id),
        "status": "confirmed",
    }
    authority.assert_called_once_with(db, workspace_id=workspace.id, domain="appointments")
    create.assert_called_once()
    call = create.call_args.kwargs
    assert call["workspace"] is workspace
    assert call["patient_id"] == patient.id
    assert str(call["branch_id"]) == parameters["branch_id"]
    assert str(call["doctor_id"]) == parameters["doctor_id"]
    assert str(call["service_id"]) == parameters["service_id"]
    assert call["source"] == "ai"
    assert call["actor_type"] == "ai"
    assert call["created_by_user_id"] is None
    assert call["idempotency_key"] == key
    db.commit.assert_called_once_with()
    db.rollback.assert_not_called()


def test_blocked_patient_only_blocks_new_booking(monkeypatch) -> None:
    db, workspace, patient = _context()
    patient.status = "blocked"
    create = MagicMock()
    monkeypatch.setattr(write_executor, "create_appointment_operation", create)
    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", MagicMock())

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_step("booking", _booking_parameters()),
    )

    assert result["ok"] is False
    assert result["error_code"] == "patient_blocked"
    create.assert_not_called()
    db.commit.assert_not_called()


def test_verified_cancel_is_patient_scoped_and_ai_attributed(monkeypatch) -> None:
    db, workspace, patient = _context()
    appointment = SimpleNamespace(id=uuid4(), status="cancelled")
    cancel = MagicMock(return_value=appointment)
    monkeypatch.setattr(write_executor, "cancel_appointment_operation", cancel)
    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", MagicMock())
    appointment_id = uuid4()

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_step("cancel_appointment", {"appointment_id": str(appointment_id)}),
    )

    assert result["ok"] is True
    call = cancel.call_args.kwargs
    assert call["appointment_id"] == appointment_id
    assert call["patient_id"] == patient.id
    assert call["actor_type"] == "ai"
    assert call["override_policy"] is False
    assert call["actor_is_admin"] is False
    db.commit.assert_called_once_with()


def test_verified_reschedule_uses_only_canonical_parameters(monkeypatch) -> None:
    db, workspace, patient = _context()
    replacement = SimpleNamespace(id=uuid4(), status="confirmed")
    previous = SimpleNamespace(id=uuid4(), status="rescheduled")
    reschedule = MagicMock(return_value=(replacement, previous))
    monkeypatch.setattr(write_executor, "reschedule_appointment_operation", reschedule)
    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", MagicMock())
    parameters = {
        **_booking_parameters(),
        "appointment_id": str(previous.id),
        "device_key": "prime_lase",
    }

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_step("reschedule", parameters),
        idempotency_key="v2-reschedule-1",
    )

    assert result["ok"] is True
    assert result["previous_appointment_id"] == str(previous.id)
    call = reschedule.call_args.kwargs
    assert call["appointment_id"] == previous.id
    assert call["patient_id"] == patient.id
    assert call["laser_device_key"] == "prime_lase"
    assert call["idempotency_key"] == "v2-reschedule-1"
    db.commit.assert_called_once_with()


def test_package_purchase_uses_verified_offer_without_inventing_payment(monkeypatch) -> None:
    db, workspace, patient = _context()
    package = SimpleNamespace(id=uuid4(), status="active")
    purchase = MagicMock(return_value=package)
    monkeypatch.setattr(write_executor, "purchase_package_offer", purchase)
    offer_id = uuid4()

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_step("buy_package", {"package_offer_id": str(offer_id)}),
        idempotency_key="v2-package-1",
    )

    assert result["ok"] is True
    assert result["amount_paid_minor"] == 0
    call = purchase.call_args.kwargs
    assert call["offer_id"] == offer_id
    assert call["patient_id"] == patient.id
    assert call["amount_paid_minor"] == 0
    assert call["payment_method"] == "unknown"
    assert call["actor_type"] == "ai"
    assert call["idempotency_key"] == "v2-package-1"
    db.commit.assert_called_once_with()


def test_follow_up_reuses_native_ai_crm_scheduler(monkeypatch) -> None:
    db, workspace, patient = _context()
    due_at = datetime.now(UTC) + timedelta(hours=2)
    task = SimpleNamespace(id=uuid4(), status="pending", due_at=due_at)
    create_task = MagicMock(return_value=task)
    monkeypatch.setattr(write_executor, "create_crm_task", create_task)

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_step("follow_up", {"follow_up_at_local": due_at.isoformat()}),
        idempotency_key="v2-followup-1",
    )

    assert result["ok"] is True
    call = create_task.call_args.kwargs
    assert call["patient_id"] == patient.id
    assert call["source"] == "ai"
    assert call["execution_mode"] == "ai"
    assert call["commit"] is False
    assert call["dedupe_key"] == "v2-followup-1"
    db.commit.assert_called_once_with()


def test_marketing_update_changes_only_verified_patient_consent(monkeypatch) -> None:
    db, workspace, patient = _context()
    authority = MagicMock()
    activity = MagicMock()
    monkeypatch.setattr(write_executor, "require_tia_patient_fields_writable", authority)
    monkeypatch.setattr(write_executor, "record_activity_event", activity)

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_step("marketing_update", {"marketing_consent": True}),
    )

    assert result["ok"] is True
    assert patient.marketing_consent is True
    assert patient.marketing_consent_at is not None
    authority.assert_called_once_with(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        fields={"marketing_consent"},
    )
    activity.assert_called_once()
    db.commit.assert_called_once_with()


def test_appointment_authority_conflict_fails_closed(monkeypatch) -> None:
    db, workspace, patient = _context()
    authority = MagicMock(side_effect=ClinicIntegrationAuthorityError("external authority"))
    create = MagicMock()
    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", authority)
    monkeypatch.setattr(write_executor, "create_appointment_operation", create)

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_step("booking", _booking_parameters()),
    )

    assert result["ok"] is False
    assert result["requires_human"] is True
    assert result["error_code"] == "authority_conflict"
    create.assert_not_called()
    db.rollback.assert_called_once_with()
    db.commit.assert_not_called()


def test_policy_override_requirement_fails_closed_to_human(monkeypatch) -> None:
    db, workspace, patient = _context()
    cancel = MagicMock(side_effect=AppointmentCancellationOverrideRequired("override required"))
    monkeypatch.setattr(write_executor, "cancel_appointment_operation", cancel)
    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", MagicMock())

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_step("cancel_appointment", {"appointment_id": str(uuid4())}),
    )

    assert result["ok"] is False
    assert result["requires_human"] is True
    assert result["error_code"] == "staff_review_required"
    db.commit.assert_not_called()
    db.rollback.assert_called_once_with()


def test_write_failure_rolls_back_without_retry(monkeypatch) -> None:
    db, workspace, patient = _context()
    create = MagicMock(side_effect=AppointmentOperationError("slot conflict"))
    monkeypatch.setattr(write_executor, "create_appointment_operation", create)
    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", MagicMock())

    result = write_executor.execute_write_ready_step(
        db,
        workspace=workspace,
        patient=patient,
        step=_step("booking", _booking_parameters()),
    )

    assert result["ok"] is False
    assert result["error_code"] == "write_failed"
    create.assert_called_once()
    db.rollback.assert_called_once_with()
    db.commit.assert_not_called()


def test_explicit_time_mismatch_blocks_booking_before_write(monkeypatch) -> None:
    db, workspace, patient = _context()
    create = MagicMock()
    monkeypatch.setattr(write_executor, "create_appointment_operation", create)
    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", MagicMock())
    parameters = _booking_parameters()
    parameters["start_at"] = "2026-10-10T15:00:00+03:00"
    parameters["_explicit_user_time_24h"] = "03:00"

    result = write_executor.execute_write_ready_step(
        db, workspace=workspace, patient=patient, step=_step("booking", parameters)
    )

    assert result["ok"] is False
    assert result["error_code"] == "explicit_time_mismatch"
    create.assert_not_called()
    db.commit.assert_not_called()


def test_explicit_time_match_allows_booking(monkeypatch) -> None:
    db, workspace, patient = _context()
    appointment = SimpleNamespace(id=uuid4(), status="confirmed")
    create = MagicMock(return_value=appointment)
    monkeypatch.setattr(write_executor, "create_appointment_operation", create)
    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", MagicMock())
    monkeypatch.setattr(
        write_executor,
        "resolve_booking_package",
        MagicMock(return_value=SimpleNamespace(package_id=None, package_used=False, package_name=None)),
    )
    parameters = _booking_parameters()
    parameters["start_at"] = "2026-10-10T15:00:00+03:00"
    parameters["_explicit_user_time_24h"] = "15:00"

    result = write_executor.execute_write_ready_step(
        db, workspace=workspace, patient=patient, step=_step("booking", parameters)
    )

    assert result["ok"] is True
    create.assert_called_once()


def test_explicit_time_mismatch_blocks_reschedule_before_write(monkeypatch) -> None:
    db, workspace, patient = _context()
    reschedule = MagicMock()
    monkeypatch.setattr(write_executor, "reschedule_appointment_operation", reschedule)
    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", MagicMock())
    parameters = {
        **_booking_parameters(),
        "appointment_id": str(uuid4()),
        "start_at": "2026-10-10T15:00:00+03:00",
        "_explicit_user_time_24h": "03:00",
    }

    result = write_executor.execute_write_ready_step(
        db, workspace=workspace, patient=patient, step=_step("reschedule", parameters)
    )

    assert result["ok"] is False
    assert result["error_code"] == "explicit_time_mismatch"
    reschedule.assert_not_called()
    db.commit.assert_not_called()


def test_explicit_time_match_uses_workspace_local_clock_for_utc_start(monkeypatch) -> None:
    db, workspace, patient = _context()
    workspace.timezone = "Africa/Cairo"
    appointment = SimpleNamespace(id=uuid4(), status="confirmed")
    create = MagicMock(return_value=appointment)
    monkeypatch.setattr(write_executor, "create_appointment_operation", create)
    monkeypatch.setattr(write_executor, "require_tia_workspace_domain_write", MagicMock())
    monkeypatch.setattr(
        write_executor,
        "resolve_booking_package",
        MagicMock(return_value=SimpleNamespace(package_id=None, package_used=False, package_name=None)),
    )
    parameters = _booking_parameters()
    parameters["start_at"] = "2026-10-10T12:00:00+00:00"
    parameters["_explicit_user_time_24h"] = "15:00"

    result = write_executor.execute_write_ready_step(
        db, workspace=workspace, patient=patient, step=_step("booking", parameters)
    )

    assert result["ok"] is True
    create.assert_called_once()
