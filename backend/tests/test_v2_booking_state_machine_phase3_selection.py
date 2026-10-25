from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

import app.services.agent_v2.stateful_test_harness as stateful_harness
from app.agents.v2.availability_reference_interpreter import (
    ReferenceDecision,
    ReferenceInterpretation,
)
from app.agents.v2.turn_contract import (
    DateConstraint,
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.orchestrator import (
    _attach_verified_booking_proposal_to_task,
    _prepare_booking_proposal,
    _verified_booking_selection_plan,
)
from app.services.agent_v2.planner import (
    PlanStep,
    VerificationFacts,
    WriteIntent,
    advance_step_after_verification,
)
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    OptionChoice,
    OptionSnapshot,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state
from app.services.agent_v2.state_persistence import _decode_flow_task, _task_namespace
from app.services.agent_v2.test_harness import V2FixtureEnvironment

NOW = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
A = "service-a"
B = "service-b"
DOCTOR = "doctor-1"
DEVICE = "device-1"
BRANCH = "branch-1"


def _catalog(*, grouped: bool = True, doctor_common: bool = True, device_common: bool = True):
    services = [
        {
            "id": A,
            "name": "Service A",
            "operational_category": "laser",
            "requires_laser_device": True,
            "duration_minutes": 15,
            "laser_devices": [
                {
                    "device_key": DEVICE,
                    "device_name": "Device 1",
                    "price_minor": 50_000,
                    "duration_minutes": 15,
                    "configured": True,
                }
            ],
        }
    ]
    if grouped:
        services.append(
            {
                "id": B,
                "name": "Service B",
                "operational_category": "laser",
                "requires_laser_device": True,
                "duration_minutes": 20,
                "laser_devices": [
                    {
                        "device_key": DEVICE if device_common else "device-2",
                        "device_name": "Device 1" if device_common else "Device 2",
                        "price_minor": 70_000,
                        "duration_minutes": 20,
                        "configured": True,
                    }
                ],
            }
        )
    return {
        "services": services,
        "doctors": [
            {
                "id": DOCTOR,
                "name": "Doctor 1",
                "service_ids": [A, B] if grouped and doctor_common else [A],
                "branch_ids": [BRANCH],
            }
        ],
        "branches": [{"id": BRANCH, "name": "Clinic"}],
        "appointments": [],
    }


def _slot(*, service_id: str = A, time: str = "20:00") -> dict[str, object]:
    return {
        "service_id": service_id,
        "branch_id": BRANCH,
        "doctor_id": DOCTOR,
        "device_key": DEVICE,
        "laser_device_key": DEVICE,
        "start_local": f"2026-09-12T{time}:00+03:00",
        "start_time_24h": time,
        "start_at": f"2026-09-12T{time}:00+03:00",
        "end_at": "2026-09-12T20:20:00+03:00",
        "doctor_name": "Doctor 1",
        "laser_device_name": "Device 1",
    }


def _task(*, grouped: bool = True, expired: bool = False, stale_version: bool = False):
    version = 4
    selected_service = A
    return BookingTaskState(
        status="awaiting_slot",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id=B if grouped else A, branch_id=BRANCH),
        requested_service_ids=[A, B] if grouped else [],
        option_snapshot=OptionSnapshot(
            snapshot_id="snapshot-1",
            purpose="booking_slot",
            task_version=version - 1 if stale_version else version,
            created_at=NOW - timedelta(minutes=5),
            expires_at=NOW - timedelta(seconds=1) if expired else NOW + timedelta(minutes=10),
            options=[OptionChoice(ref="slot-1", label="20:00", payload=_slot(service_id=selected_service))],
        ),
        version=version,
    )


def _context(*, grouped: bool = True, concrete: bool = True, services=None):
    option = {
        "option_ref": "opt_1",
        "index": 1,
        "concrete": concrete,
        "service_id": A,
        "start_time_24h": "20:00",
        "start_local": "2026-09-12T20:00:00+03:00",
        "end_local": "2026-09-12T20:00:00+03:00",
    }
    if concrete:
        option["slot"] = _slot(service_id=A)
    context = {
        "operation_type": "availability",
        "availability_reference_options": [option],
    }
    if grouped:
        context["requested_service_ids"] = services if services is not None else [A, B]
        context["multi_service_single_appointment"] = True
    return context


