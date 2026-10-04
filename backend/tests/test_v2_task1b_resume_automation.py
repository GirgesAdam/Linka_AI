from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.v2.semantic_context import (
    SemanticContext,
    SemanticReferenceTarget,
    build_semantic_context,
)
from app.agents.v2.semantic_state_view import with_safe_automation_context
from app.agents.v2.turn_contract import (
    AppointmentSelector,
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import (
    _build_interpreter_messages,
    enforce_unscoped_task_boundary,
    isolate_fresh_task_context,
    merge_automation_context,
)
from app.models.message import Message
from app.services.agent_v2.active_task_progress import adapt_matching_active_task_step
from app.services.agent_v2.live_chat import _recent_verified_action_context_from_outbounds
from app.services.agent_v2.planner import PlanStep, PlannerContext, plan_turn
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    OptionChoice,
    OptionSnapshot,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state

NOW = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)


def _authorization(source: str = "resume-source") -> WriteAuthorization:
    return WriteAuthorization(
        operation="booking",
        authorized=True,
        source_turn_id=source,
        granted_at=NOW,
    )


def _resume_state(*, date: str = "2026-10-08", time_value: str | None = None) -> BookingTaskState:
    return BookingTaskState(
        status="awaiting_choice",
        write_authorization=_authorization(),
        constraints=CustomerConstraints(
            service_id="svc-underarm",
            doctor_id="doc-mary",
            device_key="deka",
            date=DateConstraint(mode="exact", start_date=date),
            time=(
                TimeConstraint(mode="exact", start_time=time_value)
                if time_value is not None
                else None
            ),
        ),
        option_snapshot=OptionSnapshot(
            snapshot_id="old-options",
            purpose="booking_slot",
            task_version=1,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
            options=[OptionChoice(ref="old-slot", payload={"start_time_24h": "16:00"})],
        ),
    )


def _booking_context() -> SemanticContext:
    return build_semantic_context(
        {
            "services": [
                {
                    "id": "svc-underarm",
                    "name": "Under Arm",
                    "requires_laser_device": True,
                    "laser_devices": [{"device_key": "deka", "device_name": "DEKA Again"}],
                }
            ],
            "doctors": [
                {
                    "id": "doc-mary",
                    "name": "Mary",
                    "service_ids": ["svc-underarm"],
                }
            ],
            "appointments": [
                {
                    "id": "apt-reminder",
                    "service_id": "svc-underarm",
                    "doctor_id": "doc-mary",
                    "laser_device_key": "deka",
                    "status": "confirmed",
                    "start_local": "2026-10-08T17:00:00+03:00",
                }
            ],
            "packages": [],
        }
    )


def test_valid_resume_retains_stable_constraints_and_invalidates_old_options() -> None:
    active = _resume_state()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-10-10")
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
        active_task=active,
        context=_booking_context(),
        now=NOW,
    )
    transition = apply_step_state(
        active,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="resume-turn",
    )
    resumed = transition.active_task

    assert isinstance(resumed, BookingTaskState)
    assert resumed.constraints.service_id == "svc-underarm"
    assert resumed.constraints.device_key == "deka"
    assert resumed.constraints.doctor_id == "doc-mary"
    assert resumed.constraints.date == DateConstraint(mode="exact", start_date="2026-10-10")
    assert resumed.option_snapshot is None
    assert resumed.derived.selected_slot_ref is None
    assert resumed.write_authorization == active.write_authorization


def test_resume_drops_past_date_and_time_without_dropping_service_device() -> None:
    active = _resume_state(date="2026-10-03", time_value="16:00")
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="execute",
        active_task_relationship="continue",
    )
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
        active_task=active,
        context=_booking_context(),
        now=NOW,
    )
    transition = apply_step_state(
        active,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="resume-after-past-turn",
    )
    resumed = transition.active_task

    assert isinstance(resumed, BookingTaskState)
    assert resumed.constraints.service_id == "svc-underarm"
    assert resumed.constraints.device_key == "deka"
    assert resumed.constraints.doctor_id == "doc-mary"
    assert resumed.constraints.date is None
    assert resumed.constraints.time is None
    assert resumed.option_snapshot is None
    assert resumed.derived.selected_slot_ref is None


