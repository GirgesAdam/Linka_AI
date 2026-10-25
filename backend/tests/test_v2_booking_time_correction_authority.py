from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    plan_active_task_progress,
)
from app.services.agent_v2.orchestrator import _prepare_booking_proposal
from app.services.agent_v2.planner import (
    _EXPLICIT_TIME_AUTHORITY_PARAM,
    PlannerContext,
    PlanStep,
    WriteIntent,
    plan_turn,
)
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import (
    BookingProposal,
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    OptionChoice,
    OptionSnapshot,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state
from app.services.agent_v2.state_rules import apply_booking_time_change, booking_proposal_is_current
from app.services.agent_v2.write_executor import execute_write_ready_step
from app.services.whatsapp_interactions import parse_whatsapp_booking_action

NOW = datetime(2026, 10, 11, 16, 0, tzinfo=UTC)
SERVICE_ID = "11111111-1111-4111-8111-111111111111"
DOCTOR_ID = "22222222-2222-4222-8222-222222222222"
BRANCH_ID = "33333333-3333-4333-8333-333333333333"
PATIENT_ID = "44444444-4444-4444-8444-444444444444"


def _catalog() -> dict[str, object]:
    return {
        "services": [{"id": SERVICE_ID, "name": "Underarm + Bikini", "duration_minutes": 45}],
        "branches": [{"id": BRANCH_ID, "name": "New Cairo"}],
        "doctors": [
            {
                "id": DOCTOR_ID,
                "name": "Maryam",
                "service_ids": [SERVICE_ID],
                "branch_ids": [BRANCH_ID],
            }
        ],
    }


def _start_at(clock: str) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    return datetime(2026, 10, 13, hour, minute, tzinfo=ZoneInfo("Africa/Cairo")).astimezone(UTC)


def _proposal(clock: str, *, proposal_id: str = "proposal-current", task_version: int = 4) -> BookingProposal:
    start = _start_at(clock)
    return BookingProposal(
        proposal_id=proposal_id,
        patient_id=PATIENT_ID,
        service_ids=[SERVICE_ID],
        main_service_id=SERVICE_ID,
        additional_service_ids=[],
        branch_id=BRANCH_ID,
        doctor_id=DOCTOR_ID,
        start_at=start,
        end_at=start + timedelta(minutes=45),
        task_version=task_version,
        proposal_version=1,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


def _task(
    clock: str,
    *,
    proposal: BookingProposal | None = None,
    with_snapshot: bool = False,
    version: int = 4,
) -> BookingTaskState:
    snapshot = None
    if with_snapshot:
        snapshot = OptionSnapshot(
            snapshot_id="snapshot-old",
            purpose="booking_slot",
            task_version=version,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
            options=[
                OptionChoice(
                    ref="slot-old",
                    label="old slot",
                    payload={"start_time_24h": clock},
                )
            ],
        )
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-start",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(
            service_id=SERVICE_ID,
            doctor_id=DOCTOR_ID,
            branch_id=BRANCH_ID,
            date=DateConstraint(mode="exact", start_date="2026-10-13"),
            time=TimeConstraint(
                mode="exact",
                start_time=clock,
                start_time_ambiguity="none",
            ),
            package_usage="unspecified",
        ),
        requested_service_ids=[SERVICE_ID],
        proposal=proposal,
        option_snapshot=snapshot,
        derived=DerivedBookingState(
            availability_snapshot_id="snapshot-old" if with_snapshot else None,
            selected_slot_ref="slot-old" if with_snapshot else None,
        ),
        version=version,
    )


def _correction_operation(clock: str) -> TurnOperation:
    return TurnOperation(
        type="book",
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["time"],
        active_task_corrected_fields=["time"],
        entities=TurnEntities(
            time=TimeConstraint(
                mode="exact",
                start_time=clock,
                start_time_ambiguity="none",
            )
        ),
    )


def _restatement_operation(clock: str, *, include_date: bool = False) -> TurnOperation:
    explicit_fields = ["date", "time"] if include_date else ["time"]
    return TurnOperation(
        type="book",
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=explicit_fields,
        active_task_corrected_fields=[],
        entities=TurnEntities(
            date=(
                DateConstraint(mode="exact", start_date="2026-10-13")
                if include_date
                else None
            ),
            time=TimeConstraint(
                mode="exact",
                start_time=clock,
                start_time_ambiguity="none",
            ),
        ),
    )


