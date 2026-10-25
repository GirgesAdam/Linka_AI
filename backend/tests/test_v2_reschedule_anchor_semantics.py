from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    ActiveTaskRelativeDate,
    AppointmentSelector,
    DateConstraint,
    EntityReference,
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    plan_active_task_progress,
)
from app.services.agent_v2.planner import (
    PlannerContext,
    VerificationFacts,
    advance_step_after_verification,
    plan_turn,
)
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import (
    CustomerConstraints,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state
from app.services.agent_v2.write_executor import execute_write_ready_step

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _semantic_context():
    return build_semantic_context(
        {
            "services": [
                {
                    "id": "svc-underarm",
                    "name": "ليزر إبط",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_key": "candela_gentle", "device_name": "Candela Gentle"}
                    ],
                }
            ],
            "doctors": [{"id": "doc-maryam", "name": "مريم"}],
            "branches": [{"id": "branch-main", "name": "Main"}],
            "appointments": [
                {
                    "appointment_id": "apt-1",
                    "service_id": "svc-underarm",
                    "doctor_id": "doc-maryam",
                    "laser_device_key": "candela_gentle",
                    "status": "confirmed",
                    "start_local": "2026-09-14T19:00:00+03:00",
                },
                {
                    "appointment_id": "apt-2",
                    "service_id": "svc-underarm",
                    "doctor_id": "doc-maryam",
                    "laser_device_key": "candela_gentle",
                    "status": "confirmed",
                    "start_local": "2026-09-20T18:00:00+03:00",
                },
            ],
        }
    )


def _planner_context(*, active_task=None) -> PlannerContext:
    return PlannerContext(
        semantic_context=_semantic_context(),
        active_task=active_task,
        now=NOW,
    )


def _reschedule_operation(*, date: str | None = None, time: str | None = None) -> TurnOperation:
    return TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date=date) if date else None,
            time=TimeConstraint(mode="exact", start_time=time) if time else None,
        ),
        package_usage="unspecified",
    )


def _appointment_reads(rows: list[dict[str, object]]) -> ReadExecutionBundle:
    return ReadExecutionBundle(
        results=[ReadResult(kind="appointments", ok=True, payload={"appointments": rows})]
    )


def _appointment_row(appointment_id: str = "apt-1") -> dict[str, object]:
    return {
        "appointment_id": appointment_id,
        "service_id": "svc-underarm",
        "doctor_id": "doc-maryam",
        "laser_device_key": "candela_gentle",
        "start_local": "2026-09-14T19:00:00+03:00",
    }


def _active_reschedule_state(
    *,
    target_start_local: str = "2026-10-24T15:00:00+03:00",
    replacement_date: str | None = None,
) -> RescheduleTaskState:
    return RescheduleTaskState(
        write_authorization=WriteAuthorization(
            operation="reschedule",
            authorized=True,
            source_turn_id="turn-anchor",
            granted_at=NOW,
        ),
        target=RescheduleTarget(
            appointment_id="apt-1",
            service_id="svc-underarm",
            doctor_id="doc-maryam",
            device_key="candela_gentle",
            start_local=target_start_local,
        ),
        replacement=CustomerConstraints(
            service_id="svc-underarm",
            doctor_id="doc-maryam",
            device_key="candela_gentle",
            date=(
                DateConstraint(mode="exact", start_date=replacement_date)
                if replacement_date
                else None
            ),
        ),
    )



def _relative_reschedule_operation(*, weekday: str, requested_time: str | None = None) -> TurnOperation:
    explicit_fields = ["date"]
    if requested_time is not None:
        explicit_fields.append("time")
    return TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            time=(
                TimeConstraint(mode="exact", start_time=requested_time)
                if requested_time is not None
                else None
            )
        ),
        active_task_relative_date=ActiveTaskRelativeDate(
            relation="next_occurrence",
            weekday=weekday,
        ),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=explicit_fields,
    )


def _adapt_active_reschedule(
    state: RescheduleTaskState,
    operation: TurnOperation,
):
    fresh_step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(active_task=state),
    ).steps[0]
    return adapt_matching_active_task_step(
        fresh_step,
        operation=operation,
        active_task=state,
        context=_semantic_context(),
    )


