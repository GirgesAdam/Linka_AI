from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.integrations.clinic.base import AppointmentReadResult, AppointmentRecord
from app.services.agent_v2.active_task_progress import plan_active_task_progress
from app.services.agent_v2.orchestrator import (
    _active_reschedule_target_validation_request,
    _canonical_reschedule_target_is_non_actionable,
    _invalidated_reschedule_step,
)
from app.services.agent_v2.planner import PlanStep, ReadRequest, VerificationFacts
from app.services.agent_v2.read_executor import (
    ReadExecutionBundle,
    ReadExecutionContext,
    ReadResult,
    execute_step_reads,
)
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import (
    apply_step_state,
    finalize_step_after_state_transition,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
PATIENT_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeAdapter:
    def __init__(self, appointments: list[AppointmentRecord]) -> None:
        self.appointments = tuple(appointments)

    def require_capability(self, _capability) -> None:
        return None

    def get_patient_appointments(self, _request) -> AppointmentReadResult:
        return AppointmentReadResult(appointments=self.appointments)


def _context(adapter: FakeAdapter) -> ReadExecutionContext:
    return ReadExecutionContext(
        db=SimpleNamespace(),
        workspace=SimpleNamespace(
            id=WORKSPACE_ID,
            name="Tia Clinic",
            timezone="Africa/Cairo",
            primary_branch_id=None,
        ),
        patient=SimpleNamespace(id=PATIENT_ID),
        now=NOW,
        catalog={
            "services": [
                {
                    "id": "svc-hydra",
                    "name": "Hydrafacial",
                    "requires_laser_device": False,
                    "laser_devices": [],
                }
            ],
            "doctors": [
                {
                    "id": "doc-old",
                    "name": "Old Doctor",
                    "service_ids": ["svc-hydra"],
                }
            ],
            "branches": [],
        },
        adapter=adapter,
    )


def _semantic_context():
    return build_semantic_context(
        {
            "services": [
                {
                    "id": "svc-hydra",
                    "name": "Hydrafacial",
                    "requires_laser_device": False,
                    "laser_devices": [],
                },
                {
                    "id": "svc-new",
                    "name": "New Service",
                    "requires_laser_device": False,
                    "laser_devices": [],
                },
            ],
            "doctors": [],
            "appointments": [],
        }
    )


def _appointment(*, status: str) -> AppointmentRecord:
    start = datetime(2026, 9, 28, 7, 0, tzinfo=UTC)
    return AppointmentRecord(
        appointment_id="apt-old",
        patient_id=str(PATIENT_ID),
        status=status,
        service_id="svc-hydra",
        service_name="Hydrafacial",
        branch_id="branch-1",
        branch_name="Tia Clinic",
        doctor_id="doc-old",
        doctor_name="Old Doctor",
        start_at=start,
        end_at=start + timedelta(hours=1),
        timezone="Africa/Cairo",
        price_minor=180000,
        currency="EGP",
        payment_status="unpaid",
        amount_paid_minor=None,
        payment_method="unknown",
        billing_context="standard",
        package_external_id=None,
        patient_package_id=None,
        laser_device_key=None,
        laser_device_name=None,
        visit_group_id=None,
    )


def _state() -> RescheduleTaskState:
    return RescheduleTaskState(
        status="awaiting_choice",
        write_authorization=WriteAuthorization(
            operation="reschedule",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        target=RescheduleTarget(
            appointment_id="apt-old",
            service_id="svc-hydra",
            doctor_id="doc-old",
            device_key=None,
            start_local="2026-09-28T10:00:00+03:00",
        ),
        replacement=CustomerConstraints(
            service_id="svc-hydra",
            doctor_id="doc-old",
            date=DateConstraint(mode="exact", start_date="2026-09-29"),
            time=TimeConstraint(mode="exact", start_time="10:00"),
        ),
    )


def _progress_step() -> PlanStep:
    return plan_active_task_progress(
        _state(),
        operation_index=0,
        context=_semantic_context(),
    )


@pytest.mark.parametrize("status", ["cancelled", "rescheduled"])
def test_non_actionable_canonical_target_is_verified_before_availability(status: str) -> None:
    state = _state()
    step = _progress_step()
    request = _active_reschedule_target_validation_request(step, state)
    assert request == ReadRequest(
        kind="appointments",
        parameters={"appointment_id": "apt-old"},
    )

    validation_step = step.model_copy(update={"reads": [request]})
    reads = execute_step_reads(
        validation_step,
        _context(FakeAdapter([_appointment(status=status)])),
    )

    assert reads.verification.appointment_match_count == 0
    assert _canonical_reschedule_target_is_non_actionable(request, reads) is True
    assert [result.kind for result in reads.results] == ["appointments"]


def test_active_canonical_target_preserves_reschedule_task() -> None:
    state = _state()
    step = _progress_step()
    request = _active_reschedule_target_validation_request(step, state)
    assert request is not None
    reads = execute_step_reads(
        step.model_copy(update={"reads": [request]}),
        _context(FakeAdapter([_appointment(status="confirmed")])),
    )

    assert reads.verification.appointment_match_count == 1
    assert _canonical_reschedule_target_is_non_actionable(request, reads) is False


def test_failed_target_read_does_not_discard_task() -> None:
    request = ReadRequest(kind="appointments", parameters={"appointment_id": "apt-old"})
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="appointments",
                ok=False,
                payload={},
                error_code="adapter_unavailable",
            )
        ],
        verification=VerificationFacts(appointment_match_count=0),
    )

    assert _canonical_reschedule_target_is_non_actionable(request, reads) is False