def _proposal_step(clock: str) -> PlanStep:
    start = _start_at(clock)
    return PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters={
                "service_id": SERVICE_ID,
                "doctor_id": DOCTOR_ID,
                "branch_id": BRANCH_ID,
                "start_at": start.isoformat(),
                "package_usage": "unspecified",
            },
            requires_verification=True,
        ),
        response_goal="booking_completed",
        facts={
            "requested_service_ids": [SERVICE_ID],
            "booking_proposal_required": True,
        },
    )


def _availability(clock: str | None) -> ReadExecutionBundle:
    if clock is None:
        return ReadExecutionBundle(
            results=[ReadResult(kind="availability", ok=True, payload={"slots": []})]
        )
    start = _start_at(clock)
    return ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=True,
                payload={
                    "slots": [
                        {
                            "start_at": start.isoformat(),
                            "end_at": (start + timedelta(minutes=45)).isoformat(),
                            "start_local": start.astimezone(ZoneInfo("Africa/Cairo")).isoformat(),
                            "start_time_24h": clock,
                            "doctor_id": DOCTOR_ID,
                            "doctor_name": "Maryam",
                            "branch_id": BRANCH_ID,
                            "branch_name": "New Cairo",
                        }
                    ]
                },
            )
        ]
    )


def _prepare(monkeypatch, task: BookingTaskState, *, step_clock: str, available_clock: str | None):
    monkeypatch.setattr(
        "app.services.agent_v2.orchestrator.resolve_additional_services",
        lambda *_args, **_kwargs: [],
    )
    return _prepare_booking_proposal(
        _proposal_step(step_clock),
        reads=_availability(available_clock),
        current_task=task,
        db=SimpleNamespace(),
        workspace=SimpleNamespace(id="workspace-1", timezone="Africa/Cairo"),
        patient=SimpleNamespace(id=PATIENT_ID),
        catalog=_catalog(),
        now=NOW,
        turn_id="turn-proposal",
    )


def test_t1_booking_time_correction_is_authoritative_and_invalidates_stale_state() -> None:
    old = _proposal("11:00")
    state = _task("11:00", proposal=old, with_snapshot=True)
    operation = _correction_operation("12:00")
    stale_step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts={"time": TimeConstraint(mode="exact", start_time="11:00").model_dump(mode="json")},
    )

    adapted = adapt_matching_active_task_step(
        stale_step,
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    assert adapted.facts["time"]["start_time"] == "12:00"

    transition = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-correction",
    )
    updated = transition.active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "12:00"
    assert updated.option_snapshot is None
    assert updated.proposal is None
    assert updated.derived.availability_snapshot_id is None
    assert updated.derived.selected_slot_ref is None


def test_t2_corrected_available_time_executes_matching_authorized_booking(monkeypatch) -> None:
    advanced, proposal = _prepare(
        monkeypatch,
        _task("12:00", version=5),
        step_clock="12:00",
        available_clock="12:00",
    )
    assert advanced.disposition == "write_ready"
    assert advanced.response_goal == "booking_completed"
    assert advanced.write_intent is not None
    assert advanced.write_intent.kind == "booking_proposal"
    assert proposal is not None
    assert proposal.start_at.astimezone(ZoneInfo("Africa/Cairo")).strftime("%H:%M") == "12:00"


def test_t3_unavailable_corrected_time_never_falls_back_to_old_slot(monkeypatch) -> None:
    advanced, proposal = _prepare(
        monkeypatch,
        _task("12:00", version=5),
        step_clock="12:00",
        available_clock="11:00",
    )
    assert proposal is None
    assert advanced.write_intent is None
    assert advanced.response_goal == "requested_time_unavailable"


