from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import DateConstraint, TimeConstraint
from app.models.conversation_flow_state import ConversationFlowState
from app.services.agent_v2 import state_persistence
from app.services.agent_v2.active_task_progress import prepare_active_task_clock_progress
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


def _legacy_pre_258_booking_payload(
    *,
    clock_source: str = "explicit_period",
    clock: str = "15:00",
    period_status: str = "explicit",
) -> dict[str, object]:
    """Literal schema-v1 payload emitted by pre-#258 model_dump(mode="json")."""
    return {
        "task_type": "booking",
        "status": "collecting",
        "write_authorization": {
            "operation": "booking",
            "authorized": False,
            "source_turn_id": None,
            "granted_at": None,
        },
        "constraints": {
            "service_id": "service-1",
            "doctor_id": "doctor-1",
            "branch_id": "branch-1",
            "device_key": "device-1",
            "date": {
                "mode": "exact",
                "start_date": "2026-10-15",
                "end_date": None,
            },
            "time": {
                "mode": "exact",
                "start_time": clock,
                "end_time": None,
                "start_time_ambiguity": "none",
                "end_time_ambiguity": "none",
                "period_status": period_status,
            },
            "time_clock_source": clock_source,
            "package_usage": "unspecified",
            "pulse_usage": "unspecified",
        },
        "requested_service_ids": ["service-1"],
        "service_device_keys": {},
        "proposal": None,
        "grouped": None,
        "derived": {
            "availability_snapshot_id": None,
            "selected_slot_ref": None,
            "selected_package_id": None,
            "package_validated": False,
            "doctor_compatible": None,
            "device_compatible": None,
            "commercial_basis_presented_key": None,
            "commercial_basis_presented_device_key": None,
        },
        "option_snapshot": None,
        "version": 1,
    }


def _flow_for_raw_legacy_task(
    payload: dict[str, object], *, flow_type: str = "booking"
) -> ConversationFlowState:
    now = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
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
        version=3,
        expires_at=now + timedelta(hours=1),
        last_turn_at=now,
    )


def test_pre_258_raw_booking_payload_with_period_status_decodes() -> None:
    flow = _flow_for_raw_legacy_task(_legacy_pre_258_booking_payload())

    restored = state_persistence._decode_flow_task(flow)

    assert restored is not None
    assert restored.task_type == "booking"



def _legacy_pre_258_reschedule_payload() -> dict[str, object]:
    """Literal schema-v1 reschedule payload with legacy replacement.time.period_status."""
    return {
        "task_type": "reschedule",
        "status": "collecting",
        "write_authorization": {
            "operation": "reschedule",
            "authorized": False,
            "source_turn_id": None,
            "granted_at": None,
        },
        "target": {
            "appointment_id": "appointment-1",
            "service_id": "service-1",
            "doctor_id": "doctor-1",
            "device_key": "device-1",
            "start_local": "2026-10-14T15:00:00+03:00",
            "payment_context": {},
        },
        "replacement": {
            "service_id": "service-1",
            "doctor_id": "doctor-1",
            "branch_id": "branch-1",
            "device_key": "device-1",
            "date": {
                "mode": "exact",
                "start_date": "2026-10-15",
                "end_date": None,
            },
            "time": {
                "mode": "exact",
                "start_time": "15:00",
                "end_time": None,
                "start_time_ambiguity": "none",
                "end_time_ambiguity": "none",
                "period_status": "explicit",
            },
            "time_clock_source": "explicit_period",
            "package_usage": "unspecified",
            "pulse_usage": "unspecified",
        },
        "derived": {
            "availability_snapshot_id": None,
            "selected_slot_ref": None,
            "package_validated": False,
            "doctor_compatible": None,
            "device_compatible": None,
        },
        "option_snapshot": None,
        "version": 1,
    }


