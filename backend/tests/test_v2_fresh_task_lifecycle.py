from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.agents.v2.semantic_context import SemanticContext, build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import (
    isolate_fresh_task_context,
    merge_verified_action_context,
    merge_verified_read_context,
)
from app.services.agent_v2 import orchestrator
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    classify_active_task_lifecycle,
)
from app.services.agent_v2.planner import PlanStep
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    DerivedRescheduleState,
    OptionChoice,
    OptionSnapshot,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state
from app.services.agent_v2.state_persistence import PersistedActiveTask

NOW = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)


def _context():
    return build_semantic_context(
        {
            "services": [
                {
                    "id": "svc-underarm",
                    "name": "Under Arm",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_key": "deka", "device_name": "DEKA Again"},
                    ],
                },
                {
                    "id": "svc-fullbody",
                    "name": "Full Body",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_key": "candela", "device_name": "Candela Gentle"},
                    ],
                },
            ],
            "doctors": [],
            "appointments": [],
        }
    )


def _authorized(operation: str, source_turn_id: str) -> WriteAuthorization:
    return WriteAuthorization(
        operation=operation,
        authorized=True,
        source_turn_id=source_turn_id,
        granted_at=NOW,
    )


def _stale_reschedule() -> RescheduleTaskState:
    return RescheduleTaskState(
        status="awaiting_choice",
        write_authorization=_authorized("reschedule", "old-reschedule-turn"),
        target=RescheduleTarget(
            appointment_id="apt-old",
            service_id="svc-underarm",
            doctor_id="doc-old",
            device_key="deka",
            start_local="2026-10-08T10:00:00+03:00",
        ),
        replacement=CustomerConstraints(
            service_id="svc-underarm",
            doctor_id="doc-old",
            device_key="deka",
            date=DateConstraint(mode="exact", start_date="2026-10-08"),
            time=TimeConstraint(mode="after", start_time="17:00"),
        ),
        derived=DerivedRescheduleState(
            availability_snapshot_id="old-snapshot",
            selected_slot_ref="old-slot",
        ),
        option_snapshot=OptionSnapshot(
            snapshot_id="old-snapshot",
            purpose="reschedule_slot",
            task_version=1,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
            options=[
                OptionChoice(
                    ref="old-slot",
                    label="17:30",
                    payload={"appointment_id": "apt-old"},
                )
            ],
        ),
    )


def _partial_booking() -> BookingTaskState:
    return BookingTaskState(
        status="awaiting_choice",
        write_authorization=_authorized("booking", "old-booking-turn"),
        constraints=CustomerConstraints(
            service_id="svc-underarm",
            doctor_id="doc-old",
            device_key="deka",
            date=DateConstraint(mode="exact", start_date="2026-10-08"),
            time=TimeConstraint(mode="after", start_time="17:00"),
        ),
        option_snapshot=OptionSnapshot(
            snapshot_id="old-booking-snapshot",
            purpose="booking_slot",
            task_version=1,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
            options=[OptionChoice(ref="old-booking-slot", payload={"start_time_24h": "17:30"})],
        ),
    )


def _full_body_booking(
    *,
    relationship: str = "unspecified",
    fresh_task: bool = False,
) -> TurnOperation:
    return TurnOperation(
        type="book",
        entities=TurnEntities(service=EntityReference(ref="S2", text="Full Body")),
        execution_intent="execute",
        active_task_relationship=relationship,
        fresh_task=fresh_task,
        fresh_task_explicit_fields=["service"] if fresh_task else [],
    )





def test_fresh_task_context_strips_unstated_completed_booking_constraints() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(ref="S1", text="Under Arm"),
                    doctor=EntityReference(ref="D1", text="Mary"),
                    device=EntityReference(ref="V1", text="DEKA Again"),
                    appointment=EntityReference(ref="A1"),
                    date=DateConstraint(mode="exact", start_date="2026-10-08"),
                    time=TimeConstraint(mode="exact", start_time="16:00"),
                ),
                execution_intent="execute",
                continues_previous=True,
                selection={"kind": "ref", "ref": "slot-old"},
                package_usage="use_existing",
                fresh_task=True,
                fresh_task_explicit_fields=[],
            )
        ]
    )

    isolated = isolate_fresh_task_context(turn)
    operation = isolated.operations[0]

    assert operation.fresh_task is True
    assert operation.continues_previous is False
    assert operation.selection is None
    assert operation.source_appointment is None
    assert operation.package_usage == "unspecified"
    assert operation.entities.service is None
    assert operation.entities.doctor is None
    assert operation.entities.device is None
    assert operation.entities.appointment is None
    assert operation.entities.date is None
    assert operation.entities.time is None


