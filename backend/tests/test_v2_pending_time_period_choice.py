from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.active_task_progress import (
    plan_active_task_progress,
    prepare_active_task_clock_progress,
)
from app.services.agent_v2.planner import PlannerContext, PlanStep, plan_turn
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    GroupedBookingState,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state
from app.services.agent_v2.state_rules import apply_booking_date_change

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
DATE = "2026-10-15"
SERVICE = "service-1"
BRANCH = "branch-1"


def _context():
    return build_semantic_context(
        {
            "services": [
                {
                    "id": SERVICE,
                    "name": "Consultation",
                    "duration_minutes": 30,
                    "requires_laser_device": False,
                }
            ],
            "branches": [
                {
                    "id": BRANCH,
                    "name": "Clinic A",
                    "working_hours": [
                        {"weekday": weekday, "start": "09:00", "end": "23:30"}
                        for weekday in range(7)
                    ],
                }
            ],
        }
    )


def _auth(operation: str) -> WriteAuthorization:
    return WriteAuthorization(
        operation=operation,
        authorized=True,
        source_turn_id="source-turn",
        granted_at=NOW,
    )


def _constraints(clock: str) -> CustomerConstraints:
    return CustomerConstraints(
        service_id=SERVICE,
        branch_id=BRANCH,
        date=DateConstraint(mode="exact", start_date=DATE),
        time=TimeConstraint(
            mode="exact",
            start_time=clock,
            start_time_ambiguity="twelve_hour",
        ),
        time_clock_source="bare_spoken_12h",
    )


def _booking(clock: str = "10:00") -> BookingTaskState:
    return BookingTaskState(
        write_authorization=_auth("booking"),
        constraints=_constraints(clock),
        requested_service_ids=[SERVICE],
        service_device_keys={SERVICE: "device-1"},
    )


def _reschedule(clock: str = "10:00") -> RescheduleTaskState:
    return RescheduleTaskState(
        write_authorization=_auth("reschedule"),
        target=RescheduleTarget(
            appointment_id="appointment-1",
            service_id=SERVICE,
            doctor_id="doctor-1",
            device_key="device-1",
        ),
        replacement=_constraints(clock).model_copy(
            update={"doctor_id": "doctor-1", "device_key": "device-1"}
        ),
    )


def _period_pending(state: BookingTaskState | RescheduleTaskState):
    context = _context()
    gate = plan_active_task_progress(state, operation_index=0, context=context)
    assert gate.clarification_field == "time"
    assert gate.facts["clock_period_clarification"] is True
    transition = apply_step_state(
        state,
        step=gate,
        operation=TurnOperation(
            type="continue_active",
            entities=TurnEntities(),
            execution_intent="execute",
        ),
        reads=None,
        now=NOW,
        turn_id="period-clarify",
    )
    pending = transition.active_task
    assert pending is not None
    assert pending.option_snapshot is not None
    assert pending.option_snapshot.purpose == "time_period"
    assert pending.option_snapshot.is_current(task_version=pending.version, now=NOW)
    return pending, context


def _select(
    state: BookingTaskState | RescheduleTaskState,
    *,
    selection: Selection,
):
    context = _context()
    operation = TurnOperation(
        type="select_active",
        entities=TurnEntities(),
        selection=selection,
        continues_previous=True,
        execution_intent="execute",
    )
    plan = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(
            semantic_context=context,
            active_task=state,
            now=NOW,
        ),
    )
    assert len(plan.steps) == 1
    return operation, plan.steps[0], context


def _apply_selection(state, operation, step):
    return apply_step_state(
        state,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="period-selection",
    ).active_task


def _time_state(state: BookingTaskState | RescheduleTaskState):
    return state.constraints if isinstance(state, BookingTaskState) else state.replacement