def test_automation_context_exposes_opaque_refs_not_canonical_ids() -> None:
    context = _booking_context()
    scoped = with_safe_automation_context(
        context,
        automation_context={
            "source": "automation_engine",
            "automation_rule_key": "appointment_reminder_6h",
            "appointment_id": "apt-reminder",
            "service_id": "svc-underarm",
            "device_key": "deka",
            "appointment_status": "confirmed",
            "start_at": "2026-10-08T14:00:00+00:00",
        },
    )

    automation = scoped.model_input["automation_context"]
    assert automation["source"] == "automation_engine"
    assert automation["automation_rule_key"] == "appointment_reminder_6h"
    assert automation["appointment_ref"].startswith("A")
    assert automation["service_ref"].startswith("S")
    assert automation["device_ref"].startswith("V")
    assert "appointment_id" not in automation
    assert "service_id" not in automation
    assert "device_key" not in automation
    assert scoped.model_input["recent_verified_action"] == {}
    assert scoped.model_input.get("recent_verified_read", {}) == {}


def test_automation_appointment_action_binds_server_target_not_model_target() -> None:
    context = SemanticContext(
        model_input={
            "automation_context": {
                "source": "automation_engine",
                "automation_rule_key": "appointment_reminder_6h",
                "appointment_ref": "A2",
            }
        },
        reference_map={
            "A1": SemanticReferenceTarget(kind="appointment", canonical_id="apt-wrong"),
            "A2": SemanticReferenceTarget(kind="appointment", canonical_id="apt-reminder"),
        },
    )
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(time=TimeConstraint(mode="exact", start_time="18:00")),
        source_appointment=AppointmentSelector(
            appointment=EntityReference(ref="A1", text="old prose target")
        ),
        execution_intent="execute",
        automation_context_relationship="appointment_action",
    )

    merged = merge_automation_context(
        TiaTurnUnderstanding(operations=[operation]),
        context,
    )
    target = merged.operations[0].source_appointment.appointment

    assert target is not None
    assert target.ref == "A2"
    assert target.text is None


def _reminder_action_context() -> SemanticContext:
    return SemanticContext(
        model_input={
            "automation_context": {
                "source": "automation_engine",
                "automation_rule_key": "appointment_reminder_6h",
                "appointment_ref": "A2",
                # UTC calendar date intentionally differs from Cairo local date.
                "start_at": "2026-10-07T22:30:00+00:00",
            }
        },
        reference_map={
            "A2": SemanticReferenceTarget(
                kind="appointment",
                canonical_id="apt-reminder",
            )
        },
    )


def test_reminder_time_only_reschedule_preserves_verified_local_appointment_date() -> None:
    context = _reminder_action_context()
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            time=TimeConstraint(mode="exact", start_time="18:00"),
        ),
        execution_intent="execute",
        automation_context_relationship="appointment_action",
    )

    merged = merge_automation_context(
        TiaTurnUnderstanding(operations=[operation]),
        context,
        timezone_name="Africa/Cairo",
    )
    merged_operation = merged.operations[0]

    assert merged_operation.source_appointment is not None
    assert merged_operation.source_appointment.appointment == EntityReference(ref="A2")
    assert merged_operation.entities.date == DateConstraint(
        mode="exact",
        start_date="2026-10-08",
    )
    assert merged_operation.entities.time == TimeConstraint(
        mode="exact",
        start_time="18:00",
    )

    plan = plan_turn(
        merged,
        PlannerContext(
            semantic_context=context,
            active_task=None,
            now=NOW,
        ),
    )
    step = plan.steps[0]
    assert step.disposition == "read"
    assert [request.kind for request in step.reads] == ["appointments", "availability"]
    assert step.facts["date"] == {
        "mode": "exact",
        "start_date": "2026-10-08",
        "end_date": None,
    }


def test_reminder_reschedule_without_date_or_time_still_asks_for_date() -> None:
    context = _reminder_action_context()
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(),
        execution_intent="execute",
        automation_context_relationship="appointment_action",
    )

    merged = merge_automation_context(
        TiaTurnUnderstanding(operations=[operation]),
        context,
        timezone_name="Africa/Cairo",
    )
    merged_operation = merged.operations[0]

    assert merged_operation.entities.date is None
    assert merged_operation.entities.time is None

    plan = plan_turn(
        merged,
        PlannerContext(
            semantic_context=context,
            active_task=None,
            now=NOW,
        ),
    )
    step = plan.steps[0]
    assert step.disposition == "clarify"
    assert step.clarification_field == "date"
    assert step.state_action == "start_reschedule"
    assert [request.kind for request in step.reads] == ["appointments"]