def _legacy_pre_258_grouped_payload() -> dict[str, object]:
    """Literal schema-v1 grouped payload; every persisted component carries period_status."""
    payload = _legacy_pre_258_booking_payload(
        clock_source="bare_spoken_12h",
        clock="15:00",
        period_status="none",
    )
    payload["requested_service_ids"] = ["service-1", "service-2"]
    payload["grouped"] = {
        "components": [
            {
                "service_id": "service-1",
                "doctor_id": "doctor-1",
                "branch_id": "branch-1",
                "device_key": "device-1",
                "date": {
                    "mode": "exact",
                    "start_date": "2026-10-15",
                    "end_date": None,
                },
                "time": {
                    "mode": "exact",
                    "start_time": "15:00",
                    "end_time": None,
                    "start_time_ambiguity": "none",
                    "end_time_ambiguity": "none",
                    "period_status": "none",
                },
                "time_clock_source": "bare_spoken_12h",
                "package_usage": "unspecified",
                "pulse_usage": "unspecified",
            },
            {
                "service_id": "service-2",
                "doctor_id": "doctor-1",
                "branch_id": "branch-1",
                "device_key": "device-1",
                "date": {
                    "mode": "exact",
                    "start_date": "2026-10-15",
                    "end_date": None,
                },
                "time": {
                    "mode": "exact",
                    "start_time": "15:00",
                    "end_time": None,
                    "start_time_ambiguity": "none",
                    "end_time_ambiguity": "none",
                    "period_status": "none",
                },
                "time_clock_source": "bare_spoken_12h",
                "package_usage": "unspecified",
                "pulse_usage": "unspecified",
            },
        ]
    }
    return payload


def _migration_clock_context():
    return build_semantic_context(
        {
            "services": [
                {
                    "id": "service-1",
                    "name": "Consultation",
                    "duration_minutes": 30,
                    "requires_laser_device": False,
                }
            ],
            "branches": [
                {
                    "id": "branch-1",
                    "name": "Clinic A",
                    "working_hours": [
                        {"weekday": weekday, "start": "10:00", "end": "22:00"}
                        for weekday in range(7)
                    ],
                }
            ],
        }
    )


def test_l1_old_bare_booking_decodes_without_reopening_resolved_clock() -> None:
    flow = _flow_for_raw_legacy_task(
        _legacy_pre_258_booking_payload(
            clock_source="bare_spoken_12h",
            clock="15:00",
            period_status="none",
        )
    )

    restored = state_persistence._decode_flow_task(flow)

    assert restored is not None
    assert restored.constraints.time_clock_source == "bare_spoken_12h"
    assert restored.constraints.time is not None
    assert restored.constraints.time.start_time == "15:00"
    assert restored.constraints.time.start_time_ambiguity == "none"
    assert "period_status" not in restored.constraints.time.model_dump(mode="json")


def test_l2_old_explicit_period_booking_reenters_backend_owned_clock_policy() -> None:
    flow = _flow_for_raw_legacy_task(_legacy_pre_258_booking_payload())

    restored = state_persistence._decode_flow_task(flow)

    assert restored is not None
    assert restored.constraints.time_clock_source == "bare_spoken_12h"
    assert restored.constraints.time is not None
    assert restored.constraints.time.start_time == "03:00"
    assert restored.constraints.time.start_time_ambiguity == "twelve_hour"

    prepared, gate = prepare_active_task_clock_progress(
        restored,
        operation_index=0,
        context=_migration_clock_context(),
    )
    assert gate is None
    assert prepared.constraints.time is not None
    assert prepared.constraints.time.start_time == "15:00"
    assert prepared.constraints.time.start_time_ambiguity == "none"
    assert prepared.constraints.time_clock_source == "bare_spoken_12h"


