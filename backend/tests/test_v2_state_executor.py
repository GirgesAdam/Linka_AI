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
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import (
    _promote_booking_authorization_for_explicit_continuation,
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
                label="19:00 Â· Ø¯. Ù…Ø±ÙŠÙ…",
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
                            "service_name": "Ù„ÙŠØ²Ø± Ø¥Ø¨Ø·",
                            "doctor_id": "doc-maryam",
                            "doctor_name": "Ø¯. Ù…Ø±ÙŠÙ…",
                            "laser_device_key": "candela_gentle",
                            "laser_device_name": "Candela Gentle",
                            "start_at": "2026-09-12T18:00:00+03:00",
                            "start_local": "2026-09-12T18:00:00+03:00",
                        },
                        {
                            "branch_id": "branch-main",
                            "service_id": "svc-underarm",
                            "service_name": "Ù„ÙŠØ²Ø± Ø¥Ø¨Ø·",
                            "doctor_id": "doc-maryam",
                            "doctor_name": "Ø¯. Ù…Ø±ÙŠÙ…",
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


def test_start_booking_preserves_unresolved_10_11_hint_after_planner_serialization() -> None:
    semantic_time = TimeConstraint(
        mode="exact",
        start_time="10:30",
        start_time_ambiguity="twelve_hour",
        spoken_period_hint="pm",
    )
    operation = _operation(time=semantic_time)
    serialized_time = semantic_time.model_dump(mode="json")
    assert "spoken_period_hint" not in serialized_time
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="clarify",
        state_action="start_booking",
        response_goal="clarification",
        clarification_field="date",
        facts={
            "service_id": "svc-underarm",
            "time": serialized_time,
            "time_clock_source": "bare_spoken_12h",
        },
    )

    transition = apply_step_state(
        None,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-unresolved-period",
    )

    state = transition.active_task
    assert isinstance(state, BookingTaskState)
    assert state.constraints.time is not None
    assert state.constraints.time.start_time == "10:30"
    assert state.constraints.time.start_time_ambiguity == "twelve_hour"
    assert state.constraints.time.spoken_period_hint == "pm"


def test_resolved_planner_time_does_not_retain_semantic_period_hint() -> None:
    semantic_time = TimeConstraint(
        mode="exact",
        start_time="10:30",
        start_time_ambiguity="twelve_hour",
        spoken_period_hint="pm",
    )
    operation = _operation(time=semantic_time)
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        state_action="start_booking",
        facts={
            "service_id": "svc-underarm",
            "time": {
                "mode": "exact",
                "start_time": "22:30",
                "end_time": None,
                "start_time_ambiguity": "none",
                "end_time_ambiguity": "none",
            },
            "time_clock_source": "bare_spoken_12h",
        },
    )

    transition = apply_step_state(
        None,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-resolved-period",
    )

    state = transition.active_task
    assert isinstance(state, BookingTaskState)
    assert state.constraints.time is not None
    assert state.constraints.time.start_time == "22:30"
    assert state.constraints.time.start_time_ambiguity == "none"
    assert state.constraints.time.spoken_period_hint == "none"

def test_start_reschedule_preserves_unresolved_10_11_hint_after_planner_serialization() -> None:
    semantic_time = TimeConstraint(
        mode="exact",
        start_time="10:30",
        start_time_ambiguity="twelve_hour",
        spoken_period_hint="pm",
    )
    operation = _operation(operation_type="reschedule", time=semantic_time)
    serialized_time = semantic_time.model_dump(mode="json")
    step = PlanStep(
        operation_index=0,
        operation_type="reschedule",
        disposition="clarify",
        state_action="start_reschedule",
        response_goal="clarification",
        clarification_field="date",
        reads=[ReadRequest(kind="appointments")],
        facts={
            "time": serialized_time,
            "time_clock_source": "bare_spoken_12h",
        },
    )
    reads = ReadExecutionBundle(
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
                            "start_local": "2026-09-14T19:00:00+03:00",
                        }
                    ]
                },
            )
        ]
    )

    transition = apply_step_state(
        None,
        step=step,
        operation=operation,
        reads=reads,
        now=NOW,
        turn_id="turn-reschedule-unresolved-period",
    )

    state = transition.active_task
    assert isinstance(state, RescheduleTaskState)
    assert state.target.appointment_id == "apt-1"
    assert state.replacement.time is not None
    assert state.replacement.time.start_time == "10:30"
    assert state.replacement.time.start_time_ambiguity == "twelve_hour"
    assert state.replacement.time.spoken_period_hint == "pm"


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
    assert state.status == "awaiting_slot"
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


