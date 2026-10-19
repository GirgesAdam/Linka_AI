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
from app.agents.v2.turn_contract import DateConstraint, Selection, TurnEntities, TurnOperation
from app.services.agent_v2.orchestrator import (
    _attach_verified_booking_proposal_to_task,
    _prepare_booking_proposal,
    _verified_booking_selection_plan,
)
from app.services.agent_v2.planner import VerificationFacts, advance_step_after_verification
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    OptionChoice,
    OptionSnapshot,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state
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


def test_single_service_fresh_verification_builds_proposal_without_db_write_authority() -> None:
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
    prepared, proposal = _prepare_booking_proposal(
        advanced,
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
    assert prepared.response_goal == "booking_proposal"
    assert prepared.write_intent is None
    transitioned = apply_step_state(
        task,
        step=prepared,
        operation=_operation(),
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
    assert final.proposal is not None
    assert final.proposal.service_ids == [A]
    assert final.derived.selected_slot_ref == "opt_1"
    assert final.constraints.time is not None and final.constraints.time.start_time == "20:00"
    assert final.write_authorization.authorized is False


def test_stateful_h3_selection_reuses_production_boundary_and_reaches_proposal(
    monkeypatch: pytest.MonkeyPatch,
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
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
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
    assert "???? ??? ???? ??" not in result.reply
    assert result.traces[-1].simulated_write is None
    assert result.active_task.write_authorization.authorized is False