def test_reminder_reschedule_explicit_new_date_wins_over_verified_original_date() -> None:
    context = _reminder_action_context()
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-10-09"),
            time=TimeConstraint(mode="exact", start_time="18:00"),
        ),
        execution_intent="execute",
        automation_context_relationship="appointment_action",
    )

    merged = merge_automation_context(
        TiaTurnUnderstanding(operations=[operation]),
        context,
        timezone_name="Africa/Cairo",
    )
    merged_operation = merged.operations[0]

    assert merged_operation.entities.date == DateConstraint(
        mode="exact",
        start_date="2026-10-09",
    )
    assert merged_operation.entities.time == TimeConstraint(
        mode="exact",
        start_time="18:00",
    )


def test_post_visit_next_session_reuses_only_verified_stable_treatment_facts() -> None:
    context = SemanticContext(
        model_input={
            "automation_context": {
                "source": "automation_engine",
                "automation_rule_key": "post_visit_followup",
                "appointment_ref": "A1",
                "service_ref": "S1",
                "device_ref": "V1",
                "start_at": "2026-10-03T14:00:00+00:00",
            }
        },
        reference_map={
            "A1": SemanticReferenceTarget(kind="appointment", canonical_id="apt-completed"),
            "S1": SemanticReferenceTarget(kind="service", canonical_id="svc-underarm"),
            "V1": SemanticReferenceTarget(kind="device", canonical_id="deka"),
            "D1": SemanticReferenceTarget(kind="doctor", canonical_id="doc-mary"),
        },
    )
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="execute",
        fresh_task=True,
        fresh_task_explicit_fields=[],
        automation_context_relationship="next_session",
    )
    merged = merge_automation_context(TiaTurnUnderstanding(operations=[operation]), context)
    entities = merged.operations[0].entities

    assert entities.service == EntityReference(ref="S1")
    assert entities.device == EntityReference(ref="V1")
    assert entities.doctor is None
    assert entities.date is None
    assert entities.time is None
    assert merged.operations[0].source_appointment is None


def test_system_automation_boundary_is_not_recent_verified_action_passthrough() -> None:
    old_ai = Message(
        workspace_id=uuid4(),
        conversation_id=uuid4(),
        sender_type="ai",
        direction="outbound",
        message_type="text",
        content="old booking response",
        delivery_status="sent",
        metadata_json={
            "runtime": "v2",
            "v2_action_context": {
                "operation_type": "book",
                "appointment_id": "old-appointment",
            },
        },
    )
    system_automation = Message(
        workspace_id=old_ai.workspace_id,
        conversation_id=old_ai.conversation_id,
        sender_type="system",
        direction="outbound",
        message_type="text",
        content="reminder",
        delivery_status="sent",
        metadata_json={
            "source": "automation_engine",
            "appointment_id": "reminder-appointment",
        },
    )

    assert (
        _recent_verified_action_context_from_outbounds([system_automation, old_ai])
        is None
    )



def test_unscoped_booking_is_forced_fresh_and_old_dialogue_constraints_are_removed() -> None:
    context = SemanticContext(
        model_input={
            "active_task": {},
            "recent_verified_read": {},
            "recent_verified_action": {},
            "automation_context": {},
        },
        reference_map={},
    )
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(ref="S1", text="stale service"),
                    doctor=EntityReference(ref="D1", text="stale doctor"),
                    device=EntityReference(ref="V1", text="stale device"),
                    date=DateConstraint(mode="exact", start_date="2026-10-10"),
                    time=TimeConstraint(mode="after", start_time="18:00"),
                ),
                execution_intent="execute",
                fresh_task=False,
                fresh_task_explicit_fields=[],
            )
        ]
    )

    bounded = enforce_unscoped_task_boundary(turn, context)
    operation = bounded.operations[0]
    assert operation.fresh_task is True

    isolated = isolate_fresh_task_context(bounded).operations[0]
    assert isolated.entities.service is None
    assert isolated.entities.doctor is None
    assert isolated.entities.device is None
    assert isolated.entities.date is None
    assert isolated.entities.time is None
    assert isolated.continues_previous is False