def test_successful_direct_booking_proposal_clears_task_but_failed_write_preserves_it() -> None:
    state = _booking_state()
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        state_action="start_booking",
        write_intent=WriteIntent(
            kind="booking_proposal",
            authorized=True,
            parameters={"proposal_id": "proposal-1", "proposal": {}},
        ),
    )

    failed = complete_state_after_action(state, step=step, action_result={"ok": False})
    succeeded = complete_state_after_action(state, step=step, action_result={"ok": True})

    assert failed.active_task == state
    assert failed.changed is False
    assert succeeded.active_task is None
    assert succeeded.changed is True


def test_review_first_booking_starts_without_write_authorization() -> None:
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="informational",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        state_action="start_booking",
        reads=[ReadRequest(kind="availability", parameters={"service_id": "svc-underarm"})],
        response_goal="present_availability",
        facts={"service_id": "svc-underarm"},
    )

    transition = apply_step_state(
        None,
        step=step,
        operation=operation,
        reads=_availability_bundle(),
        now=NOW,
        turn_id="review-first-start",
    )

    assert isinstance(transition.active_task, BookingTaskState)
    assert transition.active_task.constraints.service_id == "svc-underarm"
    assert transition.active_task.write_authorization.authorized is False
    assert transition.active_task.write_authorization.source_turn_id is None
    assert transition.active_task.write_authorization.granted_at is None


def test_review_first_booking_continuation_revokes_write_authorization() -> None:
    state = _booking_state()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="informational",
        active_task_relationship="continue",
        continues_previous=True,
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        state_action="update_active",
        response_goal="present_availability",
        facts={},
    )

    transition = apply_step_state(
        state,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="review-first",
    )

    assert isinstance(transition.active_task, BookingTaskState)
    assert transition.active_task.write_authorization.authorized is False
    assert transition.active_task.write_authorization.source_turn_id is None
    assert transition.active_task.write_authorization.granted_at is None
    assert transition.active_task.constraints.service_id == state.constraints.service_id


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

def test_explicit_booking_continuation_promotes_read_only_booking_authority() -> None:
    state = _booking_state().model_copy(
        update={
            "requested_service_ids": ["svc-underarm", "svc-bikini"],
            "write_authorization": WriteAuthorization(
                operation="booking",
                authorized=False,
            ),
        }
    )
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-09-12"),
            time=TimeConstraint(mode="exact", start_time="18:00"),
        ),
        package_usage="unspecified",
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date", "time"],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        state_action="update_active",
        response_goal="answer_price",
        facts={
            "service_id": "svc-underarm",
            "requested_service_ids": ["svc-underarm", "svc-bikini"],
            "date": operation.entities.date.model_dump(mode="json"),
            "time": operation.entities.time.model_dump(mode="json"),
        },
    )

    transition = apply_step_state(
        state,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-explicit-book",
    )

    assert isinstance(transition.active_task, BookingTaskState)
    assert transition.active_task.write_authorization.authorized is True
    assert transition.active_task.write_authorization.source_turn_id == "turn-explicit-book"
    assert transition.active_task.write_authorization.granted_at == NOW
    assert transition.active_task.requested_service_ids == ["svc-underarm", "svc-bikini"]
    assert transition.active_task.constraints.date == operation.entities.date
    assert transition.active_task.constraints.time == operation.entities.time