def test_active_reschedule_next_same_weekday_resolves_from_verified_target_and_writes_later_date() -> None:
    state = _active_reschedule_state()
    operation = _relative_reschedule_operation(weekday="saturday", requested_time="18:00")
    adapted = _adapt_active_reschedule(state, operation)

    assert adapted.disposition == "state_update"
    assert adapted.facts["date"]["start_date"] == "2026-10-31"
    assert adapted.facts["time"]["start_time"] == "18:00"

    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-next-saturday",
    ).active_task
    assert isinstance(updated, RescheduleTaskState)
    assert updated.replacement.date is not None
    assert updated.replacement.date.start_date == "2026-10-31"
    assert updated.replacement.time is not None
    assert updated.replacement.time.start_time == "18:00"

    progress = plan_active_task_progress(updated, operation_index=0, context=_semantic_context())
    availability = next(read for read in progress.reads if read.kind == "availability")
    assert availability.parameters["date"]["start_date"] == "2026-10-31"
    assert availability.parameters["time"]["start_time"] == "18:00"

    ready = advance_step_after_verification(
        progress,
        VerificationFacts(
            appointment_match_count=1,
            exact_slot_match_count=1,
            verified_parameters={"start_at": "2026-10-31T18:00:00+03:00"},
        ),
    )
    assert ready.disposition == "write_ready"
    assert ready.write_intent is not None
    assert ready.write_intent.parameters["start_at"] == "2026-10-31T18:00:00+03:00"


def test_active_reschedule_next_weekday_uses_current_replacement_as_repeated_anchor() -> None:
    state = _active_reschedule_state(replacement_date="2026-10-31")
    operation = _relative_reschedule_operation(weekday="saturday")
    adapted = _adapt_active_reschedule(state, operation)
    assert adapted.facts["date"]["start_date"] == "2026-11-07"

    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-next-saturday-again",
    ).active_task
    assert isinstance(updated, RescheduleTaskState)
    assert updated.replacement.date is not None
    assert updated.replacement.date.start_date == "2026-11-07"


def test_active_reschedule_next_different_weekday_uses_calendar_distance_not_fixed_week() -> None:
    state = _active_reschedule_state(target_start_local="2026-10-22T15:00:00+03:00")
    operation = _relative_reschedule_operation(weekday="saturday")
    adapted = _adapt_active_reschedule(state, operation)
    assert adapted.facts["date"]["start_date"] == "2026-10-24"


def test_active_reschedule_relative_date_without_verified_anchor_fails_closed() -> None:
    state = _active_reschedule_state(target_start_local=None)
    operation = _relative_reschedule_operation(weekday="saturday")
    adapted = _adapt_active_reschedule(state, operation)
    assert adapted.disposition == "clarify"
    assert adapted.clarification_field == "date"
    assert adapted.state_action == "none"
    assert adapted.write_intent is None


def test_active_reschedule_same_date_and_explicit_exact_date_controls_remain_normal() -> None:
    state = _active_reschedule_state()
    same_date = TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-10-24"),
            time=TimeConstraint(mode="exact", start_time="18:00"),
        ),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date", "time"],
    )
    same_adapted = _adapt_active_reschedule(state, same_date)
    same_updated = apply_step_state(
        state,
        step=same_adapted,
        operation=same_date,
        reads=None,
        now=NOW,
        turn_id="turn-same-date",
    ).active_task
    assert isinstance(same_updated, RescheduleTaskState)
    assert same_updated.replacement.date is not None
    assert same_updated.replacement.date.start_date == "2026-10-24"
    assert same_updated.replacement.time is not None
    assert same_updated.replacement.time.start_time == "18:00"

    exact_date = TurnOperation(
        type="reschedule",
        entities=TurnEntities(date=DateConstraint(mode="exact", start_date="2026-10-31")),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date"],
    )
    exact_adapted = _adapt_active_reschedule(state, exact_date)
    exact_updated = apply_step_state(
        state,
        step=exact_adapted,
        operation=exact_date,
        reads=None,
        now=NOW,
        turn_id="turn-exact-date",
    ).active_task
    assert isinstance(exact_updated, RescheduleTaskState)
    assert exact_updated.replacement.date is not None
    assert exact_updated.replacement.date.start_date == "2026-10-31"