def test_pc1_booking_night_selection_binds_server_owned_pm_candidate() -> None:
    original = _booking("10:00")
    pending, _ = _period_pending(original)
    snapshot = pending.option_snapshot
    assert snapshot is not None
    assert [option.payload["canonical_time"] for option in snapshot.options] == ["10:00", "22:00"]
    assert [option.payload["period"] for option in snapshot.options] == ["am", "pm"]

    operation, step, context = _select(
        pending,
        selection=Selection(kind="ref", ref=snapshot.options[1].ref),
    )
    assert step.state_action == "update_active"
    assert step.facts["verified_time_period_selection"] is True
    resolved = _apply_selection(pending, operation, step)
    assert isinstance(resolved, BookingTaskState)
    assert resolved.constraints.time is not None
    assert resolved.constraints.time.start_time == "22:00"
    assert resolved.constraints.time.start_time_ambiguity == "none"
    assert resolved.constraints.time_clock_source == "explicit_period"
    assert resolved.option_snapshot is None
    assert resolved.constraints.service_id == original.constraints.service_id
    assert resolved.constraints.branch_id == original.constraints.branch_id
    assert resolved.constraints.date == original.constraints.date
    assert resolved.requested_service_ids == original.requested_service_ids
    assert resolved.service_device_keys == original.service_device_keys
    assert resolved.write_authorization == original.write_authorization

    next_step = plan_active_task_progress(resolved, operation_index=0, context=context)
    availability = [read for read in next_step.reads if read.kind == "availability"]
    assert availability
    assert availability[0].parameters["time"]["start_time"] == "22:00"


def test_pc2_booking_morning_selection_binds_server_owned_am_candidate() -> None:
    pending, _ = _period_pending(_booking("10:00"))
    snapshot = pending.option_snapshot
    assert snapshot is not None
    operation, step, _ = _select(
        pending,
        selection=Selection(kind="ref", ref=snapshot.options[0].ref),
    )
    resolved = _apply_selection(pending, operation, step)
    assert isinstance(resolved, BookingTaskState)
    assert resolved.constraints.time is not None
    assert resolved.constraints.time.start_time == "10:00"
    assert resolved.constraints.time.start_time_ambiguity == "none"
    assert resolved.constraints.time_clock_source == "explicit_period"


def test_pc3_eleven_pm_binds_23() -> None:
    pending, _ = _period_pending(_booking("11:00"))
    snapshot = pending.option_snapshot
    assert snapshot is not None
    assert [option.payload["canonical_time"] for option in snapshot.options] == ["11:00", "23:00"]
    operation, step, _ = _select(
        pending,
        selection=Selection(kind="index", index=2),
    )
    resolved = _apply_selection(pending, operation, step)
    assert isinstance(resolved, BookingTaskState)
    assert resolved.constraints.time is not None
    assert resolved.constraints.time.start_time == "23:00"


def test_pc4_eleven_am_binds_11() -> None:
    pending, _ = _period_pending(_booking("11:00"))
    operation, step, _ = _select(
        pending,
        selection=Selection(kind="index", index=1),
    )
    resolved = _apply_selection(pending, operation, step)
    assert isinstance(resolved, BookingTaskState)
    assert resolved.constraints.time is not None
    assert resolved.constraints.time.start_time == "11:00"


def test_pc5_non_answer_preserves_pending_choice_and_write_barrier() -> None:
    pending, context = _period_pending(_booking("10:00"))
    before = pending.model_dump(mode="json")
    side = PlanStep(
        operation_index=0,
        operation_type="social",
        disposition="respond",
        state_action="none",
        response_goal="social_ack",
    )
    transition = apply_step_state(
        pending,
        step=side,
        operation=TurnOperation(type="social", entities=TurnEntities(), execution_intent="informational"),
        reads=None,
        now=NOW,
        turn_id="ack",
    )
    assert transition.active_task is not None
    assert transition.active_task.model_dump(mode="json") == before
    gate = plan_active_task_progress(transition.active_task, operation_index=0, context=context)
    assert all(read.kind != "availability" for read in gate.reads)
    assert gate.write_intent is None