def test_booking_authority_is_not_promoted_without_explicit_execute_continuation() -> None:
    state = _booking_state().model_copy(
        update={
            "write_authorization": WriteAuthorization(
                operation="booking",
                authorized=False,
            )
        }
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        state_action="update_active",
        response_goal="present_availability",
        facts={"service_id": "svc-underarm"},
    )
    informational = TurnOperation(
        type="book",
        entities=TurnEntities(),
        package_usage="unspecified",
        execution_intent="informational",
        active_task_relationship="continue",
    )
    unrelated_execute = TurnOperation(
        type="book",
        entities=TurnEntities(),
        package_usage="unspecified",
        execution_intent="execute",
        active_task_relationship="unspecified",
    )

    for turn_id, operation in (
        ("turn-informational", informational),
        ("turn-unrelated", unrelated_execute),
    ):
        transition = apply_step_state(
            state,
            step=step,
            operation=operation,
            reads=None,
            now=NOW,
            turn_id=turn_id,
        )
        assert isinstance(transition.active_task, BookingTaskState)
        assert transition.active_task.write_authorization.authorized is False
        assert transition.active_task.write_authorization.source_turn_id is None


def test_booking_date_only_update_preserves_inherited_period_authority_from_stripped_time_echo() -> None:
    state = _booking_state(with_snapshot=True)
    state = state.model_copy(
        update={
            "constraints": state.constraints.model_copy(
                update={
                    "date": None,
                    "time": TimeConstraint(
                        mode="exact",
                        start_time="10:30",
                        start_time_ambiguity="twelve_hour",
                        spoken_period_hint="pm",
                    ),
                    "time_clock_source": "bare_spoken_12h",
                }
            )
        }
    )
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-10-15"),
            time=None,
        ),
        package_usage="unspecified",
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date"],
        continues_previous=True,
    )
    stripped_echo = TimeConstraint(
        mode="exact",
        start_time="10:30",
        start_time_ambiguity="twelve_hour",
    ).model_dump(mode="json")
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts={
            "date": operation.entities.date.model_dump(mode="json"),
            "time": stripped_echo,
        },
    )

    transition = apply_step_state(
        state,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-date-only-with-time-echo",
    )

    updated = transition.active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.date == operation.entities.date
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "10:30"
    assert updated.constraints.time.start_time_ambiguity == "twelve_hour"
    assert updated.constraints.time.spoken_period_hint == "pm"
    assert updated.constraints.time_clock_source == "bare_spoken_12h"
    # Date is the only real mutation; the inherited stripped time echo must not
    # create another version increment or a second invalidation cycle.
    assert updated.version == state.version + 1
    assert updated.option_snapshot is None
    assert updated.derived.availability_snapshot_id is None



def _time_authority_booking_state(*, hint: str = "pm") -> BookingTaskState:
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-authority",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(
            service_id="svc-underarm",
            doctor_id="doc-a",
            branch_id="branch-a",
            device_key="device-a",
            date=DateConstraint(mode="exact", start_date="2026-10-15"),
            time=TimeConstraint(
                mode="exact",
                start_time="10:30",
                start_time_ambiguity="twelve_hour",
                spoken_period_hint=hint,
            ),
            time_clock_source="bare_spoken_12h",
        ),
    )


def _stripped_1030_echo() -> dict[str, object]:
    return TimeConstraint(
        mode="exact",
        start_time="10:30",
        start_time_ambiguity="twelve_hour",
    ).model_dump(mode="json")