def test_t4_old_proposal_is_stale_immediately_after_time_correction() -> None:
    old = _proposal("11:00")
    corrected = apply_booking_time_change(
        _task("11:00", proposal=old),
        time=TimeConstraint(mode="exact", start_time="12:00", start_time_ambiguity="none"),
    )
    assert corrected.proposal is None
    assert not booking_proposal_is_current(
        corrected,
        proposal_id=old.proposal_id,
        now=NOW + timedelta(minutes=1),
    )


def test_t5_fresh_wrong_time_proposal_is_blocked_even_with_verified_old_slot(monkeypatch) -> None:
    advanced, proposal = _prepare(
        monkeypatch,
        _task("12:00", version=5),
        step_clock="11:00",
        available_clock="11:00",
    )
    assert proposal is None
    assert advanced.write_intent is None
    assert advanced.facts["booking_proposal_time_constraint_mismatch"] is True


def _confirmation_step(task: BookingTaskState) -> PlanStep:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="confirm_booking_proposal",
                execution_intent="execute",
                entities=TurnEntities(),
            )
        ]
    )
    return plan_turn(
        turn,
        PlannerContext(
            semantic_context=build_semantic_context(_catalog()),
            active_task=task,
            now=NOW + timedelta(minutes=1),
        ),
    ).steps[0]


def test_t6_text_confirmation_carries_corrected_time_authority_and_writes_it(monkeypatch) -> None:
    proposal = _proposal("12:00")
    task = _task("12:00", proposal=proposal)
    step = _confirmation_step(task)
    assert step.write_intent is not None
    assert step.write_intent.parameters[_EXPLICIT_TIME_AUTHORITY_PARAM] == "12:00"

    captured: list[str] = []
    monkeypatch.setattr(
        "app.services.agent_v2.write_executor.require_tia_workspace_domain_write",
        lambda *_args, **_kwargs: None,
    )

    def fake_execute(*_args, proposal: BookingProposal, **_kwargs):
        captured.append(proposal.start_at.astimezone(ZoneInfo("Africa/Cairo")).strftime("%H:%M"))
        return {"ok": True, "write_kind": "booking_proposal", "appointment_id": "appointment-1"}

    monkeypatch.setattr(
        "app.services.agent_v2.write_executor.execute_booking_proposal",
        fake_execute,
    )
    result = execute_write_ready_step(
        SimpleNamespace(begin_nested=lambda: nullcontext()),
        workspace=SimpleNamespace(id="workspace-1", timezone="Africa/Cairo"),
        patient=SimpleNamespace(id=PATIENT_ID, status="active"),
        step=step,
        commit=False,
    )
    assert result["ok"] is True
    assert captured == ["12:00"]


def test_t7_button_confirmation_routes_to_same_authoritative_proposal_write() -> None:
    proposal = _proposal("12:00")
    task = _task("12:00", proposal=proposal)
    action = parse_whatsapp_booking_action(
        {
            "interactive_reply": {
                "type": "button_reply",
                "id": f"tia.booking.proposal.confirm:{proposal.proposal_id}",
                "title": "تأكيد الحجز",
            }
        }
    )
    assert action is not None
    assert action.action == "proposal_confirm"
    assert action.proposal_id == proposal.proposal_id
    step = _confirmation_step(task)
    assert step.write_intent is not None
    assert step.write_intent.parameters[_EXPLICIT_TIME_AUTHORITY_PARAM] == "12:00"


def test_t8_multiple_time_corrections_leave_only_latest_authority() -> None:
    state = _task("11:00", proposal=_proposal("11:00"))
    at_twelve = apply_booking_time_change(
        state,
        time=TimeConstraint(mode="exact", start_time="12:00", start_time_ambiguity="none"),
    )
    at_one = apply_booking_time_change(
        at_twelve,
        time=TimeConstraint(mode="exact", start_time="13:00", start_time_ambiguity="none"),
    )
    assert at_twelve.proposal is None
    assert at_one.proposal is None
    assert at_one.constraints.time is not None
    assert at_one.constraints.time.start_time == "13:00"


