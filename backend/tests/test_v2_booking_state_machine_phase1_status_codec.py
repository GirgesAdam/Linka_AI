from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.v2.turn_contract import TurnEntities, TurnOperation
from app.models.conversation_flow_state import ConversationFlowState
from app.services.agent_v2 import state_persistence
from app.services.agent_v2.planner import PlanStep, WriteIntent
from app.services.agent_v2.state import (
    BookingProposal,
    BookingTaskState,
    CustomerConstraints,
    OptionChoice,
    OptionSnapshot,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state
from app.services.agent_v2.state_rules import (
    attach_booking_proposal,
    attach_option_snapshot,
    invalidate_booking_proposal,
    record_booking_commercial_basis_presented,
    record_booking_device_compatibility,
    record_booking_doctor_compatibility,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _proposal(*, task_version: int = 3) -> BookingProposal:
    start = NOW + timedelta(days=2)
    return BookingProposal(
        proposal_id="proposal-1",
        patient_id="patient-1",
        service_ids=["service-1"],
        main_service_id="service-1",
        branch_id="branch-1",
        doctor_id="doctor-1",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        task_version=task_version,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


def _snapshot(*, purpose: str, task_version: int = 3) -> OptionSnapshot:
    return OptionSnapshot(
        snapshot_id=f"snapshot-{purpose}",
        purpose=purpose,
        task_version=task_version,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        options=[OptionChoice(ref="option-1", payload={"start_time_24h": "20:00"})],
    )


def _booking_collecting() -> BookingTaskState:
    return BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking"),
        constraints=CustomerConstraints(service_id="service-1"),
        requested_service_ids=["service-1"],
        version=3,
    )


def _booking_awaiting_slot() -> BookingTaskState:
    return BookingTaskState(
        status="awaiting_slot",
        write_authorization=WriteAuthorization(operation="booking"),
        constraints=CustomerConstraints(service_id="service-1"),
        requested_service_ids=["service-1"],
        option_snapshot=_snapshot(purpose="booking_slot"),
        version=3,
    )


def _booking_awaiting_confirmation() -> BookingTaskState:
    return BookingTaskState(
        status="awaiting_confirmation",
        write_authorization=WriteAuthorization(operation="booking"),
        constraints=CustomerConstraints(service_id="service-1"),
        requested_service_ids=["service-1"],
        proposal=_proposal(),
        version=3,
    )


def _flow_from_payload(payload: dict[str, object], *, flow_type: str = "booking") -> ConversationFlowState:
    return ConversationFlowState(
        workspace_id=uuid4(),
        conversation_id=uuid4(),
        patient_id=uuid4(),
        flow_type=flow_type,
        status="collecting_requirements",
        is_active=True,
        capabilities=[],
        entity_state={
            "agent_core_v2": {
                "schema_version": 1,
                "active_task": payload,
            }
        },
        missing_information=[],
        pending_action={},
        option_snapshot={},
        last_decision={},
        version=1,
        expires_at=NOW + timedelta(hours=1),
        last_turn_at=NOW,
    )


def _decode_legacy(task: BookingTaskState, legacy_status: str) -> BookingTaskState:
    payload = task.model_dump(mode="json")
    payload["status"] = legacy_status
    restored = state_persistence._decode_flow_task(_flow_from_payload(payload))
    assert isinstance(restored, BookingTaskState)
    return restored


def test_legacy_booking_statuses_normalize_to_internal_contract() -> None:
    collecting = _decode_legacy(_booking_collecting(), "collecting")
    assert collecting.status == "collecting"

    awaiting_slot = _decode_legacy(_booking_awaiting_slot(), "awaiting_choice")
    assert awaiting_slot.status == "awaiting_slot"
    assert awaiting_slot.option_snapshot is not None
    assert awaiting_slot.option_snapshot.purpose == "booking_slot"

    non_slot = _booking_collecting().model_copy(
        update={"option_snapshot": _snapshot(purpose="device")}
    )
    collecting_with_choice = _decode_legacy(non_slot, "awaiting_choice")
    assert collecting_with_choice.status == "collecting"
    assert collecting_with_choice.option_snapshot is not None
    assert collecting_with_choice.option_snapshot.purpose == "device"

    awaiting_confirmation = _decode_legacy(_booking_awaiting_confirmation(), "ready")
    assert awaiting_confirmation.status == "awaiting_confirmation"
    assert awaiting_confirmation.proposal is not None

    ready_without_proposal = _decode_legacy(_booking_collecting(), "ready")
    assert ready_without_proposal.status == "collecting"
    assert ready_without_proposal.proposal is None

    executing = _decode_legacy(_booking_collecting(), "executing")
    assert executing.status == "collecting"
    assert executing.write_authorization.authorized is False


@pytest.mark.parametrize(
    ("task", "wire_status"),
    [
        (_booking_collecting(), "collecting"),
        (_booking_awaiting_slot(), "awaiting_choice"),
        (_booking_awaiting_confirmation(), "ready"),
    ],
)
def test_internal_booking_status_serializes_to_rollback_safe_legacy_wire(
    task: BookingTaskState,
    wire_status: str,
) -> None:
    payload = state_persistence._persisted_task_payload(task)

    assert payload["status"] == wire_status
    assert payload["status"] in {"collecting", "awaiting_choice", "ready"}
    assert payload["status"] not in {"awaiting_slot", "awaiting_confirmation"}

    restored = state_persistence._decode_flow_task(_flow_from_payload(payload))
    assert isinstance(restored, BookingTaskState)
    assert restored.status == task.status


def test_booking_flow_status_mapping_uses_internal_contract() -> None:
    assert state_persistence._flow_status_for_task(_booking_collecting()) == "collecting_requirements"
    assert state_persistence._flow_status_for_task(_booking_awaiting_slot()) == (
        "awaiting_option_selection"
    )
    assert state_persistence._flow_status_for_task(_booking_awaiting_confirmation()) == (
        "ready_to_execute"
    )


def test_reschedule_status_and_wire_contract_remain_legacy() -> None:
    for status, expected_flow in (
        ("collecting", "collecting_requirements"),
        ("awaiting_choice", "awaiting_option_selection"),
        ("ready", "ready_to_execute"),
        ("executing", "ready_to_execute"),
    ):
        snapshot = _snapshot(purpose="reschedule_slot", task_version=1) if status == "awaiting_choice" else None
        task = RescheduleTaskState(
            status=status,
            write_authorization=WriteAuthorization(operation="reschedule"),
            target=RescheduleTarget(appointment_id="appointment-1"),
            replacement=CustomerConstraints(service_id="service-1"),
            option_snapshot=snapshot,
        )
        payload = state_persistence._persisted_task_payload(task)
        assert task.status == status
        assert payload["status"] == status
        assert state_persistence._flow_status_for_task(task) == expected_flow


def test_booking_stage_structural_invariants_fail_closed() -> None:
    with pytest.raises(ValidationError, match="awaiting_slot requires"):
        BookingTaskState(
            status="awaiting_slot",
            write_authorization=WriteAuthorization(operation="booking"),
            requested_service_ids=["service-1"],
        )

    with pytest.raises(ValidationError, match="awaiting_confirmation requires"):
        BookingTaskState(
            status="awaiting_confirmation",
            write_authorization=WriteAuthorization(operation="booking"),
            requested_service_ids=["service-1"],
        )

    with pytest.raises(ValidationError, match="cannot retain a booking-slot snapshot"):
        BookingTaskState(
            status="awaiting_confirmation",
            write_authorization=WriteAuthorization(operation="booking"),
            requested_service_ids=["service-1"],
            proposal=_proposal(),
            option_snapshot=_snapshot(purpose="booking_slot"),
            version=3,
        )


def test_status_setters_use_booking_internal_stages_without_changing_reschedule() -> None:
    booking = _booking_collecting()
    booking_slot = attach_option_snapshot(
        booking,
        snapshot_id="booking-slot",
        purpose="booking_slot",
        choices=[OptionChoice(ref="slot-1", payload={})],
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    assert isinstance(booking_slot, BookingTaskState)
    assert booking_slot.status == "awaiting_slot"

    device_choice = attach_option_snapshot(
        booking,
        snapshot_id="device-choice",
        purpose="device",
        choices=[OptionChoice(ref="device-1", payload={})],
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    assert isinstance(device_choice, BookingTaskState)
    assert device_choice.status == "collecting"
    assert device_choice.option_snapshot is not None
    assert device_choice.option_snapshot.purpose == "device"

    proposal_task = attach_booking_proposal(booking, proposal=_proposal(task_version=booking.version))
    assert proposal_task.status == "awaiting_confirmation"

    reschedule = RescheduleTaskState(
        write_authorization=WriteAuthorization(operation="reschedule"),
        target=RescheduleTarget(appointment_id="appointment-1"),
        replacement=CustomerConstraints(service_id="service-1"),
    )
    reschedule_choice = attach_option_snapshot(
        reschedule,
        snapshot_id="reschedule-slot",
        purpose="reschedule_slot",
        choices=[OptionChoice(ref="slot-1", payload={})],
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    assert isinstance(reschedule_choice, RescheduleTaskState)
    assert reschedule_choice.status == "awaiting_choice"


def test_booking_write_ready_selection_does_not_claim_confirmation_before_proposal() -> None:
    state = _booking_awaiting_slot()
    transition = apply_step_state(
        state,
        step=PlanStep(
            operation_index=0,
            operation_type="select_active",
            disposition="write_ready",
            state_action="select_active",
            write_intent=WriteIntent(kind="booking", authorized=True, parameters={}),
            facts={"selected_option_ref": "option-1"},
        ),
        operation=TurnOperation(
            type="select_active",
            execution_intent="execute",
            entities=TurnEntities(),
        ),
        reads=None,
        now=NOW,
        turn_id="turn-select",
    )

    updated = transition.active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.status == "awaiting_slot"
    assert updated.proposal is None
    assert updated.derived.selected_slot_ref == "option-1"

def test_proposal_invalidation_paths_return_booking_to_collecting() -> None:
    state = _booking_awaiting_confirmation()
    invalidated = (
        invalidate_booking_proposal(state),
        record_booking_commercial_basis_presented(state, commercial_key="service-1|none|standalone"),
        record_booking_doctor_compatibility(state, compatible=True),
        record_booking_device_compatibility(state, compatible=True),
    )

    for updated in invalidated:
        assert updated.status == "collecting"
        assert updated.proposal is None
        assert updated.option_snapshot is None
