from __future__ import annotations

from datetime import UTC, datetime

from app.agents.v2.turn_contract import (
    DateConstraint,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.planner import PlanStep, ReadRequest, WriteIntent
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    OptionChoice,
    OptionSnapshot,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import (
    apply_step_state,
    complete_state_after_action,
)

NOW = datetime(2026, 9, 11, 18, 0, tzinfo=UTC)


def _operation(
    operation_type: str = "book",
    *,
    date: DateConstraint | None = None,
    time: TimeConstraint | None = None,
) -> TurnOperation:
    return TurnOperation(
        type=operation_type,
        entities=TurnEntities(date=date, time=time),
        package_usage="unspecified",
    )


def _booking_state(*, with_snapshot: bool = False) -> BookingTaskState:
    state = BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(
            service_id="svc-underarm",
            date=DateConstraint(mode="exact", start_date="2026-09-12"),
        ),
        derived=DerivedBookingState(),
    )
    if not with_snapshot:
        return state
    snapshot = OptionSnapshot(
        snapshot_id="snapshot-1",
        purpose="booking_slot",
        task_version=2,
        created_at=NOW,
        expires_at=datetime(2026, 9, 11, 18, 15, tzinfo=UTC),
        options=[
            OptionChoice(
                ref="slot-1",
                label="19:00 · د. مريم",
                payload={
                    "service_id": "svc-underarm",
                    "doctor_id": "doc-maryam",
                    "start_at": "2026-09-12T19:00:00+03:00",
                    "start_time_24h": "19:00",
                },
            )
        ],
    )
    return state.model_copy(
        update={
            "status": "awaiting_choice",
            "version": 2,
            "option_snapshot": snapshot,
            "derived": state.derived.model_copy(
                update={"availability_snapshot_id": "snapshot-1"}
            ),
        }
    )


def _availability_bundle() -> ReadExecutionBundle:
    return ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=True,
                payload={
                    "slots": [
                        {
                            "branch_id": "branch-main",
                            "service_id": "svc-underarm",
                            "service_name": "ليزر إبط",
                            "doctor_id": "doc-maryam",
                            "doctor_name": "د. مريم",
                            "laser_device_key": "candela_gentle",
                            "laser_device_name": "Candela Gentle",
                            "start_at": "2026-09-12T18:00:00+03:00",
                            "start_local": "2026-09-12T18:00:00+03:00",
                        },
                        {
                            "branch_id": "branch-main",
                            "service_id": "svc-underarm",
                            "service_name": "ليزر إبط",
                            "doctor_id": "doc-maryam",
                            "doctor_name": "د. مريم",
                            "laser_device_key": "candela_gentle",
                            "laser_device_name": "Candela Gentle",
                            "start_at": "2026-09-12T19:00:00+03:00",
                            "start_local": "2026-09-12T19:00:00+03:00",
                        },
                    ]
                },
            )
        ]
    )


def test_commercial_price_read_records_non_laser_basis_key() -> None:
    operation = _operation()
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        state_action="update_active",
        response_goal="answer_price",
        facts={
            "service_id": "svc-underarm",
            "commercial_basis_key": "svc-underarm|none|standalone",
        },
    )
    reads = ReadExecutionBundle(
        results=[ReadResult(kind="service_catalog", ok=True, payload={"service": {}})]
    )
    transition = apply_step_state(
        _booking_state(),
        step=step,
        operation=operation,
        reads=reads,
        now=NOW,
        turn_id="turn-price",
    )
    assert isinstance(transition.active_task, BookingTaskState)
    assert (
        transition.active_task.derived.commercial_basis_presented_key
        == "svc-underarm|none|standalone"
    )