def test_confirmation_write_fails_closed_if_current_authority_and_proposal_time_diverge(monkeypatch) -> None:
    task = _task("12:00", proposal=_proposal("11:00"))
    step = _confirmation_step(task)
    monkeypatch.setattr(
        "app.services.agent_v2.write_executor.require_tia_workspace_domain_write",
        lambda *_args, **_kwargs: None,
    )
    called = False

    def fake_execute(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(
        "app.services.agent_v2.write_executor.execute_booking_proposal",
        fake_execute,
    )
    result = execute_write_ready_step(
        SimpleNamespace(begin_nested=lambda: nullcontext()),
        workspace=SimpleNamespace(id="workspace-1", timezone="Africa/Cairo"),
        patient=SimpleNamespace(id=PATIENT_ID, status="active"),
        step=step,
        commit=False,
    )
    assert result["ok"] is False
    assert result["error_code"] == "booking_proposal_time_mismatch"
    assert called is False


def test_time_02_a_wrong_old_booking_correction_clears_stale_write_authority(monkeypatch) -> None:
    state = _task("16:00", proposal=_proposal("16:00"), with_snapshot=True)
    operation = _correction_operation("16:00")
    stale_step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts={"time": TimeConstraint(mode="exact", start_time="16:00").model_dump(mode="json")},
    )

    adapted = adapt_matching_active_task_step(
        stale_step,
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    assert adapted.disposition == "clarify"
    assert adapted.clarification_field == "time"
    assert adapted.write_intent is None

    transition = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="time-02-a",
    )
    updated = transition.active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is None
    assert updated.proposal is None
    assert updated.option_snapshot is None
    assert updated.derived.availability_snapshot_id is None
    assert updated.derived.selected_slot_ref is None

    monkeypatch.setattr(
        "app.services.agent_v2.write_executor.create_appointment_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("booking write must not run")),
    )
    result = execute_write_ready_step(
        SimpleNamespace(),
        workspace=SimpleNamespace(id="workspace-1", timezone="Africa/Cairo"),
        patient=SimpleNamespace(id=PATIENT_ID, status="active"),
        step=adapted,
        commit=False,
    )
    assert result["ok"] is False
    assert result["error_code"] == "write_not_ready"


def test_time_02_b_correct_new_booking_target_invalidates_old_and_requires_fresh_verification(monkeypatch) -> None:
    state = _task("16:00", proposal=_proposal("16:00"), with_snapshot=True)
    operation = _correction_operation("17:00")
    stale_step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts={"time": TimeConstraint(mode="exact", start_time="16:00").model_dump(mode="json")},
    )

    adapted = adapt_matching_active_task_step(
        stale_step,
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    transition = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="time-02-b",
    )
    updated = transition.active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "17:00"
    assert updated.proposal is None
    assert updated.option_snapshot is None
    assert updated.derived.availability_snapshot_id is None
    assert updated.derived.selected_slot_ref is None
    assert updated.write_authorization == state.write_authorization
    assert updated.write_authorization.authorized is True

    progress = plan_active_task_progress(
        updated,
        operation_index=0,
        context=build_semantic_context(_catalog()),
    )
    assert progress.disposition == "read"
    assert any(read.kind == "availability" for read in progress.reads)
    assert progress.write_intent is None
    assert progress.facts["booking_commercial_basis_step"] is True

    # Normal booking remains possible after the existing commercial-basis gate and
    # fresh exact-slot verification; the repair does not create a second booking path.
    verified_step, proposal = _prepare(
        monkeypatch,
        updated,
        step_clock="17:00",
        available_clock="17:00",
    )
    assert verified_step.disposition == "write_ready"
    assert verified_step.response_goal == "booking_completed"
    assert verified_step.write_intent is not None
    assert verified_step.write_intent.authorized is True
    assert proposal is not None
    assert proposal.start_at.astimezone(ZoneInfo("Africa/Cairo")).strftime("%H:%M") == "17:00"


def test_time_authority_review_first_time_edit_stays_unauthorized() -> None:
    state = _task("16:00", proposal=_proposal("16:00"), with_snapshot=True).model_copy(
        update={"write_authorization": WriteAuthorization(operation="booking", authorized=False)}
    )
    operation = TurnOperation(
        type="book",
        execution_intent="informational",
        active_task_relationship="continue",
        active_task_explicit_fields=["time"],
        active_task_corrected_fields=["time"],
        entities=TurnEntities(time=TimeConstraint(mode="exact", start_time="17:00")),
    )
    base = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
    )

    adapted = adapt_matching_active_task_step(
        base,
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="review-first-time-edit",
    ).active_task

    assert adapted.write_intent is None
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "17:00"
    assert updated.write_authorization.authorized is False
    assert updated.write_authorization.source_turn_id is None
    assert updated.proposal is None
    assert updated.option_snapshot is None


