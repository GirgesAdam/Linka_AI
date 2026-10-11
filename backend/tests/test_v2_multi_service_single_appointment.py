from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.multi_service_booking import (
    AdditionalServiceResolution,
    execute_booking_proposal,
    select_main_service_id,
)
from app.services.agent_v2.planner import (
    PlannerContext,
    PlanStep,
    TurnPlan,
    WriteIntent,
    plan_turn,
)
from app.services.agent_v2.state import (
    BookingProposal,
    BookingTaskState,
    CustomerConstraints,
    WriteAuthorization,
)
from app.services.agent_v2.state_rules import (
    apply_booking_requested_services_change,
    attach_booking_proposal,
    booking_proposal_is_current,
    invalidate_booking_proposal,
)
from app.services.agent_v2.turn_normalization import (
    collapse_booking_service_sets,
    guard_multiple_booking_writes,
    merge_same_visit_booking_operations,
    tag_booking_service_sets,
)

NOW = datetime(2026, 10, 11, 16, 0, tzinfo=UTC)
PATIENT_ID = "11111111-1111-1111-1111-111111111111"
BRANCH_ID = "22222222-2222-2222-2222-222222222222"
DOCTOR_ID = "33333333-3333-3333-3333-333333333333"
UNDERARM = "44444444-4444-4444-4444-444444444444"
BIKINI = "55555555-5555-5555-5555-555555555555"
COMBO = "66666666-6666-6666-6666-666666666666"
FULL_ARMS = "77777777-7777-7777-7777-777777777777"


def _catalog() -> dict[str, object]:
    return {
        "services": [
            {"id": UNDERARM, "name": "Underarm", "duration_minutes": 20},
            {"id": BIKINI, "name": "Bikini", "duration_minutes": 25},
            {"id": COMBO, "name": "Underarm + Bikini", "duration_minutes": 45},
            {"id": FULL_ARMS, "name": "Full Arms", "duration_minutes": 30},
        ],
        "branches": [{"id": BRANCH_ID, "name": "New Cairo"}],
        "doctors": [
            {
                "id": DOCTOR_ID,
                "name": "Doctor",
                "service_ids": [UNDERARM, BIKINI, COMBO, FULL_ARMS],
                "branch_ids": [BRANCH_ID],
            }
        ],
    }


def _proposal(*, services: list[str] | None = None) -> BookingProposal:
    service_ids = services or [COMBO, FULL_ARMS]
    main = select_main_service_id(_catalog(), service_ids)
    return BookingProposal(
        proposal_id="proposal-1",
        patient_id=PATIENT_ID,
        service_ids=service_ids,
        main_service_id=main,
        additional_service_ids=[item for item in service_ids if item != main],
        branch_id=BRANCH_ID,
        doctor_id=DOCTOR_ID,
        start_at=NOW + timedelta(days=2),
        end_at=NOW + timedelta(days=2, minutes=45),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


def _task(*, proposal: BookingProposal | None = None) -> BookingTaskState:
    service_ids = list(proposal.service_ids) if proposal is not None else [COMBO, FULL_ARMS]
    task = BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(service_id=COMBO, branch_id=BRANCH_ID, doctor_id=DOCTOR_ID),
        requested_service_ids=service_ids,
    )
    return attach_booking_proposal(task, proposal=proposal) if proposal is not None else task


def test_longest_service_is_main_and_request_order_breaks_ties() -> None:
    assert select_main_service_id(_catalog(), [FULL_ARMS, COMBO, UNDERARM]) == COMBO
    tied = {
        "services": [
            {"id": UNDERARM, "duration_minutes": 45},
            {"id": BIKINI, "duration_minutes": 45},
        ]
    }
    assert select_main_service_id(tied, [BIKINI, UNDERARM]) == BIKINI


def test_combo_only_set_stays_one_canonical_service() -> None:
    context = build_semantic_context(_catalog())
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=["S3"],
                        candidate_mode="set",
                    )
                ),
                execution_intent="execute",
            )
        ],
        safety_signals=[],
    )
    collapsed, sets = collapse_booking_service_sets(
        turn,
        semantic_context=context,
        catalog=_catalog(),
    )
    assert sets == {0: [COMBO]}
    assert collapsed.operations[0].entities.service is not None
    assert collapsed.operations[0].entities.service.ref == "S3"


def test_combo_plus_full_arms_selects_combo_as_main_without_duplication() -> None:
    context = build_semantic_context(_catalog())
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=["S3", "S4"],
                        candidate_mode="set",
                    )
                ),
                execution_intent="execute",
            )
        ],
        safety_signals=[],
    )
    collapsed, sets = collapse_booking_service_sets(
        turn,
        semantic_context=context,
        catalog=_catalog(),
    )
    assert sets == {0: [COMBO, FULL_ARMS]}
    assert collapsed.operations[0].entities.service is not None
    assert context.resolve(collapsed.operations[0].entities.service.ref, expected_kind="service") == COMBO


def test_legacy_same_visit_operations_are_merged_before_planning() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(service=EntityReference(ref="S3")),
                execution_intent="execute",
            ),
            TurnOperation(
                type="book",
                entities=TurnEntities(service=EntityReference(ref="S4")),
                execution_intent="execute",
            ),
        ],
        safety_signals=[],
    )
    merged = merge_same_visit_booking_operations(turn)
    assert len(merged.operations) == 1
    service = merged.operations[0].entities.service
    assert service is not None
    assert service.candidate_mode == "set"
    assert service.candidate_refs == ["S3", "S4"]


