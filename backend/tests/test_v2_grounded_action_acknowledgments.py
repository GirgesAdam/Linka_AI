from __future__ import annotations

from uuid import uuid4

import pytest

from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.orchestrator import (
    _booking_acknowledgment_step,
    _canonical_recent_booking_is_current,
    _canonical_recent_reschedule_is_current,
    _completed_action_context,
    _normalize_recent_action_acknowledgments,
    _recent_booking_validation_request,
    _recent_reschedule_validation_request,
    _reschedule_acknowledgment_step,
)
from app.services.agent_v2.planner import (
    PlanStep,
    ReadRequest,
    TurnPlan,
    VerificationFacts,
    WriteIntent,
)
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult


def _booking_step(
    *,
    service_id: str = "service-1",
    doctor_id: str = "doctor-1",
    device_key: str | None = "candela_gentle",
    date: str = "2026-09-25",
    time: str = "14:00",
    package_usage: str = "unspecified",
) -> PlanStep:
    facts: dict[str, object] = {
        "service_id": service_id,
        "doctor_id": doctor_id,
        "date": {"mode": "exact", "start_date": date, "end_date": None},
        "time": {
            "mode": "exact",
            "start_time": time,
            "end_time": None,
            "start_time_ambiguity": "none",
            "end_time_ambiguity": "none",
        },
        "package_usage": package_usage,
        "exact_time_requested": True,
    }
    if device_key is not None:
        facts["device_key"] = device_key
    return PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        reads=[ReadRequest(kind="availability", parameters=dict(facts))],
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters=dict(facts),
        ),
        response_goal="present_availability",
        facts=facts,
    )


def _booking_operation(*, continues_previous: bool = True) -> TurnOperation:
    return TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(ref="S1"),
            doctor=EntityReference(ref="D1"),
            device=EntityReference(ref="V1"),
            date=DateConstraint(mode="exact", start_date="2026-09-25"),
            time=TimeConstraint(mode="exact", start_time="14:00"),
        ),
        execution_intent="execute",
        continues_previous=continues_previous,
    )


def _recent_booking(**updates: object) -> dict[str, object]:
    base: dict[str, object] = {
        "operation_type": "book",
        "appointment_id": "appointment-1",
        "service_id": "service-1",
        "doctor_id": "doctor-1",
        "device_key": "candela_gentle",
        "start_at": "2026-09-25T11:00:00+00:00",
        "status": "confirmed",
        "package_usage": "unspecified",
    }
    base.update(updates)
    return base


def test_completed_booking_context_uses_verified_write_parameters() -> None:
    step = _booking_step().model_copy(
        update={
            "disposition": "write_ready",
            "write_intent": WriteIntent(
                kind="booking",
                authorized=True,
                parameters={
                    **_booking_step().facts,
                    "start_at": "2026-09-25T11:00:00+00:00",
                    "branch_id": "branch-1",
                },
            ),
        }
    )

    context = _completed_action_context(
        step,
        {
            "ok": True,
            "appointment_id": "appointment-1",
            "status": "confirmed",
        },
    )

    assert context is not None
    assert context["operation_type"] == "book"
    assert context["appointment_id"] == "appointment-1"
    assert context["service_id"] == "service-1"
    assert context["doctor_id"] == "doctor-1"
    assert context["device_key"] == "candela_gentle"
    assert context["start_at"] == "2026-09-25T11:00:00+00:00"
    assert context["status"] == "confirmed"


def test_completed_cancellation_context_requires_verified_success() -> None:
    step = PlanStep(
        operation_index=0,
        operation_type="cancel_appointment",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="cancel_appointment",
            authorized=True,
            parameters={"appointment_id": "appointment-1"},
        ),
        response_goal="cancellation_completed",
    )

    assert _completed_action_context(
        step,
        {"ok": True, "appointment_id": "appointment-1", "status": "cancelled"},
    ) == {
        "operation_type": "cancel_appointment",
        "appointment_id": "appointment-1",
        "status": "cancelled",
    }
    assert _completed_action_context(
        step,
        {"ok": False, "appointment_id": "appointment-1", "status": "cancelled"},
    ) is None


def _booking_validation_bundle(
    *,
    status: str = "confirmed",
    appointment_id: str = "appointment-1",
    service_id: str = "service-1",
    doctor_id: str = "doctor-1",
    device_key: str | None = "candela_gentle",
    start_at: str = "2026-09-25T11:00:00+00:00",
    match_count: int = 1,
) -> ReadExecutionBundle:
    return ReadExecutionBundle(
        results=[
            ReadResult(
                kind="appointments",
                ok=True,
                payload={
                    "appointments": [
                        {
                            "appointment_id": appointment_id,
                            "status": status,
                            "service_id": service_id,
                            "doctor_id": doctor_id,
                            "laser_device_key": device_key,
                            "start_at": start_at,
                        }
                    ]
                    if match_count
                    else [],
                },
            )
        ],
        verification=VerificationFacts(appointment_match_count=match_count),
    )