def _decision() -> ReferenceDecision:
    return ReferenceDecision(
        action="select_presented_option",
        option_ref="opt_1",
        exact_time=None,
        selection_scope="none",
    )


def _selection_plan(*, grouped: bool = True, catalog=None, context=None, task=None, selection_time="20:00"):
    return _verified_booking_selection_plan(
        task or _task(grouped=grouped),
        decision=_decision(),
        availability_context=context or _context(grouped=grouped),
        catalog=catalog or _catalog(grouped=grouped),
        selection_time=selection_time,
        now=NOW,
    )


def _operation() -> TurnOperation:
    return TurnOperation(
        type="book",
        entities=TurnEntities(),
        selection=Selection(kind="ref", ref="opt_1"),
        execution_intent="informational",
        active_task_relationship="continue",
        continues_previous=True,
    )


def test_single_and_grouped_concrete_options_use_same_fresh_verification_boundary() -> None:
    single = _selection_plan(grouped=False)
    grouped = _selection_plan(grouped=True)

    assert single is not None and grouped is not None
    for plan, _context_value in (single, grouped):
        step = plan.steps[0]
        assert step.facts["verified_booking_slot_selection"] is True
        assert step.facts["selected_option_ref"] == "opt_1"
        assert step.facts["booking_proposal_required"] is True
        assert step.facts["exact_time_requested"] is True
        assert step.write_intent is not None and step.write_intent.authorized is True
        assert step.write_intent.requires_verification is True
        assert [read.kind for read in step.reads] == ["availability"]
        assert step.state_action == "select_active"
    assert single[0].steps[0].facts["requested_service_ids"] == [A]
    assert grouped[0].steps[0].facts["requested_service_ids"] == [A, B]


def test_selection_fails_closed_for_stale_expired_compressed_or_wrong_clock() -> None:
    assert _selection_plan(task=_task(stale_version=True)) is None
    assert _selection_plan(task=_task(expired=True)) is None
    assert _selection_plan(context=_context(concrete=False), selection_time=None) is None

    assert _selection_plan(selection_time="08:00") is None


def test_group_scope_and_resource_incompatibility_do_not_progress_to_fresh_booking_write() -> None:
    assert _selection_plan(context=_context(services=[A])) is None

    doctor_rejected = _selection_plan(catalog=_catalog(doctor_common=False))
    assert doctor_rejected is not None
    doctor_step = doctor_rejected[0].steps[0]
    assert doctor_step.disposition == "clarify"
    assert doctor_step.clarification_field == "doctor"
    assert doctor_step.write_intent is None

    device_rejected = _selection_plan(catalog=_catalog(device_common=False))
    assert device_rejected is not None
    device_step = device_rejected[0].steps[0]
    assert device_step.disposition == "clarify"
    assert device_step.clarification_field == "device"
    assert device_step.write_intent is None


def test_marked_review_first_candidate_with_non_booking_operation_fails_closed() -> None:
    candidate = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="respond",
        write_intent=WriteIntent(
            kind="booking",
            authorized=False,
            parameters={"service_id": A},
            requires_verification=True,
        ),
        response_goal="booking_proposal",
        facts={
            "requested_service_ids": [A],
            "booking_proposal_required": True,
            "_verified_booking_proposal_candidate": True,
        },
    )
    prepared, proposal = _prepare_booking_proposal(
        candidate,
        operation=TurnOperation(
            type="availability",
            entities=TurnEntities(),
            execution_intent="informational",
        ),
        reads=ReadExecutionBundle(),
        current_task=_task(grouped=False),
        db=object(),
        workspace=SimpleNamespace(id="workspace", timezone="Africa/Cairo"),
        patient=SimpleNamespace(id="patient"),
        catalog=_catalog(grouped=False),
        now=NOW,
        turn_id="invalid-candidate-operation",
        additional_service_resolver=lambda _proposal: [],
    )

    assert proposal is None
    assert prepared.disposition == "blocked"
    assert prepared.write_intent is None
    assert prepared.response_goal == "clarification"
    assert prepared.facts["booking_proposal_candidate_invalid"] is True