def test_real_interpreter_scheduling_entities_are_hashable_for_same_visit_merge() -> None:
    shared_entities = {
        "doctor": EntityReference(ref="D1"),
        "branch": EntityReference(ref="B1"),
        "date": DateConstraint(mode="exact", start_date="2026-10-13"),
        "time": TimeConstraint(mode="exact", start_time="11:00"),
    }
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(ref=service_ref),
                    **shared_entities,
                ),
                execution_intent="execute",
            )
            for service_ref in ("S3", "S4")
        ],
        safety_signals=[],
    )

    merged = merge_same_visit_booking_operations(turn)

    assert len(merged.operations) == 1
    service = merged.operations[0].entities.service
    assert service is not None
    assert service.candidate_refs == ["S3", "S4"]

def test_multiple_booking_write_fallback_fails_closed() -> None:
    steps = [
        PlanStep(
            operation_index=index,
            operation_type="book",
            disposition="read",
            write_intent=WriteIntent(
                kind="booking",
                authorized=True,
                parameters={"service_id": service_id},
            ),
        )
        for index, service_id in enumerate((COMBO, FULL_ARMS))
    ]
    guarded = guard_multiple_booking_writes(TurnPlan(steps=steps))
    assert len(guarded.steps) == 1
    assert guarded.steps[0].disposition == "clarify"
    assert guarded.steps[0].write_intent is None
    assert guarded.steps[0].facts["multiple_appointment_write_blocked"] is True


def test_service_correction_replaces_set_and_invalidates_proposal() -> None:
    task = _task(proposal=_proposal())
    assert task.proposal is not None
    corrected = apply_booking_requested_services_change(task, service_ids=[COMBO])
    assert corrected.requested_service_ids == [COMBO]
    assert corrected.proposal is None
    assert corrected.grouped is None
    assert FULL_ARMS not in corrected.requested_service_ids


def test_edit_invalidates_proposal_version() -> None:
    task = _task(proposal=_proposal())
    assert task.proposal is not None
    assert booking_proposal_is_current(
        task,
        proposal_id=task.proposal.proposal_id,
        now=NOW + timedelta(minutes=1),
    )
    edited = invalidate_booking_proposal(task)
    assert edited.proposal is None
    assert edited.version == task.version + 1


def test_text_confirmation_binds_current_server_proposal_without_rebuilding() -> None:
    context = build_semantic_context(_catalog())
    task = _task(proposal=_proposal())
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="confirm_booking_proposal",
                entities=TurnEntities(),
                execution_intent="execute",
            )
        ],
        safety_signals=[],
    )
    plan = plan_turn(
        turn,
        PlannerContext(
            semantic_context=context,
            active_task=task,
            now=NOW + timedelta(minutes=1),
        ),
    )
    step = plan.steps[0]
    assert step.disposition == "write_ready"
    assert step.reads == []
    assert step.write_intent is not None
    assert step.write_intent.kind == "booking_proposal"
    assert step.write_intent.parameters["proposal_id"] == task.proposal.proposal_id
    snapshot = step.write_intent.parameters["proposal"]
    assert isinstance(snapshot, dict)
    assert snapshot["service_ids"] == [COMBO, FULL_ARMS]
    assert snapshot["main_service_id"] == COMBO


def test_tagged_plan_keeps_ordered_service_set_in_backend_facts() -> None:
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters={"service_id": COMBO},
        ),
    )
    tagged = tag_booking_service_sets(
        TurnPlan(steps=[step]),
        service_sets={0: [COMBO, FULL_ARMS]},
    )
    assert tagged.steps[0].facts["requested_service_ids"] == [COMBO, FULL_ARMS]
    assert tagged.steps[0].facts["booking_proposal_required"] is True


def test_proposal_executor_creates_one_appointment_then_additional_rows(monkeypatch) -> None:
    proposal = _proposal()
    appointment = SimpleNamespace(
        id=UUID("88888888-8888-8888-8888-888888888888"),
        service_id=UUID(COMBO),
        status="pending",
        duration_minutes=45,
    )
    created: list[str] = []

    monkeypatch.setattr(
        "app.services.agent_v2.multi_service_booking.resolve_additional_services",
        lambda *_args, **_kwargs: [
            AdditionalServiceResolution(service_id=UUID(FULL_ARMS), device_key=None)
        ],
    )
    monkeypatch.setattr(
        "app.services.agent_v2.multi_service_booking.resolve_booking_package",
        lambda *_args, **_kwargs: SimpleNamespace(
            package_id=None,
            package_used=False,
            package_name=None,
        ),
    )

    def fake_create(*_args, **_kwargs):
        created.append("appointment")
        return appointment

    def fake_add(*_args, **kwargs):
        created.append(f"additional:{kwargs['service_id']}")
        return SimpleNamespace(
            id=UUID("99999999-9999-9999-9999-999999999999"),
            service_id=kwargs["service_id"],
        )

    monkeypatch.setattr(
        "app.services.agent_v2.multi_service_booking.create_appointment_operation",
        fake_create,
    )
    monkeypatch.setattr(
        "app.services.agent_v2.multi_service_booking.add_additional_service",
        fake_add,
    )
    result = execute_booking_proposal(
        SimpleNamespace(),
        workspace=SimpleNamespace(id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")),
        patient=SimpleNamespace(id=UUID(PATIENT_ID), status="active"),
        proposal=proposal,
        idempotency_key="test-proposal",
    )
    assert created == ["appointment", f"additional:{FULL_ARMS}"]
    assert result["main_service_id"] == COMBO
    assert result["additional_service_ids"] == [FULL_ARMS]
    assert result["reserved_duration_minutes"] == 45
