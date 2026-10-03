from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.conversation_flow_state import ConversationFlowState
from app.services.agent_v2 import state_persistence
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    GroupedBookingState,
    OptionChoice,
    OptionSnapshot,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)


def _booking_task(*, status: str = "collecting") -> BookingTaskState:
    now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    return BookingTaskState(
        status=status,
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=now,
        ),
        constraints=CustomerConstraints(service_id="service-1"),
        option_snapshot=(
            OptionSnapshot(
                snapshot_id="snapshot-1",
                purpose="booking_slot",
                task_version=1,
                created_at=now,
                expires_at=now + timedelta(minutes=15),
                options=[OptionChoice(ref="V1", payload={"slot": "20:00"})],
            )
            if status == "awaiting_choice"
            else None
        ),
    )


def _reschedule_task() -> RescheduleTaskState:
    return RescheduleTaskState(
        write_authorization=WriteAuthorization(operation="reschedule"),
        target=RescheduleTarget(appointment_id="appointment-1", service_id="service-1"),
        replacement=CustomerConstraints(service_id="service-1"),
    )


def _flow_for_task(task, *, flow_type: str | None = None) -> ConversationFlowState:
    now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    return ConversationFlowState(
        workspace_id=uuid4(),
        conversation_id=uuid4(),
        patient_id=uuid4(),
        flow_type=flow_type or state_persistence._flow_type_for_task(task),
        status=state_persistence._flow_status_for_task(task),
        is_active=True,
        capabilities=[],
        entity_state=state_persistence._entity_state_for_task(task),
        missing_information=[],
        pending_action={},
        option_snapshot={},
        last_decision={},
        version=3,
        expires_at=now + timedelta(hours=1),
        last_turn_at=now,
    )


def test_v2_task_round_trips_through_namespaced_json() -> None:
    task = _booking_task(status="awaiting_choice")
    flow = _flow_for_task(task)

    namespace = flow.entity_state["agent_core_v2"]
    assert namespace["schema_version"] == 1
    assert isinstance(namespace["active_task"]["write_authorization"]["granted_at"], str)

    restored = state_persistence._decode_flow_task(flow)
    assert restored == task
    assert restored is not None
    assert restored.option_snapshot is not None
    assert restored.option_snapshot.options[0].payload == {"slot": "20:00"}


def test_foreign_flow_is_not_deserialized_as_v2() -> None:
    task = _booking_task()
    flow = _flow_for_task(task)
    flow.entity_state = {"service_id": "legacy-service"}

    assert state_persistence._decode_flow_task(flow) is None


def test_flow_type_mismatch_fails_closed() -> None:
    flow = _flow_for_task(_booking_task(), flow_type="appointment_reschedule")

    with pytest.raises(state_persistence.InvalidPersistedV2StateError):
        state_persistence._decode_flow_task(flow)


def test_task_status_maps_to_existing_flow_statuses() -> None:
    assert state_persistence._flow_status_for_task(_booking_task(status="collecting")) == (
        "collecting_requirements"
    )
    assert state_persistence._flow_status_for_task(_booking_task(status="awaiting_choice")) == (
        "awaiting_option_selection"
    )
    assert state_persistence._flow_status_for_task(_booking_task(status="ready")) == "ready_to_execute"
    assert state_persistence._flow_status_for_task(_booking_task(status="executing")) == (
        "ready_to_execute"
    )


def test_reschedule_uses_existing_reschedule_flow_type() -> None:
    task = _reschedule_task()

    assert state_persistence._flow_type_for_task(task) == "appointment_reschedule"
    assert state_persistence._capabilities_for_task(task) == ["appointment_reschedule"]


def test_create_rejects_foreign_active_flow_without_replacing_it(monkeypatch) -> None:
    task = _booking_task()
    foreign = _flow_for_task(task)
    foreign.entity_state = {"legacy": {"service": "x"}}

    monkeypatch.setattr(state_persistence, "get_active_flow", lambda *args, **kwargs: foreign)

    with pytest.raises(state_persistence.ForeignActiveFlowError):
        state_persistence.save_active_task(
            object(),
            workspace_id=foreign.workspace_id,
            conversation_id=foreign.conversation_id,
            patient_id=foreign.patient_id,
            active_task=task,
            run_id=uuid4(),
        )


def test_create_requires_expected_handle_when_v2_task_already_exists(monkeypatch) -> None:
    task = _booking_task()
    current = _flow_for_task(task)

    monkeypatch.setattr(state_persistence, "get_active_flow", lambda *args, **kwargs: current)

    with pytest.raises(state_persistence.V2StateConflictError):
        state_persistence.save_active_task(
            object(),
            workspace_id=current.workspace_id,
            conversation_id=current.conversation_id,
            patient_id=current.patient_id,
            active_task=task,
            run_id=uuid4(),
        )


def test_stale_db_version_is_rejected_before_transition() -> None:
    task = _booking_task()
    flow = _flow_for_task(task)
    flow.version = 4
    expected = state_persistence.PersistedActiveTask(
        active_task=task,
        flow_id=flow.id,
        flow_version=3,
    )

    class FakeSession:
        def scalar(self, statement):
            return flow

    with pytest.raises(state_persistence.V2StateConflictError):
        state_persistence.save_active_task(
            FakeSession(),
            workspace_id=flow.workspace_id,
            conversation_id=flow.conversation_id,
            patient_id=flow.patient_id,
            active_task=task,
            run_id=uuid4(),
            expected=expected,
        )

def test_legacy_pulse_usage_loads_and_normalizes_to_unspecified() -> None:
    task = _booking_task(status="awaiting_choice")
    flow = _flow_for_task(task)
    namespace = flow.entity_state["agent_core_v2"]
    namespace["active_task"]["constraints"]["pulse_usage"] = "use_existing"
    namespace["active_task"]["option_snapshot"]["options"][0]["payload"]["pulse_usage"] = "use_existing"

    restored = state_persistence._decode_flow_task(flow)

    assert restored is not None
    assert restored.task_type == "booking"
    assert restored.constraints.pulse_usage == "unspecified"
    assert restored.option_snapshot is not None
    assert restored.option_snapshot.options[0].payload["pulse_usage"] == "use_existing"


def test_grouped_booking_state_round_trips_without_schema_version_bump() -> None:
    task = _booking_task().model_copy(
        update={
            "grouped": GroupedBookingState(
                components=[
                    CustomerConstraints(service_id="service-1"),
                    CustomerConstraints(service_id="service-2"),
                ]
            )
        }
    )
    flow = _flow_for_task(task)

    namespace = flow.entity_state["agent_core_v2"]
    assert namespace["schema_version"] == 1

    restored = state_persistence._decode_flow_task(flow)

    assert restored == task
    assert restored is not None
    assert restored.grouped is not None
    assert [item.service_id for item in restored.grouped.components] == [
        "service-1",
        "service-2",
    ]



def test_persisted_task_type_change_still_fails_closed() -> None:
    booking = _booking_task()
    reschedule = _reschedule_task()
    flow = _flow_for_task(reschedule)
    expected = state_persistence.PersistedActiveTask(
        active_task=reschedule,
        flow_id=flow.id,
        flow_version=flow.version,
    )

    class FakeSession:
        def scalar(self, statement):
            return flow

    with pytest.raises(
        state_persistence.V2StateConflictError,
        match="cannot change task type in place",
    ):
        state_persistence.save_active_task(
            FakeSession(),
            workspace_id=flow.workspace_id,
            conversation_id=flow.conversation_id,
            patient_id=flow.patient_id,
            active_task=booking,
            run_id=uuid4(),
            expected=expected,
        )