def test_same_effective_booking_waits_for_canonical_revalidation() -> None:
    turn = TiaTurnUnderstanding(operations=[_booking_operation()])
    plan = TurnPlan(steps=[_booking_step()])

    normalized = _normalize_recent_action_acknowledgments(
        plan,
        turn,
        recent_action=_recent_booking(),
        timezone_name="Africa/Cairo",
    )

    assert normalized == plan
    request = _recent_booking_validation_request(
        plan.steps[0],
        _booking_operation(),
        _recent_booking(),
        timezone_name="Africa/Cairo",
    )
    assert request == ReadRequest(
        kind="appointments",
        parameters={"appointment_id": "appointment-1"},
    )


def test_active_recent_booking_acknowledges_without_duplicate_write() -> None:
    recent = _recent_booking()
    reads = _booking_validation_bundle()

    assert _canonical_recent_booking_is_current(recent, reads) is True
    step = _booking_acknowledgment_step(_booking_step())

    assert step.disposition == "respond"
    assert step.reads == []
    assert step.write_intent is None
    assert step.response_goal == "social_ack"
    assert step.facts == {
        "acknowledgment": {
            "action": "booking",
            "already_completed": True,
            "same_booking": True,
        }
    }


def test_external_cancellation_invalidates_recent_booking_acknowledgment() -> None:
    assert (
        _canonical_recent_booking_is_current(
            _recent_booking(),
            _booking_validation_bundle(match_count=0),
        )
        is False
    )


def test_completed_recent_booking_is_not_treated_as_active() -> None:
    assert (
        _canonical_recent_booking_is_current(
            _recent_booking(),
            _booking_validation_bundle(status="completed"),
        )
        is False
    )


def test_materially_changed_canonical_booking_is_not_acknowledged_as_stale_match() -> None:
    assert (
        _canonical_recent_booking_is_current(
            _recent_booking(),
            _booking_validation_bundle(start_at="2026-09-25T12:00:00+00:00"),
        )
        is False
    )


def test_explicit_new_booking_does_not_use_recent_booking_acknowledgment() -> None:
    request = _recent_booking_validation_request(
        _booking_step(),
        _booking_operation(continues_previous=False),
        _recent_booking(),
        timezone_name="Africa/Cairo",
    )

    assert request is None


def test_meaningful_customer_dimension_change_uses_normal_booking_flow() -> None:
    request = _recent_booking_validation_request(
        _booking_step(time="15:00"),
        _booking_operation(),
        _recent_booking(),
        timezone_name="Africa/Cairo",
    )

    assert request is None


@pytest.mark.parametrize(
    ("step_updates", "recent_updates"),
    [
        ({"service_id": "service-2"}, {}),
        ({"doctor_id": "doctor-2"}, {}),
        ({"device_key": "prime_lase"}, {}),
        ({"date": "2026-09-26"}, {}),
        ({"time": "15:00"}, {}),
        ({}, {"status": "cancelled"}),
    ],
)
def test_different_or_inactive_booking_is_not_treated_as_duplicate(
    step_updates: dict[str, object],
    recent_updates: dict[str, object],
) -> None:
    step_kwargs = dict(step_updates)
    plan = TurnPlan(steps=[_booking_step(**step_kwargs)])
    turn = TiaTurnUnderstanding(operations=[_booking_operation()])

    normalized = _normalize_recent_action_acknowledgments(
        plan,
        turn,
        recent_action=_recent_booking(**recent_updates),
        timezone_name="Africa/Cairo",
    )

    assert normalized == plan


def test_bare_immediate_cancellation_after_completed_cancel_is_acknowledged() -> None:
    operation = TurnOperation(
        type="cancel_appointment",
        entities=TurnEntities(),
        execution_intent="execute",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="cancel_appointment",
        disposition="read",
        reads=[ReadRequest(kind="appointments")],
        write_intent=WriteIntent(
            kind="cancel_appointment",
            authorized=True,
            parameters={},
        ),
        response_goal="cancellation_completed",
    )

    normalized = _normalize_recent_action_acknowledgments(
        TurnPlan(steps=[step]),
        TiaTurnUnderstanding(operations=[operation]),
        recent_action={
            "operation_type": "cancel_appointment",
            "appointment_id": "appointment-1",
            "status": "cancelled",
        },
        timezone_name="Africa/Cairo",
    )

    result = normalized.steps[0]
    assert result.disposition == "respond"
    assert result.reads == []
    assert result.write_intent is None
    assert result.facts["acknowledgment"] == {
        "action": "cancel_appointment",
        "already_completed": True,
    }