def test_fresh_task_context_preserves_only_current_message_explicit_fields() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(ref="S2", text="Full Body"),
                    doctor=EntityReference(ref="D1", text="old doctor"),
                    device=EntityReference(ref="V1", text="old device"),
                    date=DateConstraint(mode="exact", start_date="2026-10-08"),
                    time=TimeConstraint(mode="exact", start_time="16:00"),
                ),
                execution_intent="execute",
                fresh_task=True,
                fresh_task_explicit_fields=["service"],
            )
        ]
    )

    operation = isolate_fresh_task_context(turn).operations[0]

    assert operation.entities.service == EntityReference(ref="S2", text="Full Body")
    assert operation.entities.doctor is None
    assert operation.entities.device is None
    assert operation.entities.date is None
    assert operation.entities.time is None


def test_fresh_task_stays_clean_after_verified_context_merge() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(ref="S2", text="Full Body"),
                    doctor=EntityReference(ref="D1", text="old doctor"),
                    device=EntityReference(ref="V1", text="old device"),
                    date=DateConstraint(mode="exact", start_date="2026-10-08"),
                    time=TimeConstraint(mode="exact", start_time="16:00"),
                ),
                execution_intent="execute",
                continues_previous=True,
                fresh_task=True,
                fresh_task_explicit_fields=["service"],
            )
        ]
    )
    semantic_context = SemanticContext(
        model_input={
            "recent_verified_read": {
                "service_ref": "S1",
                "doctor_ref": "D1",
                "device_ref": "V1",
                "date": {"mode": "exact", "start_date": "2026-10-08"},
                "time": {"mode": "exact", "start_time": "16:00"},
            },
            "recent_verified_action": {
                "operation_type": "book",
                "appointment_ref": "A1",
                "appointment_id": "old-appointment",
                "service_id": "svc-underarm",
                "doctor_id": "doc-old",
                "device_key": "deka",
            },
        },
        reference_map={},
    )

    isolated = isolate_fresh_task_context(turn)
    merged = merge_verified_read_context(isolated, semantic_context)
    merged = merge_verified_action_context(merged, semantic_context)
    operation = merged.operations[0]

    assert operation.continues_previous is False
    assert operation.entities.service == EntityReference(ref="S2", text="Full Body")
    assert operation.entities.doctor is None
    assert operation.entities.device is None
    assert operation.entities.appointment is None
    assert operation.entities.date is None
    assert operation.entities.time is None
    assert operation.source_appointment is None
    assert operation.selection is None


def test_non_task_relationship_marker_is_ignored_by_lifecycle_classifier() -> None:
    operation = TurnOperation(
        type="select_active",
        entities=TurnEntities(),
        execution_intent="execute",
        active_task_relationship="replace",
    )

    assert (
        classify_active_task_lifecycle(operation, active_task=_partial_booking())
        == "preserve"
    )


def test_cross_task_type_primary_goal_is_deterministic_replacement() -> None:
    operation = _full_body_booking()

    assert (
        classify_active_task_lifecycle(operation, active_task=_stale_reschedule())
        == "replace"
    )


def test_side_read_never_replaces_active_task_from_operation_type_difference() -> None:
    operation = TurnOperation(
        type="pricing",
        entities=TurnEntities(service=EntityReference(ref="S2", text="Full Body")),
        execution_intent="informational",
    )

    assert (
        classify_active_task_lifecycle(operation, active_task=_stale_reschedule())
        == "preserve"
    )


def test_same_booking_correction_remains_current_task() -> None:
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-10-06"),
        ),
        execution_intent="execute",
        active_task_relationship="continue",
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="clarify",
        clarification_field="time",
        response_goal="clarification",
    )

    adapted = adapt_matching_active_task_step(
        step,
        operation=operation,
        active_task=_partial_booking(),
        context=_context(),
    )

    assert adapted.state_action == "update_active"
    assert adapted.facts["date"]["start_date"] == "2026-10-06"