def test_reschedule_misrouted_relative_option_shape_fails_closed_before_state_authority() -> None:
    state = _active_reschedule_state()
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-10-24"),
            time=TimeConstraint(mode="exact", start_time="18:00"),
        ),
        selection=Selection(kind="relative", relative="next"),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date", "time"],
    )
    fresh_step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(active_task=state),
    ).steps[0]
    adapted = adapt_matching_active_task_step(
        fresh_step,
        operation=operation,
        active_task=state,
        context=_semantic_context(),
    )

    assert adapted.disposition == "clarify"
    assert adapted.state_action == "none"
    assert adapted.write_intent is None

    transition = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-bad-relative-shape",
    )
    assert transition.active_task == state


def test_reschedule_anchors_unique_target_before_date_then_reuses_same_id_on_follow_up() -> None:
    first_operation = _reschedule_operation()
    first_turn = TiaTurnUnderstanding(operations=[first_operation], safety_signals=[])
    first_step = plan_turn(first_turn, _planner_context()).steps[0]

    # The first turn must resolve the existing appointment before asking for replacement timing.
    assert first_step.disposition == "clarify"
    assert first_step.clarification_field == "date"
    assert first_step.state_action == "start_reschedule"
    assert [read.kind for read in first_step.reads] == ["appointments"]
    assert first_step.write_intent is None

    verified_first_step = advance_step_after_verification(
        first_step,
        VerificationFacts(
            appointment_match_count=1,
            verified_parameters={"appointment_id": "apt-1"},
        ),
    )
    anchored_transition = apply_step_state(
        None,
        step=verified_first_step,
        operation=first_operation,
        reads=_appointment_reads([_appointment_row()]),
        now=NOW,
        turn_id="turn-reschedule-start",
    )

    anchored = anchored_transition.active_task
    assert isinstance(anchored, RescheduleTaskState)
    assert anchored.target.appointment_id == "apt-1"
    assert anchored.replacement.date is None
    assert anchored.write_authorization.authorized is True

    # A natural follow-up may be interpreted as reschedule again. Python must update only the
    # replacement constraints and keep the previously verified appointment target.
    follow_up_operation = _reschedule_operation(date="2026-09-15", time="18:00")
    follow_up_turn = TiaTurnUnderstanding(operations=[follow_up_operation], safety_signals=[])
    fresh_follow_up_step = plan_turn(
        follow_up_turn,
        _planner_context(active_task=anchored),
    ).steps[0]
    adapted_follow_up_step = adapt_matching_active_task_step(
        fresh_follow_up_step,
        operation=follow_up_operation,
        active_task=anchored,
        context=_semantic_context(),
    )

    assert adapted_follow_up_step.disposition == "state_update"
    assert adapted_follow_up_step.state_action == "update_active"
    assert "appointment_id" not in adapted_follow_up_step.facts

    updated_transition = apply_step_state(
        anchored,
        step=adapted_follow_up_step,
        operation=follow_up_operation,
        reads=None,
        now=NOW,
        turn_id="turn-reschedule-time",
    )
    updated = updated_transition.active_task
    assert isinstance(updated, RescheduleTaskState)
    assert updated.target.appointment_id == "apt-1"
    assert updated.replacement.date is not None
    assert updated.replacement.date.start_date == "2026-09-15"
    assert updated.replacement.time is not None
    assert updated.replacement.time.start_time == "18:00"

    progress = plan_active_task_progress(
        updated,
        operation_index=0,
        context=_semantic_context(),
    )
    assert progress.disposition == "read"
    assert progress.write_intent is not None
    assert progress.write_intent.kind == "reschedule"
    assert progress.write_intent.parameters["appointment_id"] == "apt-1"
    assert progress.reads[0].kind == "appointments"
    assert progress.reads[0].parameters == {"appointment_id": "apt-1"}
    assert progress.reads[1].kind == "availability"
    assert progress.reads[1].parameters["appointment_id"] == "apt-1"
    assert progress.reads[1].parameters["date"]["start_date"] == "2026-09-15"
    assert progress.reads[1].parameters["time"]["start_time"] == "18:00"

    ready = advance_step_after_verification(
        progress,
        VerificationFacts(
            appointment_match_count=1,
            exact_slot_match_count=1,
            verified_parameters={"start_at": "2026-09-15T18:00:00+03:00"},
        ),
    )
    assert ready.disposition == "write_ready"
    assert ready.write_intent is not None
    assert ready.write_intent.parameters["appointment_id"] == "apt-1"
    assert ready.write_intent.parameters["start_at"] == "2026-09-15T18:00:00+03:00"
    assert ready.write_intent.parameters["_reschedule_required_fields_resolved"] is True