def test_different_explicit_cancellation_target_is_not_acknowledged() -> None:
    operation = TurnOperation(
        type="cancel_appointment",
        entities=TurnEntities(appointment=EntityReference(ref="A2")),
        execution_intent="execute",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="cancel_appointment",
        disposition="read",
        reads=[ReadRequest(kind="appointments", parameters={"appointment_id": "appointment-2"})],
        write_intent=WriteIntent(
            kind="cancel_appointment",
            authorized=True,
            parameters={"appointment_id": "appointment-2"},
        ),
        response_goal="cancellation_completed",
        facts={"appointment_id": "appointment-2"},
    )
    plan = TurnPlan(steps=[step])

    normalized = _normalize_recent_action_acknowledgments(
        plan,
        TiaTurnUnderstanding(operations=[operation]),
        recent_action={
            "operation_type": "cancel_appointment",
            "appointment_id": "appointment-1",
            "status": "cancelled",
        },
        timezone_name="Africa/Cairo",
    )

    assert normalized == plan


def test_different_cancellation_selection_is_not_acknowledged_as_already_completed() -> None:
    operation = TurnOperation(
        type="cancel_appointment",
        entities=TurnEntities(),
        selection=Selection(kind="index", index=2),
        execution_intent="execute",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="cancel_appointment",
        disposition="read",
        reads=[ReadRequest(kind="appointments")],
        write_intent=WriteIntent(
            kind="cancel_appointment",
            authorized=True,
            parameters={},
        ),
        response_goal="cancellation_completed",
    )
    plan = TurnPlan(steps=[step])

    normalized = _normalize_recent_action_acknowledgments(
        plan,
        TiaTurnUnderstanding(operations=[operation]),
        recent_action={
            "operation_type": "cancel_appointment",
            "appointment_id": "appointment-1",
            "status": "cancelled",
        },
        timezone_name="Africa/Cairo",
    )

    assert normalized == plan


def test_unrelated_social_followup_is_not_forced_into_completed_action_acknowledgment() -> None:
    operation = TurnOperation(type="social", entities=TurnEntities())
    step = PlanStep(
        operation_index=0,
        operation_type="social",
        disposition="respond",
        response_goal="social_ack",
    )
    plan = TurnPlan(steps=[step])

    normalized = _normalize_recent_action_acknowledgments(
        plan,
        TiaTurnUnderstanding(operations=[operation]),
        recent_action={
            "operation_type": "cancel_appointment",
            "appointment_id": "appointment-1",
            "status": "cancelled",
        },
        timezone_name="Africa/Cairo",
    )

    assert normalized == plan




def _reschedule_step(
    *,
    service_id: str = "service-1",
    doctor_id: str = "doctor-1",
    device_key: str | None = None,
    date: str = "2026-09-29",
    time: str = "10:00",
) -> PlanStep:
    facts: dict[str, object] = {
        "service_id": service_id,
        "doctor_id": doctor_id,
        "date": {"mode": "exact", "start_date": date, "end_date": None},
        "time": {
            "mode": "exact",
            "start_time": time,
            "end_time": None,
            "start_time_ambiguity": "none",
            "end_time_ambiguity": "none",
        },
        "source_appointment": {},
    }
    if device_key is not None:
        facts["device_key"] = device_key
    return PlanStep(
        operation_index=0,
        operation_type="reschedule",
        disposition="read",
        reads=[ReadRequest(kind="appointments"), ReadRequest(kind="availability")],
        write_intent=WriteIntent(
            kind="reschedule",
            authorized=True,
            parameters=dict(facts),
        ),
        state_action="start_reschedule",
        response_goal="present_availability",
        facts=facts,
    )


def _reschedule_operation(
    *,
    date: str = "2026-09-29",
    time: str = "10:00",
    appointment: EntityReference | None = None,
) -> TurnOperation:
    return TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            appointment=appointment,
            date=DateConstraint(mode="exact", start_date=date),
            time=TimeConstraint(mode="exact", start_time=time),
        ),
        execution_intent="execute",
    )