def test_explicit_second_booking_replaces_task_without_stale_execution_state() -> None:
    old = _partial_booking()
    operation = _full_body_booking(relationship="replace", fresh_task=True)
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="clarify",
        clarification_field="date",
        response_goal="clarification",
    )

    adapted = adapt_matching_active_task_step(
        step,
        operation=operation,
        active_task=old,
        context=_context(),
    )
    transition = apply_step_state(
        old,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW + timedelta(days=3),
        turn_id="fresh-booking-turn",
    )

    assert adapted.state_action == "replace_active"
    assert adapted.facts["fresh_task_started"] is True
    assert transition.changed is True
    assert isinstance(transition.active_task, BookingTaskState)
    fresh = transition.active_task
    assert fresh.constraints.service_id == "svc-fullbody"
    assert fresh.constraints.doctor_id is None
    assert fresh.constraints.device_key is None
    assert fresh.constraints.date is None
    assert fresh.constraints.time is None
    assert fresh.option_snapshot is None
    assert fresh.derived.selected_slot_ref is None
    assert fresh.write_authorization.operation == "booking"
    assert fresh.write_authorization.source_turn_id == "fresh-booking-turn"
    assert fresh.write_authorization != old.write_authorization

    continuation = TurnOperation(
        type="book",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-10-08"),
        ),
        execution_intent="execute",
        active_task_relationship="continue",
    )
    continuation_step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="clarify",
        clarification_field="time",
        response_goal="clarification",
    )
    continued = adapt_matching_active_task_step(
        continuation_step,
        operation=continuation,
        active_task=fresh,
        context=_context(),
    )
    continued_state = apply_step_state(
        fresh,
        step=continued,
        operation=continuation,
        reads=None,
        now=NOW + timedelta(days=3),
        turn_id="fresh-booking-date-turn",
    ).active_task

    assert isinstance(continued_state, BookingTaskState)
    assert continued_state.constraints.date == DateConstraint(
        mode="exact",
        start_date="2026-10-08",
    )
    assert continued_state.constraints.time is None
    assert continued_state.write_authorization == fresh.write_authorization


def test_fresh_booking_replaces_persisted_flow_by_cancel_then_create(monkeypatch) -> None:
    old = _stale_reschedule()
    fresh = BookingTaskState(
        write_authorization=_authorized("booking", "fresh-booking-turn"),
        constraints=CustomerConstraints(service_id="svc-fullbody"),
    )
    expected = PersistedActiveTask(
        active_task=old,
        flow_id=uuid4(),
        flow_version=7,
    )
    calls: list[tuple[str, object]] = []

    def fake_cancel(_db, **kwargs):
        calls.append(("cancel", kwargs))
        assert kwargs["expected"] is expected
        assert kwargs["reason"] == "fresh_customer_task_replaced_active_task"

    persisted_result = PersistedActiveTask(
        active_task=fresh,
        flow_id=uuid4(),
        flow_version=1,
    )

    def fake_save(_db, **kwargs):
        calls.append(("save", kwargs))
        assert kwargs["active_task"] is fresh
        assert kwargs["expected"] is None
        return persisted_result

    monkeypatch.setattr(orchestrator, "cancel_active_task", fake_cancel)
    monkeypatch.setattr(orchestrator, "save_active_task", fake_save)

    result = orchestrator._persist_final_task(
        db=object(),
        workspace_id=UUID("11111111-1111-4111-8111-111111111111"),
        conversation_id=UUID("22222222-2222-4222-8222-222222222222"),
        patient_id=UUID("33333333-3333-4333-8333-333333333333"),
        run_id=UUID("44444444-4444-4444-8444-444444444444"),
        initial=expected,
        final_task=fresh,
        cancelled_existing_task=False,
        cancelled_existing_task_reason=None,
        replaced_existing_task=True,
        replaced_existing_task_reason="fresh_customer_task_replaced_active_task",
        completed_existing_task_result=None,
    )

    assert result is persisted_result
    assert [kind for kind, _ in calls] == ["cancel", "save"]