def test_time_authority_execute_continue_active_without_booking_authorization_stays_unauthorized() -> None:
    state = _task("16:00", proposal=_proposal("16:00"), with_snapshot=True).model_copy(
        update={"write_authorization": WriteAuthorization(operation="booking", authorized=False)}
    )
    operation = TurnOperation(
        type="continue_active",
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["time"],
        entities=TurnEntities(time=TimeConstraint(mode="exact", start_time="17:00")),
    )
    base = PlanStep(
        operation_index=0,
        operation_type="continue_active",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
    )

    adapted = adapt_matching_active_task_step(
        base,
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="execute-continuation-no-book-auth",
    ).active_task

    assert adapted.write_intent is None
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "17:00"
    assert updated.write_authorization.authorized is False
    assert updated.proposal is None
    assert updated.option_snapshot is None


def test_time_authority_informational_continue_active_time_edit_never_gains_write_authority() -> None:
    state = _task("16:00", proposal=_proposal("16:00"), with_snapshot=True).model_copy(
        update={"write_authorization": WriteAuthorization(operation="booking", authorized=False)}
    )
    operation = TurnOperation(
        type="continue_active",
        execution_intent="informational",
        active_task_relationship="continue",
        active_task_explicit_fields=["time"],
        entities=TurnEntities(time=TimeConstraint(mode="exact", start_time="17:00")),
    )
    base = PlanStep(
        operation_index=0,
        operation_type="continue_active",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
    )

    adapted = adapt_matching_active_task_step(
        base,
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="informational-continuation-time-edit",
    ).active_task

    assert adapted.write_intent is None
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "17:00"
    assert updated.write_authorization.authorized is False
    assert updated.proposal is None
    assert updated.option_snapshot is None


def test_time_02_i_normal_proposal_confirmation_is_unchanged() -> None:
    proposal = _proposal("16:00")
    step = _confirmation_step(_task("16:00", proposal=proposal))
    assert step.disposition == "write_ready"
    assert step.write_intent is not None
    assert step.write_intent.kind == "booking_proposal"
    assert step.write_intent.parameters[_EXPLICIT_TIME_AUTHORITY_PARAM] == "16:00"


def test_time_02_j_verified_server_slot_selection_bypasses_correction_guard() -> None:
    state = _task("16:00", proposal=None, with_snapshot=True)
    operation = _correction_operation("16:00")
    verified_selection = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        state_action="select_active",
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters={
                "service_id": SERVICE_ID,
                "doctor_id": DOCTOR_ID,
                "branch_id": BRANCH_ID,
                "start_at": _start_at("16:00").isoformat(),
            },
            requires_verification=True,
        ),
        response_goal="booking_completed",
        facts={"verified_booking_slot_selection": True},
    )
    adapted = adapt_matching_active_task_step(
        verified_selection,
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    assert adapted == verified_selection
    assert adapted.disposition == "write_ready"


def _time_03_update_step(clock: str, *, include_date: bool = False) -> PlanStep:
    facts: dict[str, object] = {
        "time": TimeConstraint(
            mode="exact",
            start_time=clock,
            start_time_ambiguity="none",
        ).model_dump(mode="json")
    }
    if include_date:
        facts["date"] = DateConstraint(
            mode="exact",
            start_date="2026-10-13",
        ).model_dump(mode="json")
    return PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts=facts,
    )


def _awaiting_confirmation_task(clock: str) -> BookingTaskState:
    return _task(clock, proposal=_proposal(clock)).model_copy(
        update={"status": "awaiting_confirmation"}
    )