def test_non_time_booking_updates_do_not_turn_inherited_time_echo_into_mutation() -> None:
    cases = (
        ("doctor_id", "doc-b"),
        ("branch_id", "branch-b"),
        ("device_key", "device-b"),
        ("service_id", "svc-bikini"),
    )
    for field, value in cases:
        state = _time_authority_booking_state()
        operation = TurnOperation(
            type="book",
            entities=TurnEntities(),
            execution_intent="execute",
            active_task_relationship="continue",
        )
        step = PlanStep(
            operation_index=0,
            operation_type="book",
            disposition="state_update",
            state_action="update_active",
            response_goal="clarification",
            facts={field: value, "time": _stripped_1030_echo()},
        )
        updated = apply_step_state(
            state,
            step=step,
            operation=operation,
            reads=None,
            now=NOW,
            turn_id=f"turn-{field}",
        ).active_task
        assert isinstance(updated, BookingTaskState)
        assert updated.constraints.time is not None
        assert updated.constraints.time.spoken_period_hint == "pm"
        assert updated.constraints.time_clock_source == "bare_spoken_12h"
        assert updated.version == state.version + 1


def test_explicit_same_clock_correction_removes_stale_period_hint() -> None:
    state = _time_authority_booking_state()
    semantic = TimeConstraint(
        mode="exact",
        start_time="10:30",
        start_time_ambiguity="twelve_hour",
    )
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(time=semantic),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["time"],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts={"time": semantic.model_dump(mode="json")},
    )
    updated = apply_step_state(
        state, step=step, operation=operation, reads=None, now=NOW, turn_id="turn-time-correction"
    ).active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "10:30"
    assert updated.constraints.time.start_time_ambiguity == "twelve_hour"
    assert updated.constraints.time.spoken_period_hint == "none"
    assert updated.version == state.version + 1


def test_verified_same_clock_canonical_transition_applies_despite_numeric_equality() -> None:
    state = _time_authority_booking_state()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="execute",
        active_task_relationship="continue",
    )
    verified = TimeConstraint(
        mode="exact",
        start_time="10:30",
        start_time_ambiguity="none",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="present_availability",
        facts={
            "time": verified.model_dump(mode="json"),
            "verified_availability_reference": True,
            "selected_option_ref": "slot-1",
        },
    )
    updated = apply_step_state(
        state, step=step, operation=operation, reads=None, now=NOW, turn_id="turn-verified-slot"
    ).active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time == verified
    assert updated.constraints.time.spoken_period_hint == "none"
    assert updated.version == state.version + 1


def test_unknown_unproven_material_time_fact_fails_closed() -> None:
    state = _time_authority_booking_state()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="execute",
        active_task_relationship="continue",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts={
            "time": TimeConstraint(
                mode="exact", start_time="11:30", start_time_ambiguity="twelve_hour"
            ).model_dump(mode="json")
        },
    )
    updated = apply_step_state(
        state, step=step, operation=operation, reads=None, now=NOW, turn_id="turn-unproven-time"
    ).active_task
    assert updated == state


def test_deterministic_past_time_clear_remains_authoritative() -> None:
    state = _time_authority_booking_state()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="execute",
        active_task_relationship="continue",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts={"time": None, "_backend_time_state_action": "clear_past"},
    )
    updated = apply_step_state(
        state, step=step, operation=operation, reads=None, now=NOW, turn_id="turn-past-clear"
    ).active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is None
    assert updated.version == state.version + 1