def test_verified_review_first_proposal_builder_rejection_fails_closed() -> None:
    slot = _slot(service_id=A)
    reads = ReadExecutionBundle(
        results=[ReadResult(kind="availability", ok=True, payload={"slots": [slot]})],
        verification=VerificationFacts(
            exact_slot_match_count=1,
            verified_parameters={
                "service_id": A,
                "branch_id": BRANCH,
                "doctor_id": DOCTOR,
                "device_key": DEVICE,
                "start_at": slot["start_at"],
            },
        ),
    )
    candidate = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="respond",
        write_intent=WriteIntent(
            kind="booking",
            authorized=False,
            parameters={
                "service_id": A,
                "branch_id": BRANCH,
                "doctor_id": DOCTOR,
                "device_key": DEVICE,
                "start_at": slot["start_at"],
            },
            requires_verification=True,
        ),
        state_action="update_active",
        response_goal="booking_proposal",
        facts={
            "requested_service_ids": [A, B],
            "booking_proposal_required": True,
            "_verified_booking_proposal_candidate": True,
        },
    )

    prepared, proposal = _prepare_booking_proposal(
        candidate,
        operation=TurnOperation(
            type="book",
            entities=TurnEntities(),
            execution_intent="informational",
        ),
        reads=reads,
        current_task=_task(),
        db=object(),
        workspace=SimpleNamespace(id="workspace", timezone="Africa/Cairo"),
        patient=SimpleNamespace(id="patient"),
        catalog=_catalog(doctor_common=False),
        now=NOW,
        turn_id="proposal-rejected",
        additional_service_resolver=lambda _proposal: pytest.fail(
            "invalid proposal must reject before additional service resolution"
        ),
    )

    assert proposal is None
    assert prepared.disposition == "blocked"
    assert prepared.facts["booking_proposal_main_service_mismatch"] is True
    assert prepared.write_intent is None
    assert prepared.response_goal == "clarification"


def test_disappeared_slot_clears_old_authority_and_presents_only_fresh_alternatives() -> None:
    task = _task()
    planned = _selection_plan(task=task)
    assert planned is not None
    step = planned[0].steps[0]
    verification = VerificationFacts(exact_slot_match_count=0)
    advanced = advance_step_after_verification(step, verification)
    alternative = _slot(service_id=B, time="20:30")
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=True,
                payload={"slots": [], "nearest_alternative_slots": [alternative]},
            )
        ],
        verification=verification,
    )
    transition = apply_step_state(
        task,
        step=advanced,
        operation=_operation(),
        reads=reads,
        now=NOW,
        turn_id="slot-disappeared",
        catalog=_catalog(),
    )
    state = transition.active_task
    assert isinstance(state, BookingTaskState)
    assert advanced.response_goal == "requested_time_unavailable"
    assert state.proposal is None
    assert state.derived.selected_slot_ref is None
    assert state.status == "awaiting_slot"
    assert state.option_snapshot is not None
    assert len(state.option_snapshot.options) == 1
    assert state.option_snapshot.options[0].payload["start_time_24h"] == "20:30"
    assert state.write_authorization.authorized is False


def test_disappeared_slot_without_alternatives_leaves_valid_collecting_state() -> None:
    task = _task()
    planned = _selection_plan(task=task)
    assert planned is not None
    step = planned[0].steps[0]
    verification = VerificationFacts(exact_slot_match_count=0)
    advanced = advance_step_after_verification(step, verification)
    reads = ReadExecutionBundle(
        results=[ReadResult(kind="availability", ok=True, payload={"slots": []})],
        verification=verification,
    )
    state = apply_step_state(
        task,
        step=advanced,
        operation=_operation(),
        reads=reads,
        now=NOW,
        turn_id="slot-gone",
        catalog=_catalog(),
    ).active_task
    assert isinstance(state, BookingTaskState)
    assert state.status == "collecting"
    assert state.option_snapshot is None
    assert state.derived.selected_slot_ref is None
    assert state.proposal is None
    assert state.write_authorization.authorized is False