def test_pc6_side_question_preserves_choice_then_later_selection_resolves() -> None:
    pending, _ = _period_pending(_booking("10:00"))
    snapshot_id = pending.option_snapshot.snapshot_id if pending.option_snapshot else None
    side = PlanStep(
        operation_index=0,
        operation_type="service_info",
        disposition="respond",
        state_action="none",
        response_goal="answer_service",
    )
    preserved = apply_step_state(
        pending,
        step=side,
        operation=TurnOperation(
            type="service_info",
            entities=TurnEntities(),
            execution_intent="informational",
        ),
        reads=None,
        now=NOW,
        turn_id="side-question",
    ).active_task
    assert isinstance(preserved, BookingTaskState)
    assert preserved.option_snapshot is not None
    assert preserved.option_snapshot.snapshot_id == snapshot_id
    operation, step, _ = _select(
        preserved,
        selection=Selection(kind="time", time="22:00"),
    )
    resolved = _apply_selection(preserved, operation, step)
    assert isinstance(resolved, BookingTaskState)
    assert resolved.constraints.time is not None
    assert resolved.constraints.time.start_time == "22:00"


def test_pc7_changed_clock_invalidates_old_pending_choice() -> None:
    pending, _ = _period_pending(_booking("10:00"))
    old_ref = pending.option_snapshot.options[1].ref if pending.option_snapshot else "period-2"
    correction = TurnOperation(
        type="continue_active",
        entities=TurnEntities(
            time=TimeConstraint(mode="exact", start_time="16:00", start_time_ambiguity="none")
        ),
        clock_source="explicit_period",
        time_period_provenance="explicit_pm",
        active_task_relationship="continue",
        active_task_explicit_fields=["time"],
        execution_intent="execute",
    )
    update = PlanStep(
        operation_index=0,
        operation_type="continue_active",
        disposition="respond",
        state_action="update_active",
        response_goal="clarification",
        facts={
            "time": correction.entities.time.model_dump(mode="json"),
            "time_clock_source": "explicit_period",
        },
    )
    corrected = apply_step_state(
        pending,
        step=update,
        operation=correction,
        reads=None,
        now=NOW,
        turn_id="correction",
    ).active_task
    assert isinstance(corrected, BookingTaskState)
    assert corrected.option_snapshot is None
    assert corrected.constraints.time is not None
    assert corrected.constraints.time.start_time == "16:00"
    operation, stale_step, _ = _select(
        corrected,
        selection=Selection(kind="ref", ref=old_ref),
    )
    assert operation.type == "select_active"
    assert stale_step.disposition == "clarify"
    assert stale_step.write_intent is None


def test_pc8_selection_cannot_fabricate_candidate() -> None:
    pending, _ = _period_pending(_booking("10:00"))
    _, step, _ = _select(
        pending,
        selection=Selection(kind="time", time="18:00"),
    )
    assert step.disposition == "clarify"
    assert step.write_intent is None
    assert step.facts.get("verified_time_period_selection") is not True


def test_pc9_stale_choice_cannot_apply_after_task_version_change() -> None:
    pending, _ = _period_pending(_booking("10:00"))
    assert pending.option_snapshot is not None
    old_ref = pending.option_snapshot.options[1].ref
    changed = apply_booking_date_change(
        pending,
        date=DateConstraint(mode="exact", start_date="2026-10-16"),
    )
    assert changed.version > pending.version
    assert changed.option_snapshot is None
    _, step, _ = _select(changed, selection=Selection(kind="ref", ref=old_ref))
    assert step.disposition == "clarify"
    assert step.write_intent is None


