from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.models.conversation_flow_state import ConversationFlowState
from app.services.agent_v2 import state_persistence
from app.services.agent_v2.planner import PlannerContext, PlanStep, ReadRequest, plan_turn
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

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _flow(task: BookingTaskState) -> ConversationFlowState:
    return ConversationFlowState(
        workspace_id=uuid4(),
        conversation_id=uuid4(),
        patient_id=uuid4(),
        flow_type="booking",
        status=state_persistence._flow_status_for_task(task),
        is_active=True,
        capabilities=["appointment_creation"],
        entity_state=state_persistence._entity_state_for_task(task),
        missing_information=[],
        pending_action={},
        option_snapshot=state_persistence._option_snapshot_for_task(task),
        last_decision=state_persistence._last_decision_for_task(task),
        version=3,
        expires_at=NOW + timedelta(hours=1),
        last_turn_at=NOW,
    )


def _proposal(*, task_version: int, expires_at: datetime) -> BookingProposal:
    start = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
    return BookingProposal(
        proposal_id="proposal-1",
        patient_id="patient-1",
        service_ids=["svc-underarm", "svc-bikini"],
        main_service_id="svc-bikini",
        additional_service_ids=["svc-underarm"],
        branch_id="branch-1",
        doctor_id="doc-maryam",
        start_at=start,
        end_at=start + timedelta(minutes=45),
        device_key="candela_gentle",
        additional_service_device_keys={"svc-underarm": "candela_gentle"},
        package_usage="unspecified",
        task_version=task_version,
        proposal_version=2,
        created_at=NOW - timedelta(minutes=1),
        expires_at=expires_at,
    )


def test_phase0_legacy_collecting_booking_shape_round_trips() -> None:
    task = BookingTaskState(
        status="collecting",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id="svc-underarm"),
        requested_service_ids=["svc-underarm", "svc-bikini"],
    )

    restored = state_persistence._decode_flow_task(_flow(task))

    assert isinstance(restored, BookingTaskState)
    assert restored.status == "collecting"
    assert restored.option_snapshot is None
    assert restored.derived.selected_slot_ref is None
    assert restored.proposal is None
    assert restored.requested_service_ids == ["svc-underarm", "svc-bikini"]


def test_phase0_legacy_awaiting_choice_booking_slot_shape_round_trips() -> None:
    snapshot = OptionSnapshot(
        snapshot_id="snapshot-1",
        purpose="booking_slot",
        task_version=2,
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=14),
        options=[
            OptionChoice(
                ref="slot-1",
                payload={
                    "service_id": "svc-bikini",
                    "doctor_id": "doc-maryam",
                    "device_key": "candela_gentle",
                    "start_at": "2026-10-18T20:00:00+00:00",
                    "start_time_24h": "20:00",
                },
            )
        ],
    )
    task = BookingTaskState(
        status="awaiting_choice",
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW - timedelta(minutes=2),
        ),
        constraints=CustomerConstraints(
            service_id="svc-bikini",
            doctor_id="doc-maryam",
            branch_id="branch-1",
            device_key="candela_gentle",
        ),
        requested_service_ids=["svc-underarm", "svc-bikini"],
        service_device_keys={
            "svc-underarm": "candela_gentle",
            "svc-bikini": "candela_gentle",
        },
        option_snapshot=snapshot,
        derived=DerivedBookingState(availability_snapshot_id="snapshot-1"),
        version=2,
    )

    restored = state_persistence._decode_flow_task(_flow(task))

    assert isinstance(restored, BookingTaskState)
    assert restored.status == "awaiting_choice"
    assert restored.option_snapshot is not None
    assert restored.option_snapshot.purpose == "booking_slot"
    assert restored.derived.availability_snapshot_id == "snapshot-1"
    assert restored.derived.selected_slot_ref is None
    assert restored.proposal is None
    assert restored.requested_service_ids == ["svc-underarm", "svc-bikini"]


def test_phase0_legacy_ready_current_proposal_shape_round_trips() -> None:
    proposal = _proposal(task_version=4, expires_at=NOW + timedelta(minutes=14))
    task = BookingTaskState(
        status="ready",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(
            service_id="svc-bikini",
            doctor_id="doc-maryam",
            branch_id="branch-1",
            device_key="candela_gentle",
        ),
        requested_service_ids=["svc-underarm", "svc-bikini"],
        service_device_keys={
            "svc-underarm": "candela_gentle",
            "svc-bikini": "candela_gentle",
        },
        proposal=proposal,
        derived=DerivedBookingState(selected_slot_ref="slot-1"),
        version=4,
    )

    restored = state_persistence._decode_flow_task(_flow(task))

    assert isinstance(restored, BookingTaskState)
    assert restored.status == "ready"
    assert restored.option_snapshot is None
    assert restored.derived.selected_slot_ref == "slot-1"
    assert restored.proposal is not None
    assert restored.proposal.is_current(task_version=restored.version, now=NOW)
    assert restored.write_authorization.authorized is False


def test_phase0_expired_booking_proposal_confirmation_never_writes() -> None:
    task = BookingTaskState(
        status="ready",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id="svc-bikini"),
        requested_service_ids=["svc-underarm", "svc-bikini"],
        proposal=_proposal(task_version=4, expires_at=NOW - timedelta(seconds=1)),
        version=4,
    )
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="confirm_booking_proposal",
                execution_intent="execute",
                entities=TurnEntities(),
            )
        ]
    )
    step = plan_turn(
        turn,
        PlannerContext(
            semantic_context=SemanticContext(model_input={}, reference_map={}, server_metadata={}),
            active_task=task,
            now=NOW,
        ),
    ).steps[0]

    assert step.disposition == "blocked"
    assert step.write_intent is None
    assert step.response_goal == "clarification"
    assert step.facts["booking_proposal_expired"] is True


def test_phase0_side_read_preserves_ready_proposal_exactly() -> None:
    task = BookingTaskState(
        status="ready",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id="svc-bikini"),
        requested_service_ids=["svc-underarm", "svc-bikini"],
        proposal=_proposal(task_version=4, expires_at=NOW + timedelta(minutes=14)),
        version=4,
    )
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        state_action="none",
        reads=[ReadRequest(kind="service_catalog")],
    )
    transition = apply_step_state(
        task,
        step=step,
        operation=TurnOperation(
            type="pricing",
            execution_intent="informational",
            entities=TurnEntities(),
        ),
        reads=None,
        now=NOW,
        turn_id="turn-side-read",
    )

    assert transition.active_task == task
    assert transition.changed is False
    assert transition.reason == "side_read_preserved"