def test_multiple_fresh_exact_matches_fail_closed_without_selected_authority() -> None:
    task = _task()
    planned = _selection_plan(task=task)
    assert planned is not None
    step = planned[0].steps[0]
    verification = VerificationFacts(exact_slot_match_count=2)
    advanced = advance_step_after_verification(step, verification)
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=True,
                payload={"slots": [_slot(service_id=B), _slot(service_id=B)]},
            )
        ],
        verification=verification,
    )
    state = apply_step_state(
        task,
        step=advanced,
        operation=_operation(),
        reads=reads,
        now=NOW,
        turn_id="slot-ambiguous",
        catalog=_catalog(),
    ).active_task
    assert isinstance(state, BookingTaskState)
    assert advanced.disposition == "clarify"
    assert state.proposal is None
    assert state.derived.selected_slot_ref is None
    assert state.write_authorization.authorized is False


@pytest.mark.parametrize(
    ("execution_intent", "expected_goal"),
    [("informational", "booking_proposal"), ("execute", "booking_completed")],
)
def test_single_service_fresh_verification_respects_booking_authority(
    execution_intent: str,
    expected_goal: str,
) -> None:
    task = _task(grouped=False)
    planned = _selection_plan(grouped=False, task=task)
    assert planned is not None
    step = planned[0].steps[0]
    fresh_slot = _slot(service_id=A)
    verification = VerificationFacts(
        exact_slot_match_count=1,
        verified_parameters={
            "service_id": A,
            "branch_id": BRANCH,
            "doctor_id": DOCTOR,
            "device_key": DEVICE,
            "start_at": fresh_slot["start_at"],
        },
    )
    reads = ReadExecutionBundle(
        results=[ReadResult(kind="availability", ok=True, payload={"slots": [fresh_slot]})],
        verification=verification,
    )
    advanced = advance_step_after_verification(step, verification)
    operation = _operation().model_copy(update={"execution_intent": execution_intent})
    prepared, proposal = _prepare_booking_proposal(
        advanced,
        operation=operation,
        reads=reads,
        current_task=task,
        db=object(),
        workspace=SimpleNamespace(id="workspace", timezone="Africa/Cairo"),
        patient=SimpleNamespace(id="patient"),
        catalog=_catalog(grouped=False),
        now=NOW,
        turn_id="single-selected",
        additional_service_resolver=lambda _proposal: [],
    )
    assert proposal is not None
    if execution_intent == "execute":
        assert prepared.disposition == "write_ready"
        assert prepared.response_goal == expected_goal
        assert prepared.write_intent is not None
        assert prepared.write_intent.kind == "booking_proposal"
        assert prepared.write_intent.authorized is True
        return

    assert prepared.response_goal == expected_goal
    assert prepared.write_intent is None
    transitioned = apply_step_state(
        task,
        step=prepared,
        operation=operation,
        reads=reads,
        now=NOW,
        turn_id="single-selected",
        catalog=_catalog(grouped=False),
    ).active_task
    assert isinstance(transitioned, BookingTaskState)
    final = _attach_verified_booking_proposal_to_task(
        transitioned,
        proposal=proposal,
        selected_option_ref="opt_1",
        catalog=_catalog(grouped=False),
        timezone_name="Africa/Cairo",
    )
    assert final.status == "awaiting_confirmation"
    assert final.requested_service_ids == [A]
    assert final.proposal is not None
    assert final.proposal.service_ids == [A]
    assert final.derived.selected_slot_ref == "opt_1"
    assert final.constraints.time is not None and final.constraints.time.start_time == "20:00"
    assert final.write_authorization.authorized is False

    reloaded = _decode_flow_task(
        SimpleNamespace(
            entity_state={"agent_core_v2": _task_namespace(final)},
            flow_type="booking",
        )
    )
    assert isinstance(reloaded, BookingTaskState)
    assert reloaded.requested_service_ids == [A]
    assert reloaded.proposal is not None
    assert reloaded.proposal.service_ids == [A]

    # Semantic cancel_active represents a customer declining the pending proposal.
    decline_step = PlanStep(
        operation_index=0,
        operation_type="cancel_active",
        disposition="state_update",
        state_action="cancel_active",
        response_goal="active_task_cancelled",
    )
    declined = apply_step_state(
        reloaded,
        step=decline_step,
        operation=TurnOperation(
            type="cancel_active",
            execution_intent="informational",
            entities=TurnEntities(),
        ),
        reads=None,
        now=NOW,
        turn_id="single-declined",
        catalog=_catalog(grouped=False),
    )
    assert decline_step.write_intent is None
    assert declined.changed is True
    assert declined.active_task is None