def test_l3_old_literal_24h_booking_remains_exact() -> None:
    flow = _flow_for_raw_legacy_task(
        _legacy_pre_258_booking_payload(
            clock_source="literal_24h",
            clock="03:00",
            period_status="none",
        )
    )

    restored = state_persistence._decode_flow_task(flow)

    assert restored is not None
    assert restored.constraints.time_clock_source == "literal_24h"
    assert restored.constraints.time is not None
    assert restored.constraints.time.start_time == "03:00"
    assert restored.constraints.time.start_time_ambiguity == "none"

    prepared, gate = prepare_active_task_clock_progress(
        restored,
        operation_index=0,
        context=_migration_clock_context(),
    )
    assert gate is None
    assert prepared.constraints.time is not None
    assert prepared.constraints.time.start_time == "03:00"
    assert prepared.constraints.time.start_time_ambiguity == "none"


def test_l4_old_reschedule_replacement_time_decodes_and_preserves_target() -> None:
    flow = _flow_for_raw_legacy_task(
        _legacy_pre_258_reschedule_payload(),
        flow_type="appointment_reschedule",
    )

    restored = state_persistence._decode_flow_task(flow)

    assert restored is not None
    assert restored.task_type == "reschedule"
    assert restored.target.appointment_id == "appointment-1"
    assert restored.replacement.service_id == "service-1"
    assert restored.replacement.time_clock_source == "bare_spoken_12h"
    assert restored.replacement.time is not None
    assert restored.replacement.time.start_time == "03:00"
    assert restored.replacement.time.start_time_ambiguity == "twelve_hour"
    assert "period_status" not in restored.replacement.time.model_dump(mode="json")


def test_l5_old_grouped_component_times_decode_and_preserve_requested_set() -> None:
    flow = _flow_for_raw_legacy_task(_legacy_pre_258_grouped_payload())

    restored = state_persistence._decode_flow_task(flow)

    assert restored is not None
    assert restored.requested_service_ids == ["service-1", "service-2"]
    assert restored.grouped is not None
    assert [component.service_id for component in restored.grouped.components] == [
        "service-1",
        "service-2",
    ]
    for component in restored.grouped.components:
        assert component.time is not None
        assert "period_status" not in component.time.model_dump(mode="json")


def test_l6_legacy_decode_resave_emits_only_current_schema() -> None:
    flow = _flow_for_raw_legacy_task(_legacy_pre_258_booking_payload())

    restored = state_persistence._decode_flow_task(flow)
    assert restored is not None
    namespace = state_persistence._task_namespace(restored)
    persisted = namespace["active_task"]

    assert namespace["schema_version"] == 1
    assert isinstance(persisted, dict)
    constraints = persisted["constraints"]
    assert isinstance(constraints, dict)
    migrated_time = constraints["time"]
    assert isinstance(migrated_time, dict)
    assert "period_status" not in migrated_time
    assert constraints["time_clock_source"] == "bare_spoken_12h"
    assert constraints["time_clock_source"] != "explicit_period"
    assert migrated_time["start_time"] == "03:00"
    assert migrated_time["start_time_ambiguity"] == "twelve_hour"


def test_pre_deploy_booking_loads_post_deploy_and_progresses_without_losing_context() -> None:
    flow = _flow_for_raw_legacy_task(_legacy_pre_258_booking_payload())

    restored = state_persistence._decode_flow_task(flow)
    assert restored is not None
    before = (
        restored.constraints.service_id,
        restored.constraints.doctor_id,
        restored.constraints.branch_id,
        restored.constraints.device_key,
        restored.constraints.date,
        tuple(restored.requested_service_ids),
    )

    prepared, gate = prepare_active_task_clock_progress(
        restored,
        operation_index=0,
        context=_migration_clock_context(),
    )

    assert gate is None
    assert prepared.task_type == "booking"
    assert (
        prepared.constraints.service_id,
        prepared.constraints.doctor_id,
        prepared.constraints.branch_id,
        prepared.constraints.device_key,
        prepared.constraints.date,
        tuple(prepared.requested_service_ids),
    ) == before
    assert prepared.constraints.time is not None
    assert prepared.constraints.time.start_time == "15:00"
    assert prepared.write_authorization.authorized is False
    assert prepared.proposal is None