def test_reschedule_does_not_anchor_an_arbitrary_target_when_multiple_appointments_match() -> None:
    operation = _reschedule_operation()
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    verified = advance_step_after_verification(
        step,
        VerificationFacts(appointment_match_count=2),
    )

    assert verified.disposition == "clarify"
    assert verified.clarification_field == "appointment"
    assert verified.response_goal == "ask_appointment_choice"

    transition = apply_step_state(
        None,
        step=verified,
        operation=operation,
        reads=_appointment_reads([_appointment_row("apt-1"), _appointment_row("apt-2")]),
        now=NOW,
        turn_id="turn-reschedule-ambiguous",
    )
    assert transition.active_task is None


def test_reschedule_does_not_create_target_when_no_appointment_matches() -> None:
    operation = _reschedule_operation()
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    verified = advance_step_after_verification(
        step,
        VerificationFacts(appointment_match_count=0),
    )

    assert verified.disposition == "blocked"
    assert verified.write_intent is None

    transition = apply_step_state(
        None,
        step=verified,
        operation=operation,
        reads=_appointment_reads([]),
        now=NOW,
        turn_id="turn-reschedule-missing",
    )
    assert transition.active_task is None


def test_reschedule_missing_time_never_promotes_to_write_ready() -> None:
    operation = _reschedule_operation()
    first_step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    verified_first = advance_step_after_verification(
        first_step,
        VerificationFacts(appointment_match_count=1, verified_parameters={"appointment_id": "apt-1"}),
    )
    transition = apply_step_state(
        None,
        step=verified_first,
        operation=operation,
        reads=_appointment_reads([_appointment_row()]),
        now=NOW,
        turn_id="turn-start",
    )
    state = transition.active_task
    assert isinstance(state, RescheduleTaskState)
    state = state.model_copy(
        update={
            "replacement": state.replacement.model_copy(
                update={"date": DateConstraint(mode="exact", start_date="2026-09-15"), "time": None}
            )
        }
    )
    progress = plan_active_task_progress(state, operation_index=0, context=_semantic_context())
    assert progress.facts["exact_time_requested"] is False
    advanced = advance_step_after_verification(
        progress,
        VerificationFacts(appointment_match_count=1, exact_slot_match_count=1, verified_parameters={"start_at": "2026-09-15T18:00:00+03:00"}),
    )
    assert advanced.disposition != "write_ready"



def test_reschedule_occupied_exact_target_never_promotes_to_write() -> None:
    operation = _reschedule_operation(date="2026-09-15", time="18:00")
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    verified_target = advance_step_after_verification(
        step,
        VerificationFacts(appointment_match_count=1, verified_parameters={"appointment_id": "apt-1"}),
    )
    transition = apply_step_state(
        None,
        step=verified_target,
        operation=operation,
        reads=_appointment_reads([_appointment_row()]),
        now=NOW,
        turn_id="turn-occupied",
    )
    state = transition.active_task
    assert isinstance(state, RescheduleTaskState)
    progress = plan_active_task_progress(state, operation_index=0, context=_semantic_context())
    blocked = advance_step_after_verification(
        progress,
        VerificationFacts(
            appointment_match_count=1,
            exact_slot_match_count=0,
            verified_parameters={},
        ),
    )
    assert blocked.disposition == "blocked"
    assert blocked.response_goal == "requested_time_unavailable"
    assert blocked.disposition != "write_ready"


def _time_02_appointment_ref() -> str:
    context = _semantic_context()
    return next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == "appointment" and target.canonical_id == "apt-1"
    )


def _time_02_branch_ref() -> str:
    context = _semantic_context()
    return next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == "branch" and target.canonical_id == "branch-main"
    )


def _time_02_reschedule_operation(*, source_clock: str, target_clock: str) -> TurnOperation:
    return TurnOperation(
        type="reschedule",
        source_appointment=AppointmentSelector(
            appointment=EntityReference(ref=_time_02_appointment_ref()),
            time=TimeConstraint(mode="exact", start_time=source_clock),
        ),
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-09-14"),
            time=TimeConstraint(mode="exact", start_time=target_clock),
        ),
        execution_intent="execute",
        fresh_task=True,
        fresh_task_explicit_fields=["date", "time"],
    )


