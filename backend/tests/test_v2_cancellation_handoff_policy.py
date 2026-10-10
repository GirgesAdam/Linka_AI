from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.integrations.clinic.base import AppointmentReadResult, AppointmentRecord
from app.services.agent_v2.orchestrator import _appointment_choice_snapshot
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.planner import PlanStep, ReadRequest, VerificationFacts, WriteIntent
from app.services.agent_v2.read_executor import (
    ReadExecutionBundle,
    ReadExecutionContext,
    ReadResult,
    execute_step_reads,
)
from app.services.agent_v2.write_policy import advance_step_with_write_policies

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=ZoneInfo("Africa/Cairo"))


def _cancel_step(**parameters: object) -> PlanStep:
    return PlanStep(
        operation_index=0,
        operation_type="cancel_appointment",
        disposition="read",
        reads=[ReadRequest(kind="appointments", parameters=dict(parameters))],
        write_intent=WriteIntent(
            kind="cancel_appointment",
            authorized=True,
            parameters=dict(parameters),
        ),
        response_goal="cancellation_completed",
    )


def _confirm_step() -> PlanStep:
    return PlanStep(
        operation_index=0,
        operation_type="confirm_appointment",
        disposition="read",
        reads=[ReadRequest(kind="appointments")],
        write_intent=WriteIntent(kind="confirm_appointment", authorized=True, parameters={}),
        response_goal="appointment_confirmed",
    )


def _row(appointment_id: str, *, service_id: str = "svc-hydra") -> dict[str, object]:
    return {
        "appointment_id": appointment_id,
        "service_id": service_id,
        "service_name": service_id,
        "doctor_id": "doctor-1",
        "doctor_name": "Doctor",
        "start_local": "2026-10-13T16:00:00+03:00",
        "payment_status": "unpaid",
        "amount_paid_minor": 0,
        "billing_context": "standard",
        "patient_package_id": None,
        "package_external_id": None,
    }


def _bundle(count: int) -> ReadExecutionBundle:
    rows = [_row(f"apt-{index + 1}") for index in range(count)]
    verified = {"appointment_id": "apt-1"} if count == 1 else {}
    return ReadExecutionBundle(
        results=[ReadResult(kind="appointments", ok=True, payload={"appointments": rows})],
        verification=VerificationFacts(
            appointment_match_count=count,
            verified_parameters=verified,
        ),
    )


def _appointment(
    appointment_id: str,
    *,
    patient_id: str = "patient-current",
    service_id: str = "svc-hydra",
    status: str = "confirmed",
    visit_group_id: str | None = None,
) -> AppointmentRecord:
    start = NOW + timedelta(days=2)
    return AppointmentRecord(
        appointment_id=appointment_id,
        patient_id=patient_id,
        status=status,
        service_id=service_id,
        service_name=service_id,
        branch_id="branch-1",
        branch_name="Main",
        doctor_id="doctor-1",
        doctor_name="Doctor",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        timezone="Africa/Cairo",
        price_minor=100_000,
        currency="EGP",
        payment_status="unpaid",
        amount_paid_minor=0,
        visit_group_id=visit_group_id,
    )


class _PatientScopedAdapter:
    def __init__(self, rows: list[AppointmentRecord]) -> None:
        self.rows = rows
        self.requested_patient_ids: list[str] = []

    def require_capability(self, _capability: object) -> None:
        return None

    def get_patient_appointments(self, request: object) -> AppointmentReadResult:
        patient_id = str(request.patient_id)
        self.requested_patient_ids.append(patient_id)
        return AppointmentReadResult(
            appointments=tuple(row for row in self.rows if row.patient_id == patient_id)
        )


def _execute(rows: list[AppointmentRecord], **parameters: object) -> ReadExecutionBundle:
    adapter = _PatientScopedAdapter(rows)
    context = ReadExecutionContext(
        db=SimpleNamespace(),
        workspace=SimpleNamespace(id="workspace-current"),
        patient=SimpleNamespace(id="patient-current"),
        now=NOW,
        adapter=adapter,
    )
    bundle = execute_step_reads(_cancel_step(**parameters), context)
    assert adapter.requested_patient_ids == ["patient-current"]
    return bundle