def _all_day_clock_context():
    return build_semantic_context(
        {
            "services": [{"id": "service-1", "name": "Consultation", "duration_minutes": 30}],
            "branches": [
                {
                    "id": "branch-1",
                    "name": "Clinic A",
                    "working_hours": [
                        {"weekday": weekday, "start": "00:00", "end": "23:59"}
                        for weekday in range(7)
                    ],
                }
            ],
        }
    )


def test_unresolved_1030_pm_survives_two_persistence_decode_cycles() -> None:
    task = _booking_task().model_copy(
        update={
            "constraints": CustomerConstraints(
                service_id="service-1",
                branch_id="branch-1",
                time=TimeConstraint(
                    mode="exact",
                    start_time="10:30",
                    start_time_ambiguity="twelve_hour",
                    spoken_period_hint="pm",
                ),
                time_clock_source="bare_spoken_12h",
            )
        }
    )

    first_payload = state_persistence._task_namespace(task)["active_task"]
    assert first_payload["constraints"]["time"]["spoken_period_hint"] == "pm"
    first = state_persistence._decode_flow_task(_flow_for_task(task))
    assert first is not None
    assert first.constraints.time is not None
    assert first.constraints.time.spoken_period_hint == "pm"

    second_payload = state_persistence._task_namespace(first)["active_task"]
    assert second_payload["constraints"]["time"]["spoken_period_hint"] == "pm"
    second = state_persistence._decode_flow_task(_flow_for_task(first))
    assert second is not None
    assert second.constraints.time is not None
    assert second.constraints.time.start_time == "10:30"
    assert second.constraints.time.start_time_ambiguity == "twelve_hour"
    assert second.constraints.time.spoken_period_hint == "pm"


def test_reschedule_1030_pm_round_trip_preserves_target_then_resolves() -> None:
    task = _reschedule_task().model_copy(
        update={
            "replacement": CustomerConstraints(
                service_id="service-1",
                branch_id="branch-1",
                date=DateConstraint(mode="exact", start_date="2026-10-15"),
                time=TimeConstraint(
                    mode="exact",
                    start_time="10:30",
                    start_time_ambiguity="twelve_hour",
                    spoken_period_hint="pm",
                ),
                time_clock_source="bare_spoken_12h",
            )
        }
    )
    restored = state_persistence._decode_flow_task(_flow_for_task(task))
    assert isinstance(restored, RescheduleTaskState)
    assert restored.target.appointment_id == "appointment-1"
    assert restored.replacement.time is not None
    assert restored.replacement.time.spoken_period_hint == "pm"

    prepared, gate = prepare_active_task_clock_progress(
        restored,
        operation_index=0,
        context=_all_day_clock_context(),
    )
    assert gate is None
    assert isinstance(prepared, RescheduleTaskState)
    assert prepared.target.appointment_id == "appointment-1"
    assert prepared.replacement.time is not None
    assert prepared.replacement.time.start_time == "22:30"
    assert prepared.replacement.time.start_time_ambiguity == "none"
    assert prepared.replacement.time.spoken_period_hint == "none"


def test_one_to_nine_provider_noise_is_never_injected_into_persisted_state() -> None:
    task = _booking_task().model_copy(
        update={
            "constraints": CustomerConstraints(
                service_id="service-1",
                branch_id="branch-1",
                date=DateConstraint(mode="exact", start_date="2026-10-15"),
                time=TimeConstraint(
                    mode="exact",
                    start_time="03:00",
                    start_time_ambiguity="twelve_hour",
                    spoken_period_hint="am",
                ),
                time_clock_source="bare_spoken_12h",
            )
        }
    )
    payload = state_persistence._task_namespace(task)["active_task"]
    assert "spoken_period_hint" not in payload["constraints"]["time"]