@pytest.mark.parametrize("authorized", [False, True])
def test_stateful_h3_selection_reuses_production_boundary_and_respects_booking_authority(
    monkeypatch: pytest.MonkeyPatch,
    authorized: bool,
) -> None:
    base = V2FixtureEnvironment()
    catalog = deepcopy(base.catalog)
    catalog["primary_branch_id"] = "single-location"
    catalog["branches"] = [
        {
            "id": "single-location",
            "name": "Main Branch",
            "timezone": "Africa/Cairo",
            "working_hours": [
                {"weekday": weekday, "start": "10:00", "end": "22:00"}
                for weekday in range(7)
            ],
        }
    ]
    for doctor in catalog.get("doctors", []):
        if isinstance(doctor, dict):
            doctor["branch_ids"] = ["single-location"]
    env = V2FixtureEnvironment(
        catalog=catalog,
        clinic_info=deepcopy(base.clinic_info),
        profile=deepcopy(base.profile),
        history=deepcopy(base.history),
        slots=deepcopy(base.slots),
        package_offers=deepcopy(base.package_offers),
        refund_quotes=deepcopy(base.refund_quotes),
    )
    selected_slot = next(
        dict(row)
        for row in env.slots
        if row.get("service_id") == "svc-bikini"
        and row.get("doctor_id") == "doc-maryam"
        and row.get("laser_device_key") == "candela_gentle"
        and str(row.get("start_local") or "")[11:16] == "20:00"
    )
    task = BookingTaskState(
        status="awaiting_slot",
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=authorized,
            source_turn_id="booking-request" if authorized else None,
            granted_at=NOW if authorized else None,
        ),
        constraints=CustomerConstraints(
            service_id="svc-bikini",
            branch_id="single-location",
            doctor_id="doc-maryam",
            device_key="candela_gentle",
            date=DateConstraint(mode="exact", start_date="2026-09-12"),
        ),
        requested_service_ids=["svc-underarm", "svc-bikini"],
        service_device_keys={
            "svc-underarm": "candela_gentle",
            "svc-bikini": "candela_gentle",
        },
        option_snapshot=OptionSnapshot(
            snapshot_id="ra-h3-slot",
            purpose="booking_slot",
            task_version=4,
            created_at=NOW - timedelta(minutes=5),
            expires_at=NOW + timedelta(minutes=10),
            options=[OptionChoice(ref="slot-1", label="20:00", payload=selected_slot)],
        ),
        version=4,
    )
    reference_context = {
        "operation_type": "availability",
        "requested_service_ids": ["svc-underarm", "svc-bikini"],
        "multi_service_single_appointment": True,
        "date": {"mode": "exact", "start_date": "2026-09-12", "end_date": None},
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "index": 1,
                "concrete": True,
                "service_id": "svc-bikini",
                "start_time_24h": "20:00",
                "start_local": selected_slot["start_local"],
                "slot": selected_slot,
            }
        ],
    }
    monkeypatch.setattr(
        stateful_harness,
        "interpret_availability_reference_turn",
        lambda **kwargs: ReferenceInterpretation(
            decision=ReferenceDecision(
                action="select_presented_option",
                option_ref="opt_1",
                exact_time=None,
                selection_scope="none",
            ),
            model_name="deterministic-test",
        ),
    )
    monkeypatch.setattr(
        stateful_harness,
        "interpret_customer_turn_v2",
        lambda **kwargs: pytest.fail("verified booking selection must bypass the full interpreter"),
    )

    result = stateful_harness.run_v2_stateful_fixture_turn(
        history=[HumanMessage(content="?????? 8 ???? ??????")],
        local_now=NOW,
        timezone_name="Africa/Cairo",
        env=env,
        active_task=task,
        availability_reference_context=reference_context,
        simulate_writes=True,
        turn_id="ra-h3-phase3",
    )

    assert result.reference_action == "select_presented_option"
    assert result.selected_option_ref == "opt_1"
    assert result.full_interpreter_called is False
    assert "???? ??? ???? ??" not in result.reply
    if authorized:
        assert result.active_task is None
        assert result.outcomes[-1].response_goal == "booking_completed"
        assert result.traces[-1].simulated_write == "booking_proposal"
    else:
        assert isinstance(result.active_task, BookingTaskState)
        assert result.active_task.status == "awaiting_confirmation"
        assert result.active_task.requested_service_ids == ["svc-underarm", "svc-bikini"]
        assert result.active_task.constraints.doctor_id == "doc-maryam"
        assert result.active_task.constraints.device_key == "candela_gentle"
        assert result.active_task.constraints.date is not None
        assert result.active_task.constraints.date.start_date == "2026-09-12"
        assert result.active_task.constraints.time is not None
        assert result.active_task.constraints.time.start_time == "20:00"
        assert result.active_task.proposal is not None
        assert result.active_task.proposal.service_ids == ["svc-underarm", "svc-bikini"]
        assert result.active_task.proposal.doctor_id == "doc-maryam"
        assert result.active_task.proposal.device_key == "candela_gentle"
        assert result.active_task.proposal.start_at.isoformat() == "2026-09-12T20:00:00+03:00"
        assert result.outcomes[-1].response_goal == "booking_proposal"
        assert result.traces[-1].simulated_write is None
        assert result.active_task.write_authorization.authorized is False