def test_reschedule_date_only_update_preserves_time_authority_and_target() -> None:
    state = RescheduleTaskState(
        write_authorization=WriteAuthorization(
            operation="reschedule",
            authorized=True,
            source_turn_id="turn-reschedule",
            granted_at=NOW,
        ),
        target=RescheduleTarget(appointment_id="apt-1", service_id="svc-underarm"),
        replacement=CustomerConstraints(
            service_id="svc-underarm",
            date=None,
            time=TimeConstraint(
                mode="exact",
                start_time="10:30",
                start_time_ambiguity="twelve_hour",
                spoken_period_hint="pm",
            ),
            time_clock_source="bare_spoken_12h",
        ),
    )
    date = DateConstraint(mode="exact", start_date="2026-10-15")
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(date=date),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date"],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="reschedule",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts={"date": date.model_dump(mode="json"), "time": _stripped_1030_echo()},
    )
    updated = apply_step_state(
        state, step=step, operation=operation, reads=None, now=NOW, turn_id="turn-reschedule-date"
    ).active_task
    assert isinstance(updated, RescheduleTaskState)
    assert updated.target == state.target
    assert updated.replacement.date == date
    assert updated.replacement.time is not None
    assert updated.replacement.time.spoken_period_hint == "pm"
    assert updated.replacement.time_clock_source == "bare_spoken_12h"
    assert updated.version == state.version + 1



def _time_authority_reschedule_state() -> RescheduleTaskState:
    return RescheduleTaskState(
        write_authorization=WriteAuthorization(
            operation="reschedule",
            authorized=True,
            source_turn_id="turn-reschedule-authority",
            granted_at=NOW,
        ),
        target=RescheduleTarget(appointment_id="apt-1", service_id="svc-underarm"),
        replacement=CustomerConstraints(
            service_id="svc-underarm",
            date=DateConstraint(mode="exact", start_date="2026-10-15"),
            time=TimeConstraint(
                mode="exact",
                start_time="10:30",
                start_time_ambiguity="twelve_hour",
                spoken_period_hint="pm",
            ),
            time_clock_source="bare_spoken_12h",
        ),
    )


def test_reschedule_explicit_same_clock_correction_removes_stale_pm() -> None:
    state = _time_authority_reschedule_state()
    semantic = TimeConstraint(
        mode="exact", start_time="10:30", start_time_ambiguity="twelve_hour"
    )
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(time=semantic),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["time"],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="reschedule",
        disposition="state_update",
        state_action="update_active",
        facts={"time": semantic.model_dump(mode="json")},
    )
    updated = apply_step_state(
        state, step=step, operation=operation, reads=None, now=NOW, turn_id="turn-rs-correction"
    ).active_task
    assert isinstance(updated, RescheduleTaskState)
    assert updated.target == state.target
    assert updated.replacement.time is not None
    assert updated.replacement.time.start_time == "10:30"
    assert updated.replacement.time.start_time_ambiguity == "twelve_hour"
    assert updated.replacement.time.spoken_period_hint == "none"


def test_reschedule_verified_same_clock_selection_applies_canonical_time() -> None:
    state = _time_authority_reschedule_state()
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(),
        execution_intent="execute",
        active_task_relationship="continue",
    )
    canonical = TimeConstraint(mode="exact", start_time="10:30", start_time_ambiguity="none")
    step = PlanStep(
        operation_index=0,
        operation_type="reschedule",
        disposition="state_update",
        state_action="update_active",
        facts={
            "time": canonical.model_dump(mode="json"),
            "verified_availability_reference": True,
            "selected_option_ref": "slot-rs-1",
        },
    )
    updated = apply_step_state(
        state, step=step, operation=operation, reads=None, now=NOW, turn_id="turn-rs-slot"
    ).active_task
    assert isinstance(updated, RescheduleTaskState)
    assert updated.target == state.target
    assert updated.replacement.time == canonical
    assert updated.replacement.time.spoken_period_hint == "none"


def test_execute_continuation_with_explicit_time_does_not_grant_booking_authority() -> None:
    state = BookingTaskState(
        status="collecting",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id="svc-bikini"),
        requested_service_ids=["svc-underarm", "svc-bikini"],
    )
    operation = TurnOperation(
        type="book",
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["time"],
        entities=TurnEntities(time=TimeConstraint(mode="exact", start_time="20:00")),
    )
    updated = _promote_booking_authorization_for_explicit_continuation(
        state, operation=operation, turn_id="turn-target", now=NOW
    )
    assert updated.write_authorization.authorized is False