def _time_02_verification(*, source_clock: str, target_clock: str) -> VerificationFacts:
    return VerificationFacts(
        appointment_match_count=1,
        exact_slot_match_count=1,
        verified_parameters={
            "appointment_id": "apt-1",
            "branch_id": "branch-main",
            "service_id": "svc-underarm",
            "doctor_id": "doc-maryam",
            "device_key": "candela_gentle",
            "start_at": f"2026-09-14T{target_clock}:00+03:00",
            "_source_appointment_start_at": f"2026-09-14T{source_clock}:00+03:00",
            "_source_appointment_start_local": f"2026-09-14T{source_clock}:00+03:00",
            "_source_appointment_branch_id": "branch-main",
            "_source_appointment_service_id": "svc-underarm",
            "_source_appointment_doctor_id": "doc-maryam",
            "_source_appointment_device_key": "candela_gentle",
        },
    )


def _time_02_source_read(clock: str) -> ReadExecutionBundle:
    row = _appointment_row()
    row["start_local"] = f"2026-09-14T{clock}:00+03:00"
    return _appointment_reads([row])


def test_time_02_c_wrong_old_reschedule_target_never_reaches_mutation(monkeypatch) -> None:
    operation = _time_02_reschedule_operation(source_clock="14:00", target_clock="14:00")
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    blocked = advance_step_after_verification(
        step,
        _time_02_verification(source_clock="14:00", target_clock="14:00"),
    )

    assert blocked.disposition == "clarify"
    assert blocked.clarification_field == "time"
    assert blocked.write_intent is None

    transition = apply_step_state(
        None,
        step=blocked,
        operation=operation,
        reads=_time_02_source_read("14:00"),
        now=NOW,
        turn_id="time-02-c",
    )
    state = transition.active_task
    assert isinstance(state, RescheduleTaskState)
    assert state.target.appointment_id == "apt-1"
    assert state.replacement.time is None

    monkeypatch.setattr(
        "app.services.agent_v2.write_executor.reschedule_appointment_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("reschedule write must not run")),
    )
    result = execute_write_ready_step(
        SimpleNamespace(),
        workspace=SimpleNamespace(id="workspace-1", timezone="Africa/Cairo"),
        patient=SimpleNamespace(id="patient-1", status="active"),
        step=blocked,
        commit=False,
    )
    assert result["ok"] is False
    assert result["error_code"] == "write_not_ready"


def test_time_02_c_explicit_same_branch_does_not_disguise_source_time_as_new_target() -> None:
    operation = TurnOperation(
        type="reschedule",
        source_appointment=AppointmentSelector(
            appointment=EntityReference(ref=_time_02_appointment_ref()),
            time=TimeConstraint(mode="exact", start_time="14:00"),
        ),
        entities=TurnEntities(
            branch=EntityReference(ref=_time_02_branch_ref()),
            date=DateConstraint(mode="exact", start_date="2026-09-14"),
            time=TimeConstraint(mode="exact", start_time="14:00"),
        ),
        execution_intent="execute",
        fresh_task=True,
        fresh_task_explicit_fields=["date", "time"],
    )
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    assert step.facts["branch_id"] == "branch-main"

    blocked = advance_step_after_verification(
        step,
        _time_02_verification(source_clock="14:00", target_clock="14:00"),
    )

    assert blocked.disposition == "clarify"
    assert blocked.clarification_field == "time"
    assert blocked.write_intent is None


def test_time_02_d_correct_new_reschedule_target_can_promote_after_verification() -> None:
    operation = _time_02_reschedule_operation(source_clock="14:00", target_clock="15:00")
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    ready = advance_step_after_verification(
        step,
        _time_02_verification(source_clock="14:00", target_clock="15:00"),
    )
    assert ready.disposition == "write_ready"
    assert ready.write_intent is not None
    assert ready.write_intent.kind == "reschedule"
    assert ready.write_intent.parameters["start_at"] == "2026-09-14T15:00:00+03:00"
    assert ready.write_intent.parameters["_reschedule_required_fields_resolved"] is True