def test_time_03_a_ra_g5_same_date_time_restatement_preserves_proposal_state() -> None:
    state = _awaiting_confirmation_task("18:00")
    operation = _restatement_operation("18:00", include_date=True)
    adapted = adapt_matching_active_task_step(
        _time_03_update_step("18:00", include_date=True),
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    assert adapted.disposition == "state_update"
    assert adapted.clarification_field is None
    assert adapted.facts.get("_time_correction_authority") is None

    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="time-03-a",
    ).active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.status == "awaiting_confirmation"
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "18:00"
    assert updated.proposal is not None
    assert updated.proposal.proposal_id == state.proposal.proposal_id


def test_time_03_b_same_time_simple_restatement_is_not_correction_conflict() -> None:
    state = _awaiting_confirmation_task("18:00")
    operation = _restatement_operation("18:00")
    adapted = adapt_matching_active_task_step(
        _time_03_update_step("18:00"),
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    assert adapted.clarification_field is None
    assert adapted.facts.get("_time_correction_authority") is None


def test_time_03_c_genuine_new_time_correction_invalidates_old_authority() -> None:
    state = _awaiting_confirmation_task("16:00")
    operation = _correction_operation("17:00")
    adapted = adapt_matching_active_task_step(
        _time_03_update_step("17:00"),
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="time-03-c",
    ).active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "17:00"
    assert updated.proposal is None
    assert updated.option_snapshot is None
    progress = plan_active_task_progress(
        updated,
        operation_index=0,
        context=build_semantic_context(_catalog()),
    )
    assert progress.disposition == "read"


def test_time_03_d_wrong_old_correction_fault_injection_fails_closed(monkeypatch) -> None:
    state = _task("16:00", proposal=_proposal("16:00"), with_snapshot=True).model_copy(
        update={"status": "awaiting_confirmation"}
    )
    operation = _correction_operation("16:00")
    adapted = adapt_matching_active_task_step(
        _time_03_update_step("16:00"),
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    assert adapted.disposition == "clarify"
    assert adapted.clarification_field == "time"
    assert adapted.write_intent is None

    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="time-03-d",
    ).active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.time is None
    assert updated.proposal is None
    assert updated.option_snapshot is None
    assert updated.derived.availability_snapshot_id is None
    assert updated.derived.selected_slot_ref is None

    monkeypatch.setattr(
        "app.services.agent_v2.write_executor.create_appointment_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("booking mutation must not run")
        ),
    )
    result = execute_write_ready_step(
        SimpleNamespace(),
        workspace=SimpleNamespace(id="workspace-1", timezone="Africa/Cairo"),
        patient=SimpleNamespace(id=PATIENT_ID, status="active"),
        step=adapted,
        commit=False,
    )
    assert result["ok"] is False
    assert result["error_code"] == "write_not_ready"


def test_time_03_e_confirmation_without_repeating_time_is_unchanged() -> None:
    state = _awaiting_confirmation_task("18:00")
    step = _confirmation_step(state)
    assert step.disposition == "write_ready"
    assert step.write_intent is not None
    assert step.write_intent.kind == "booking_proposal"


def test_time_03_f_confirmation_restatement_same_time_has_no_false_clarification() -> None:
    state = _awaiting_confirmation_task("18:00")
    operation = _restatement_operation("18:00")
    adapted = adapt_matching_active_task_step(
        _time_03_update_step("18:00"),
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    )
    updated = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="time-03-f",
    ).active_task
    assert adapted.clarification_field is None
    assert isinstance(updated, BookingTaskState)
    assert updated.status == "awaiting_confirmation"
    assert updated.proposal is not None


def test_time_03_h_verified_server_slot_selection_remains_unchanged() -> None:
    state = _task("16:00", proposal=None, with_snapshot=True)
    operation = _restatement_operation("16:00")
    verified_selection = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        state_action="select_active",
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters={
                "service_id": SERVICE_ID,
                "doctor_id": DOCTOR_ID,
                "branch_id": BRANCH_ID,
                "start_at": _start_at("16:00").isoformat(),
            },
            requires_verification=True,
        ),
        response_goal="booking_completed",
        facts={"verified_booking_slot_selection": True},
    )
    assert adapt_matching_active_task_step(
        verified_selection,
        operation=operation,
        active_task=state,
        context=build_semantic_context(_catalog()),
        now=NOW,
    ) == verified_selection