def test_ca1_zero_eligible_keeps_existing_blocked_behavior() -> None:
    advanced = advance_step_with_write_policies(_cancel_step(), _bundle(0))
    assert advanced.disposition == "blocked"
    assert advanced.response_goal == "clarification"


def test_ca2_exactly_one_eligible_keeps_existing_write_path() -> None:
    advanced = advance_step_with_write_policies(_cancel_step(), _bundle(1))
    assert advanced.disposition == "write_ready"
    assert advanced.write_intent is not None
    assert advanced.write_intent.parameters["appointment_id"] == "apt-1"


def test_ca3_two_eligible_handoff_before_choice_or_write() -> None:
    advanced = advance_step_with_write_policies(_cancel_step(), _bundle(2))
    assert advanced.disposition == "handoff"
    assert advanced.response_goal == "handoff"
    assert advanced.facts["reason"] == "multiple_eligible_cancellation_targets_require_staff"

    outcome = TurnOutcome(status="handoff", response_goal="handoff")
    assert (
        _appointment_choice_snapshot(
            step=advanced,
            outcome=outcome,
            now=NOW,
            turn_id="ca3",
        )
        is None
    )


def test_ca4_three_plus_eligible_handoff() -> None:
    advanced = advance_step_with_write_policies(_cancel_step(), _bundle(4))
    assert advanced.disposition == "handoff"
    assert advanced.response_goal == "handoff"


def test_cancellation_guard_does_not_change_confirmation_ambiguity() -> None:
    advanced = advance_step_with_write_policies(_confirm_step(), _bundle(2))
    assert advanced.disposition == "clarify"
    assert advanced.response_goal == "ask_appointment_choice"


def test_ca6_canonical_request_filter_can_narrow_many_raw_rows_to_one() -> None:
    bundle = _execute(
        [
            _appointment("apt-hydra", service_id="svc-hydra"),
            _appointment("apt-laser-1", service_id="svc-laser"),
            _appointment("apt-laser-2", service_id="svc-laser"),
        ],
        service_id="svc-hydra",
    )
    assert bundle.verification.appointment_match_count == 1
    advanced = advance_step_with_write_policies(_cancel_step(service_id="svc-hydra"), bundle)
    assert advanced.disposition == "write_ready"
    assert advanced.write_intent is not None
    assert advanced.write_intent.parameters["appointment_id"] == "apt-hydra"


def test_ca7_canonical_request_filter_remaining_two_hands_off() -> None:
    bundle = _execute(
        [
            _appointment("apt-hydra", service_id="svc-hydra"),
            _appointment("apt-laser-1", service_id="svc-laser"),
            _appointment("apt-laser-2", service_id="svc-laser"),
        ],
        service_id="svc-laser",
    )
    assert bundle.verification.appointment_match_count == 2
    advanced = advance_step_with_write_policies(_cancel_step(service_id="svc-laser"), bundle)
    assert advanced.disposition == "handoff"


def test_ca9_patient_and_actionable_status_filtering_precedes_candidate_count() -> None:
    bundle = _execute(
        [
            _appointment("apt-current", patient_id="patient-current", status="confirmed"),
            _appointment("apt-other-patient", patient_id="patient-other", status="confirmed"),
            _appointment("apt-completed", patient_id="patient-current", status="completed"),
            _appointment("apt-cancelled", patient_id="patient-current", status="cancelled"),
        ]
    )
    assert bundle.verification.appointment_match_count == 1
    appointments = bundle.results[0].payload["appointments"]
    assert isinstance(appointments, list)
    assert [row["appointment_id"] for row in appointments] == ["apt-current"]


def test_grouped_visit_rows_count_as_one_logical_cancellation_target() -> None:
    bundle = _execute(
        [
            _appointment("apt-a", service_id="svc-a", visit_group_id="visit-1"),
            _appointment("apt-b", service_id="svc-b", visit_group_id="visit-1"),
        ]
    )
    assert bundle.verification.appointment_match_count == 1
    assert bundle.verification.verified_parameters["appointment_ids"] == ["apt-a", "apt-b"]
    assert bundle.verification.verified_parameters["visit_group_id"] == "visit-1"