def test_unscoped_booking_preserves_only_semantically_explicit_service() -> None:
    context = SemanticContext(
        model_input={
            "active_task": {},
            "recent_verified_read": {},
            "recent_verified_action": {},
            "automation_context": {},
        },
        reference_map={},
    )
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(ref="S1", text="Under Arm"),
                    device=EntityReference(ref="V1", text="old device"),
                    date=DateConstraint(mode="exact", start_date="2026-10-10"),
                ),
                execution_intent="execute",
                fresh_task=False,
                fresh_task_explicit_fields=["service"],
            )
        ]
    )

    bounded = enforce_unscoped_task_boundary(turn, context)
    isolated = isolate_fresh_task_context(bounded).operations[0]

    assert bounded.operations[0].fresh_task is True
    assert isolated.entities.service == EntityReference(ref="S1", text="Under Arm")
    assert isolated.entities.device is None
    assert isolated.entities.date is None
    assert isolated.entities.time is None


def test_verified_read_continuation_is_not_forced_fresh_without_active_task() -> None:
    context = SemanticContext(
        model_input={
            "active_task": {},
            "recent_verified_read": {"operation_type": "availability", "service_ref": "S1"},
            "recent_verified_action": {},
            "automation_context": {},
        },
        reference_map={},
    )
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="execute",
        continues_previous=True,
        fresh_task=False,
    )

    bounded = enforce_unscoped_task_boundary(
        TiaTurnUnderstanding(operations=[operation]),
        context,
    )

    assert bounded.operations[0].fresh_task is False
    assert bounded.operations[0].continues_previous is True


def test_verified_pulse_purchase_booking_continuation_is_not_forced_fresh() -> None:
    context = SemanticContext(
        model_input={
            "active_task": {},
            "recent_verified_read": {},
            "recent_verified_action": {"operation_type": "buy_pulse_pack", "device_ref": "V1"},
            "automation_context": {},
        },
        reference_map={},
    )
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="execute",
        continues_previous=True,
        fresh_task=False,
    )

    bounded = enforce_unscoped_task_boundary(
        TiaTurnUnderstanding(operations=[operation]),
        context,
    )

    assert bounded.operations[0].fresh_task is False



def test_automation_context_is_ordered_after_native_dialogue_for_interpreter() -> None:
    scoped = with_safe_automation_context(
        _booking_context(),
        automation_context={
            "source": "automation_engine",
            "automation_rule_key": "appointment_reminder_6h",
            "appointment_id": "apt-reminder",
            "service_id": "svc-underarm",
            "device_key": "deka",
            "appointment_status": "confirmed",
            "start_at": "2026-10-08T14:00:00+00:00",
        },
    )
    messages = _build_interpreter_messages(
        history=[
            HumanMessage(content="عايز احجز Under Arm"),
            AIMessage(content="تقصد ديسمبر؟"),
            HumanMessage(content="تمام"),
        ],
        semantic_context=scoped,
        timezone_name="Africa/Cairo",
        local_now=NOW,
    )

    marker_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, SystemMessage)
        and "CONVERSATION_ORDER:" in str(message.content)
    ]
    assert len(marker_indexes) == 1
    marker_index = marker_indexes[0]
    prior_ai_index = next(
        index
        for index, message in enumerate(messages)
        if isinstance(message, AIMessage) and "ديسمبر" in str(message.content)
    )
    assert prior_ai_index < marker_index < len(messages) - 1
    assert isinstance(messages[-1], HumanMessage)
    assert messages[-1].content == "تمام"


def test_social_turn_after_automation_is_normalized_to_read_only_acknowledgement() -> None:
    context = SemanticContext(
        model_input={
            "automation_context": {
                "source": "automation_engine",
                "automation_rule_key": "appointment_reminder_6h",
            }
        },
        reference_map={},
    )
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="social",
                entities=TurnEntities(),
                execution_intent="informational",
                automation_context_relationship="none",
            )
        ]
    )

    merged = merge_automation_context(turn, context)

    assert merged.operations[0].automation_context_relationship == "acknowledge"


def test_non_social_active_task_turn_is_not_rewritten_as_automation_acknowledgement() -> None:
    context = SemanticContext(
        model_input={
            "automation_context": {
                "source": "automation_engine",
                "automation_rule_key": "appointment_reminder_6h",
            }
        },
        reference_map={},
    )
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="continue_active",
                entities=TurnEntities(),
                execution_intent="execute",
                automation_context_relationship="none",
            )
        ]
    )

    merged = merge_automation_context(turn, context)

    assert merged.operations[0].automation_context_relationship == "none"