def test_ambiguous_target_read_does_not_discard_task() -> None:
    request = ReadRequest(kind="appointments", parameters={"appointment_id": "apt-old"})
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="appointments",
                ok=True,
                payload={"appointments": [{}, {}]},
            )
        ],
        verification=VerificationFacts(appointment_match_count=2),
    )

    assert _canonical_reschedule_target_is_non_actionable(request, reads) is False


def test_verified_non_actionable_target_closes_active_reschedule_task() -> None:
    state = _state()
    step = _invalidated_reschedule_step(_progress_step())
    operation = TurnOperation(
        type="continue_active",
        entities=TurnEntities(),
        execution_intent="execute",
    )

    transition = apply_step_state(
        state,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-2",
    )
    final_step = finalize_step_after_state_transition(step, transition)

    assert transition.active_task is None
    assert transition.changed is True
    assert final_step.disposition == "respond"
    assert final_step.write_intent is None
    assert final_step.response_goal == "clarification"
    assert final_step.facts["canonical_target_non_actionable"] is True
    assert final_step.facts["active_task_invalidated"] is True


def test_new_booking_after_invalidation_starts_without_old_reschedule_constraints() -> None:
    state = _state()
    invalidated = _invalidated_reschedule_step(_progress_step())
    cleared = apply_step_state(
        state,
        step=invalidated,
        operation=TurnOperation(
            type="continue_active",
            entities=TurnEntities(),
            execution_intent="execute",
        ),
        reads=None,
        now=NOW,
        turn_id="turn-2",
    ).active_task
    assert cleared is None

    booking_step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        state_action="start_booking",
        response_goal="present_availability",
        facts={
            "service_id": "svc-new",
            "date": {"mode": "exact", "start_date": "2026-10-03"},
            "time": {"mode": "exact", "start_time": "17:00"},
        },
    )
    booking = apply_step_state(
        cleared,
        step=booking_step,
        operation=TurnOperation(
            type="book",
            entities=TurnEntities(
                date=DateConstraint(mode="exact", start_date="2026-10-03"),
                time=TimeConstraint(mode="exact", start_time="17:00"),
            ),
            execution_intent="execute",
        ),
        reads=None,
        now=NOW,
        turn_id="turn-3",
    ).active_task

    assert isinstance(booking, BookingTaskState)
    assert booking.constraints.service_id == "svc-new"
    assert booking.constraints.doctor_id is None
    assert booking.constraints.device_key is None
    assert booking.constraints.date == DateConstraint(
        mode="exact",
        start_date="2026-10-03",
    )
    assert booking.constraints.time == TimeConstraint(
        mode="exact",
        start_time="17:00",
    )