def test_pc10_serialization_round_trip_then_night_selection() -> None:
    pending, _ = _period_pending(_booking("10:00"))
    restored = BookingTaskState.model_validate(pending.model_dump(mode="json"))
    assert restored.option_snapshot is not None
    assert restored.option_snapshot.purpose == "time_period"
    operation, step, _ = _select(
        restored,
        selection=Selection(kind="ref", ref=restored.option_snapshot.options[1].ref),
    )
    resolved = _apply_selection(restored, operation, step)
    assert isinstance(resolved, BookingTaskState)
    assert resolved.constraints.time is not None
    assert resolved.constraints.time.start_time == "22:00"
    assert resolved.constraints.time.start_time_ambiguity == "none"


def test_pc11_reschedule_period_selection_preserves_same_target() -> None:
    original = _reschedule("10:00")
    pending, context = _period_pending(original)
    assert isinstance(pending, RescheduleTaskState)
    assert pending.option_snapshot is not None
    operation, step, _ = _select(
        pending,
        selection=Selection(kind="ref", ref=pending.option_snapshot.options[1].ref),
    )
    resolved = _apply_selection(pending, operation, step)
    assert isinstance(resolved, RescheduleTaskState)
    assert resolved.target == original.target
    assert resolved.write_authorization == original.write_authorization
    assert resolved.replacement.time is not None
    assert resolved.replacement.time.start_time == "22:00"
    assert resolved.replacement.time.start_time_ambiguity == "none"
    assert resolved.replacement.time_clock_source == "explicit_period"
    assert resolved.option_snapshot is None
    next_step = plan_active_task_progress(resolved, operation_index=0, context=context)
    availability = [read for read in next_step.reads if read.kind == "availability"]
    assert availability
    assert availability[0].parameters["time"]["start_time"] == "22:00"


@pytest.mark.parametrize(
    ("clock", "source"),
    [
        ("03:00", "literal_24h"),
        ("16:15", "literal_24h"),
        ("16:15", "explicit_period"),
        ("23:00", "explicit_period"),
        ("12:00", "explicit_period"),
        ("00:00", "explicit_period"),
    ],
)
def test_pc12_explicit_times_never_create_period_choice(clock: str, source: str) -> None:
    state = _booking("10:00").model_copy(
        update={
            "constraints": _booking("10:00").constraints.model_copy(
                update={
                    "time": TimeConstraint(
                        mode="exact",
                        start_time=clock,
                        start_time_ambiguity="none",
                    ),
                    "time_clock_source": source,
                }
            )
        }
    )
    prepared, gate = prepare_active_task_clock_progress(
        state,
        operation_index=0,
        context=_context(),
    )
    assert gate is None
    assert prepared.option_snapshot is None
    assert prepared.constraints.time is not None
    assert prepared.constraints.time.start_time == clock


def test_period_selection_keeps_grouped_service_set_and_syncs_clock() -> None:
    base = _booking("10:00")
    second = base.constraints.model_copy(update={"service_id": "service-2"})
    grouped = base.model_copy(
        update={
            "requested_service_ids": [SERVICE, "service-2"],
            "service_device_keys": {SERVICE: "device-1", "service-2": "device-1"},
            "grouped": GroupedBookingState(components=[base.constraints, second]),
        }
    )
    pending, _ = _period_pending(grouped)
    assert isinstance(pending, BookingTaskState)
    assert pending.option_snapshot is not None
    operation, step, _ = _select(
        pending,
        selection=Selection(kind="ref", ref=pending.option_snapshot.options[1].ref),
    )
    resolved = _apply_selection(pending, operation, step)
    assert isinstance(resolved, BookingTaskState)
    assert resolved.requested_service_ids == [SERVICE, "service-2"]
    assert resolved.grouped is not None
    assert [component.service_id for component in resolved.grouped.components] == [SERVICE, "service-2"]
    assert all(
        component.time is not None
        and component.time.start_time == "22:00"
        and component.time.start_time_ambiguity == "none"
        and component.time_clock_source == "explicit_period"
        for component in resolved.grouped.components
    )