def test_review_first_booking_revokes_prior_authority_then_explicit_proceed_revalidates_and_writes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(grouped=False).model_copy(
        update={
            "write_authorization": WriteAuthorization(
                operation="booking",
                authorized=True,
                source_turn_id="original-booking-request",
                granted_at=NOW - timedelta(minutes=5),
            ),
            "constraints": CustomerConstraints(
                service_id=A,
                doctor_id=DOCTOR,
                branch_id=BRANCH,
                device_key=DEVICE,
                date=DateConstraint(mode="exact", start_date="2026-09-12"),
                time=TimeConstraint(mode="exact", start_time="20:00"),
            ),
            "derived": DerivedBookingState(
                availability_snapshot_id="snapshot-1",
                commercial_basis_presented_key=f"{A}|{DEVICE}|standalone",
            ),
        }
    )
    def understanding(execution_intent: str) -> TiaTurnUnderstanding:
        return TiaTurnUnderstanding(
            operations=[
                TurnOperation(
                    type="book",
                    entities=TurnEntities(),
                    execution_intent=execution_intent,
                    active_task_relationship="continue",
                    continues_previous=True,
                )
            ]
        )

    understandings = iter([understanding("informational"), understanding("execute")])
    monkeypatch.setattr(
        stateful_harness,
        "interpret_customer_turn_v2",
        lambda **_kwargs: next(understandings),
    )
    monkeypatch.setattr(
        stateful_harness,
        "compose_v2_customer_reply",
        lambda **_kwargs: ("Deterministic test reply.", "deterministic-test"),
    )
    env = V2FixtureEnvironment(
        catalog=_catalog(grouped=False),
        slots=[_slot(service_id=A)],
    )

    review = stateful_harness.run_v2_stateful_fixture_turn(
        history=[HumanMessage(content="عايزة أراجع تفاصيل الحجز قبل ما أأكد")],
        local_now=NOW,
        timezone_name="Africa/Cairo",
        env=env,
        active_task=task,
        simulate_writes=True,
        turn_id="review-before-confirmation",
    )

    assert isinstance(review.active_task, BookingTaskState)
    assert review.active_task.write_authorization.authorized is False
    assert review.active_task.write_authorization.source_turn_id is None
    assert review.active_task.proposal is None
    assert review.active_task.constraints.service_id == A
    assert review.active_task.constraints.date == DateConstraint(
        mode="exact", start_date="2026-09-12"
    )
    assert review.active_task.constraints.time == TimeConstraint(
        mode="exact", start_time="20:00"
    )
    assert any("availability" in trace.read_kinds for trace in review.traces)
    assert all(trace.simulated_write is None for trace in review.traces)

    proceed = stateful_harness.run_v2_stateful_fixture_turn(
        history=[HumanMessage(content="تمام احجز")],
        local_now=NOW,
        timezone_name="Africa/Cairo",
        env=env,
        active_task=review.active_task,
        simulate_writes=True,
        turn_id="explicit-proceed-after-review",
    )

    assert proceed.active_task is None
    assert any("availability" in trace.read_kinds for trace in proceed.traces)
    assert sum(trace.simulated_write is not None for trace in proceed.traces) == 1
    assert proceed.traces[-1].simulated_write == "booking"
    assert proceed.outcomes[-1].response_goal == "booking_completed"