def test_time_02_e_reverse_wording_semantics_do_not_make_old_clock_authoritative() -> None:
    # Textual order is intentionally absent here: the deterministic layer receives only
    # typed source-vs-target semantics, so a wrong OLD target must fail closed either way.
    operation = _time_02_reschedule_operation(source_clock="16:00", target_clock="16:00")
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    blocked = advance_step_after_verification(
        step,
        _time_02_verification(source_clock="16:00", target_clock="16:00"),
    )
    assert blocked.disposition == "clarify"
    assert blocked.clarification_field == "time"
    assert blocked.write_intent is None


def test_time_02_h_unresolved_reschedule_target_remains_fail_closed() -> None:
    operation = _reschedule_operation(date="2026-09-14")
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    advanced = advance_step_after_verification(
        step,
        VerificationFacts(
            appointment_match_count=1,
            exact_slot_match_count=1,
            verified_parameters={"appointment_id": "apt-1"},
        ),
    )
    assert advanced.disposition != "write_ready"


def test_active_reschedule_explicit_old_clock_correction_clears_target_authority() -> None:
    state = _active_reschedule_state(target_start_local="2026-10-24T15:00:00+03:00")
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(time=TimeConstraint(mode="exact", start_time="15:00")),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["time"],
        active_task_corrected_fields=["time"],
    )
    adapted = _adapt_active_reschedule(state, operation)
    assert adapted.disposition == "clarify"
    assert adapted.clarification_field == "time"
    assert adapted.write_intent is None

    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="time-02-active-conflict",
    ).active_task
    assert isinstance(updated, RescheduleTaskState)
    assert updated.replacement.time is None
    assert updated.option_snapshot is None
    assert updated.derived.availability_snapshot_id is None
    assert updated.derived.selected_slot_ref is None


def test_active_reschedule_same_clock_on_new_date_is_not_blanket_blocked() -> None:
    state = _active_reschedule_state(target_start_local="2026-10-24T15:00:00+03:00")
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-10-31"),
            time=TimeConstraint(mode="exact", start_time="15:00"),
        ),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date", "time"],
    )
    adapted = _adapt_active_reschedule(state, operation)
    assert adapted.disposition == "state_update"
    assert adapted.clarification_field is None

    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="time-02-new-date-same-clock",
    ).active_task
    assert isinstance(updated, RescheduleTaskState)
    assert updated.replacement.date is not None
    assert updated.replacement.date.start_date == "2026-10-31"
    assert updated.replacement.time is not None
    assert updated.replacement.time.start_time == "15:00"


def test_time_03_g_same_clock_different_explicit_date_remains_valid() -> None:
    state = _active_reschedule_state(target_start_local="2026-10-24T15:00:00+03:00")
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-10-31"),
            time=TimeConstraint(mode="exact", start_time="15:00"),
        ),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date", "time"],
        active_task_corrected_fields=["date"],
    )
    adapted = _adapt_active_reschedule(state, operation)
    assert adapted.disposition == "state_update"
    assert adapted.clarification_field is None

    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="time-03-g",
    ).active_task
    assert isinstance(updated, RescheduleTaskState)
    assert updated.replacement.date is not None
    assert updated.replacement.date.start_date == "2026-10-31"
    assert updated.replacement.time is not None
    assert updated.replacement.time.start_time == "15:00"


def test_time_03_i_reschedule_wrong_old_protection_remains_fail_closed() -> None:
    operation = _time_02_reschedule_operation(source_clock="14:00", target_clock="14:00")
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    blocked = advance_step_after_verification(
        step,
        _time_02_verification(source_clock="14:00", target_clock="14:00"),
    )
    assert blocked.disposition == "clarify"
    assert blocked.clarification_field == "time"
    assert blocked.write_intent is None


def test_time_03_j_unresolved_reschedule_target_still_never_writes() -> None:
    operation = _reschedule_operation(date="2026-09-14")
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _planner_context(),
    ).steps[0]
    advanced = advance_step_after_verification(
        step,
        VerificationFacts(
            appointment_match_count=1,
            exact_slot_match_count=1,
            verified_parameters={"appointment_id": "apt-1"},
        ),
    )
    assert advanced.disposition != "write_ready"
    assert advanced.write_intent is None or advanced.disposition != "write_ready"