def test_package_commercial_read_records_basis_only_for_usable_coverage() -> None:
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        package_usage="use_existing",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        state_action="update_active",
        response_goal="package_information",
        facts={
            "service_id": "svc-underarm",
            "package_usage": "use_existing",
            "commercial_basis_key": "svc-underarm|none|package",
        },
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="customer_packages",
                ok=True,
                payload={
                    "packages": [
                        {
                            "effective_status": "active",
                            "sessions_remaining": 3,
                        }
                    ]
                },
            )
        ]
    )
    transition = apply_step_state(
        _booking_state().model_copy(
            update={
                "constraints": CustomerConstraints(
                    service_id="svc-underarm",
                    package_usage="use_existing",
                )
            }
        ),
        step=step,
        operation=operation,
        reads=reads,
        now=NOW,
        turn_id="turn-package-truth",
    )
    assert isinstance(transition.active_task, BookingTaskState)
    assert (
        transition.active_task.derived.commercial_basis_presented_key
        == "svc-underarm|none|package"
    )


def test_start_booking_persists_authorization_and_known_constraints() -> None:
    operation = _operation(date=DateConstraint(mode="exact", start_date="2026-09-12"))
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        state_action="start_booking",
        reads=[ReadRequest(kind="availability")],
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters={"service_id": "svc-underarm"},
        ),
        facts={
            "service_id": "svc-underarm",
            "date": {"mode": "exact", "start_date": "2026-09-12", "end_date": None},
        },
    )

    transition = apply_step_state(
        None,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-book",
    )

    state = transition.active_task
    assert isinstance(state, BookingTaskState)
    assert state.constraints.service_id == "svc-underarm"
    assert state.constraints.date == operation.entities.date
    assert state.write_authorization.authorized is True
    assert state.write_authorization.source_turn_id == "turn-book"
    assert transition.changed is True


def test_broad_booking_availability_becomes_current_option_snapshot() -> None:
    operation = _operation(date=DateConstraint(mode="exact", start_date="2026-09-12"))
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        state_action="start_booking",
        reads=[ReadRequest(kind="availability")],
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters={"service_id": "svc-underarm"},
        ),
        facts={
            "service_id": "svc-underarm",
            "date": {"mode": "exact", "start_date": "2026-09-12", "end_date": None},
            "exact_time_requested": False,
        },
    )

    transition = apply_step_state(
        None,
        step=step,
        operation=operation,
        reads=_availability_bundle(),
        now=NOW,
        turn_id="turn-slots",
    )

    state = transition.active_task
    assert isinstance(state, BookingTaskState)
    assert state.status == "awaiting_choice"
    assert state.option_snapshot is not None
    assert state.option_snapshot.task_version == state.version
    assert len(state.option_snapshot.options) == 2
    assert state.option_snapshot.options[0].payload["start_time_24h"] == "18:00"
    assert state.option_snapshot.options[0].payload["device_key"] == "candela_gentle"
    assert state.derived.availability_snapshot_id == state.option_snapshot.snapshot_id


def test_active_constraint_change_invalidates_stale_snapshot() -> None:
    state = _booking_state(with_snapshot=True)
    operation = _operation(
        operation_type="continue_active",
        time=TimeConstraint(mode="after", start_time="20:00"),
    )
    step = PlanStep(
        operation_index=0,
        operation_type="continue_active",
        disposition="state_update",
        state_action="update_active",
        facts={
            "time": {
                "mode": "after",
                "start_time": "20:00",
                "end_time": None,
                "start_time_ambiguity": "none",
                "end_time_ambiguity": "none",
            }
        },
    )

    transition = apply_step_state(
        state,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-change",
    )

    updated = transition.active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time == operation.entities.time
    assert updated.option_snapshot is None
    assert updated.derived.availability_snapshot_id is None
    assert updated.version > state.version


def test_informational_side_read_preserves_active_task_exactly() -> None:
    state = _booking_state(with_snapshot=True)
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        state_action="none",
        reads=[ReadRequest(kind="service_catalog")],
    )

    transition = apply_step_state(
        state,
        step=step,
        operation=_operation(operation_type="pricing"),
        reads=None,
        now=NOW,
        turn_id="turn-side-read",
    )

    assert transition.active_task == state
    assert transition.changed is False
    assert transition.reason == "side_read_preserved"