def test_explicit_booking_authority_survives_missing_date_and_writes_once_after_verified_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = V2FixtureEnvironment()
    task = BookingTaskState(
        status="collecting",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(
            service_id="svc-underarm",
            branch_id="single-location",
            doctor_id="doc-maryam",
            device_key="candela_gentle",
            time=TimeConstraint(mode="exact", start_time="20:00"),
        ),
        derived=DerivedBookingState(
            commercial_basis_presented_key="svc-underarm|candela_gentle|standalone",
            commercial_basis_presented_device_key="candela_gentle",
        ),
        version=3,
    )
    understandings = iter(
        [
            TiaTurnUnderstanding(
                operations=[
                    TurnOperation(
                        type="book",
                        execution_intent="execute",
                        active_task_relationship="continue",
                        continues_previous=True,
                        entities=TurnEntities(),
                    )
                ]
            ),
            TiaTurnUnderstanding(
                operations=[
                    TurnOperation(
                        type="continue_active",
                        execution_intent="execute",
                        active_task_relationship="continue",
                        continues_previous=True,
                        entities=TurnEntities(
                            date=DateConstraint(
                                mode="exact",
                                start_date="2026-09-12",
                            )
                        ),
                    )
                ]
            ),
        ]
    )
    monkeypatch.setattr(
        stateful_harness,
        "interpret_customer_turn_v2",
        lambda **kwargs: next(understandings),
    )

    def deterministic_reply(**kwargs: object) -> tuple[str, str]:
        outcomes = kwargs.get("outcomes", [])
        if any(
            getattr(outcome, "response_goal", None) == "booking_completed"
            for outcome in outcomes
        ):
            return "Your underarm appointment is booked.", "deterministic-test"
        return "What date would you like?", "deterministic-test"

    monkeypatch.setattr(stateful_harness, "compose_v2_customer_reply", deterministic_reply)

    first = stateful_harness.run_v2_stateful_fixture_turn(
        history=[HumanMessage(content="Book the appointment")],
        local_now=NOW,
        timezone_name="Africa/Cairo",
        env=env,
        active_task=task,
        simulate_writes=True,
        turn_id="explicit-booking-missing-date",
    )

    assert isinstance(first.active_task, BookingTaskState)
    assert first.active_task.write_authorization.authorized is True
    assert first.active_task.write_authorization.source_turn_id == "explicit-booking-missing-date"
    assert first.active_task.constraints.service_id == "svc-underarm"
    assert first.active_task.constraints.date is None
    assert first.active_task.constraints.time == TimeConstraint(
        mode="exact",
        start_time="20:00",
    )
    assert first.outcomes[-1].status == "needs_input"
    assert first.outcomes[-1].response_goal == "clarification"
    assert first.outcomes[-1].facts["needed"] == "date"
    assert all(trace.simulated_write is None for trace in first.traces)

    second = stateful_harness.run_v2_stateful_fixture_turn(
        history=[HumanMessage(content="September 12")],
        local_now=NOW,
        timezone_name="Africa/Cairo",
        env=env,
        active_task=first.active_task,
        simulate_writes=True,
        turn_id="explicit-booking-date-supplied",
    )

    assert second.active_task is None
    assert sum(trace.simulated_write is not None for trace in second.traces) == 1
    assert second.traces[-1].simulated_write == "booking"
    assert second.traces[-1].disposition_after == "write_ready"
    assert "availability" in second.traces[-1].read_kinds
    assert second.outcomes[-1].status == "completed"
    assert second.outcomes[-1].response_goal == "booking_completed"
    assert second.outcomes[-1].action_result is not None
    assert second.outcomes[-1].action_result["ok"] is True
    assert all(outcome.response_goal != "booking_proposal" for outcome in second.outcomes)
    assert second.reply == "Your underarm appointment is booked."
    assert "confirm" not in second.reply.lower()