def _recent_reschedule(**updates: object) -> dict[str, object]:
    base: dict[str, object] = {
        "operation_type": "reschedule",
        "appointment_id": "replacement-1",
        "previous_appointment_id": "source-1",
        "service_id": "service-1",
        "doctor_id": "doctor-1",
        "start_at": "2026-09-29T07:00:00+00:00",
        "status": "confirmed",
    }
    base.update(updates)
    return base


def test_completed_reschedule_context_uses_replacement_identity() -> None:
    step = _reschedule_step().model_copy(
        update={
            "disposition": "write_ready",
            "write_intent": WriteIntent(
                kind="reschedule",
                authorized=True,
                parameters={
                    **_reschedule_step().facts,
                    "appointment_id": "source-1",
                    "branch_id": "branch-1",
                    "start_at": "2026-09-29T07:00:00+00:00",
                },
            ),
        }
    )

    context = _completed_action_context(
        step,
        {
            "ok": True,
            "appointment_id": "replacement-1",
            "previous_appointment_id": "source-1",
            "status": "confirmed",
        },
    )

    assert context == {
        "operation_type": "reschedule",
        "appointment_id": "replacement-1",
        "previous_appointment_id": "source-1",
        "service_id": "service-1",
        "doctor_id": "doctor-1",
        "start_at": "2026-09-29T07:00:00+00:00",
        "status": "confirmed",
        "date": {"mode": "exact", "start_date": "2026-09-29", "end_date": None},
        "time": {
            "mode": "exact",
            "start_time": "10:00",
            "end_time": None,
            "start_time_ambiguity": "none",
            "end_time_ambiguity": "none",
        },
    }


def test_completed_reschedule_bare_confirmation_waits_for_canonical_read() -> None:
    step = _reschedule_step()
    operation = _reschedule_operation()
    request = _recent_reschedule_validation_request(
        step,
        operation,
        _recent_reschedule(),
        timezone_name="Africa/Cairo",
    )

    assert request == ReadRequest(
        kind="appointments",
        parameters={"appointment_id": "replacement-1"},
    )


@pytest.mark.parametrize(
    ("date", "time"),
    [("2026-09-30", "10:00"), ("2026-09-29", "12:00")],
)
def test_completed_reschedule_explicit_date_or_time_change_is_not_deduped(
    date: str,
    time: str,
) -> None:
    assert (
        _recent_reschedule_validation_request(
            _reschedule_step(date=date, time=time),
            _reschedule_operation(date=date, time=time),
            _recent_reschedule(),
            timezone_name="Africa/Cairo",
        )
        is None
    )


def test_completed_reschedule_explicit_target_is_not_deduped() -> None:
    assert (
        _recent_reschedule_validation_request(
            _reschedule_step(),
            _reschedule_operation(appointment=EntityReference(ref="A2")),
            _recent_reschedule(),
            timezone_name="Africa/Cairo",
        )
        is None
    )


def test_completed_reschedule_canonical_replacement_must_still_match() -> None:
    recent = _recent_reschedule()
    assert _canonical_recent_reschedule_is_current(
        recent,
        _booking_validation_bundle(
            appointment_id="replacement-1",
            service_id="service-1",
            doctor_id="doctor-1",
            device_key=None,
            start_at="2026-09-29T07:00:00+00:00",
        ),
    )
    assert not _canonical_recent_reschedule_is_current(
        recent,
        _booking_validation_bundle(match_count=0),
    )
    assert not _canonical_recent_reschedule_is_current(
        recent,
        _booking_validation_bundle(
            appointment_id="replacement-1",
            service_id="service-1",
            doctor_id="doctor-1",
            device_key=None,
            start_at="2026-09-29T08:00:00+00:00",
        ),
    )


def test_completed_reschedule_acknowledgment_has_no_write() -> None:
    step = _reschedule_acknowledgment_step(_reschedule_step())

    assert step.disposition == "respond"
    assert step.reads == []
    assert step.write_intent is None
    assert step.state_action == "none"
    assert step.response_goal == "social_ack"
    assert step.facts == {
        "acknowledgment": {
            "action": "reschedule",
            "already_completed": True,
        }
    }


def test_failed_or_unverified_write_never_creates_acknowledgment_context() -> None:
    step = _booking_step().model_copy(
        update={
            "disposition": "write_ready",
            "write_intent": WriteIntent(
                kind="booking",
                authorized=True,
                parameters={
                    **_booking_step().facts,
                    "start_at": "2026-09-25T11:00:00+00:00",
                },
            ),
        }
    )
    assert _completed_action_context(
        step,
        {
            "ok": False,
            "appointment_id": str(uuid4()),
            "status": "confirmed",
        },
    ) is None