def test_cancel_active_clears_conversation_task_without_appointment_write() -> None:
    state = _booking_state()
    step = PlanStep(
        operation_index=0,
        operation_type="cancel_active",
        disposition="state_update",
        state_action="cancel_active",
    )

    transition = apply_step_state(
        state,
        step=step,
        operation=_operation(operation_type="cancel_active"),
        reads=None,
        now=NOW,
        turn_id="turn-cancel",
    )

    assert transition.active_task is None
    assert transition.changed is True
    assert step.write_intent is None


def test_successful_booking_clears_task_but_failed_write_preserves_it() -> None:
    state = _booking_state()
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        state_action="start_booking",
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters={"service_id": "svc-underarm"},
        ),
    )

    failed = complete_state_after_action(state, step=step, action_result={"ok": False})
    succeeded = complete_state_after_action(state, step=step, action_result={"ok": True})

    assert failed.active_task == state
    assert failed.changed is False
    assert succeeded.active_task is None
    assert succeeded.changed is True


def test_reschedule_state_requires_exactly_one_verified_target() -> None:
    operation = _operation(
        operation_type="reschedule",
        date=DateConstraint(mode="exact", start_date="2026-09-13"),
    )
    step = PlanStep(
        operation_index=0,
        operation_type="reschedule",
        disposition="read",
        state_action="start_reschedule",
        reads=[ReadRequest(kind="appointments"), ReadRequest(kind="availability")],
        write_intent=WriteIntent(kind="reschedule", authorized=True, parameters={}),
        facts={
            "date": {"mode": "exact", "start_date": "2026-09-13", "end_date": None},
        },
    )
    ambiguous_reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="appointments",
                ok=True,
                payload={
                    "appointments": [
                        {"appointment_id": "apt-1", "service_id": "svc-underarm"},
                        {"appointment_id": "apt-2", "service_id": "svc-underarm"},
                    ]
                },
            )
        ]
    )
    unique_reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="appointments",
                ok=True,
                payload={
                    "appointments": [
                        {
                            "appointment_id": "apt-1",
                            "service_id": "svc-underarm",
                            "doctor_id": "doc-maryam",
                            "laser_device_key": "candela_gentle",
                            "start_local": "2026-09-12T19:00:00+03:00",
                        }
                    ]
                },
            )
        ]
    )

    ambiguous = apply_step_state(
        None,
        step=step,
        operation=operation,
        reads=ambiguous_reads,
        now=NOW,
        turn_id="turn-reschedule",
    )
    unique = apply_step_state(
        None,
        step=step,
        operation=operation,
        reads=unique_reads,
        now=NOW,
        turn_id="turn-reschedule",
    )

    assert ambiguous.active_task is None
    assert ambiguous.reason == "reschedule_target_unverified"
    assert unique.active_task is not None
    assert unique.active_task.task_type == "reschedule"
    assert unique.active_task.target.appointment_id == "apt-1"
    assert unique.active_task.replacement.date == operation.entities.date

def test_new_booking_snapshot_never_persists_pulse_billing_choice() -> None:
    operation = _operation(date=DateConstraint(mode="exact", start_date="2026-09-12"))
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        state_action="start_booking",
        reads=[ReadRequest(kind="availability")],
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters={"service_id": "svc-underarm"},
        ),
        facts={
            "service_id": "svc-underarm",
            "date": {"mode": "exact", "start_date": "2026-09-12", "end_date": None},
            "exact_time_requested": False,
        },
    )

    transition = apply_step_state(
        None,
        step=step,
        operation=operation,
        reads=_availability_bundle(),
        now=NOW,
        turn_id="turn-no-pulse-billing",
    )

    state = transition.active_task
    assert isinstance(state, BookingTaskState)
    assert state.constraints.pulse_usage == "unspecified"
    assert state.option_snapshot is not None
    assert all(
        "pulse_usage" not in option.payload for option in state.option_snapshot.options
    )
