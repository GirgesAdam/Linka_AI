from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.v2 import turn_interpreter as interpreter
from app.agents.v2.semantic_context import (
    SemanticContext,
    build_semantic_context,
    ground_turn_references,
)
from app.agents.v2.semantic_state_view import with_safe_action_context
from app.agents.v2.turn_contract import (
    AppointmentSelector,
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import (
    _build_interpreter_messages,
    interpret_customer_turn_v2,
    merge_verified_action_context,
    merge_verified_read_context,
)
from app.services.agent_v2.planner import PlannerContext, plan_turn


def _catalog() -> dict[str, object]:
    return {
        "services": [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "name": "ليزر إبط",
                "category": "laser",
                "requires_laser_device": True,
                "laser_devices": [
                    {"device_key": "candela_gentle", "device_name": "Candela Gentle"}
                ],
            }
        ],
        "doctors": [
            {
                "id": "22222222-2222-4222-8222-222222222222",
                "name": "مريم",
                "service_ids": ["11111111-1111-4111-8111-111111111111"],
            }
        ],
        "appointments": [
            {
                "appointment_id": "33333333-3333-4333-8333-333333333333",
                "service_id": "11111111-1111-4111-8111-111111111111",
                "doctor_id": "22222222-2222-4222-8222-222222222222",
                "status": "confirmed",
                "start_local": "2026-09-17T19:00:00+03:00",
            }
        ],
    }


def _context_with_verified_availability(*, option_count: int) -> SemanticContext:
    context = build_semantic_context(_catalog())
    return SemanticContext(
        model_input={
            **context.model_input,
            "recent_verified_read": {
                "operation_type": "availability",
                "service_ref": "S1",
                "date": {
                    "mode": "range",
                    "start_date": "2026-09-12",
                    "end_date": "2026-09-14",
                },
                "availability_option_count": option_count,
                "availability_found": option_count > 0,
            },
        },
        reference_map=context.reference_map,
    )


def _next_available_turn(*, condition: str) -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                entities=TurnEntities(date=DateConstraint(mode="next_available")),
                selection=None,
                package_usage="unspecified",
                continues_previous=True,
                continuation_condition=condition,
            )
        ],
        safety_signals=[],
    )


def test_semantic_context_hides_canonical_ids_and_keeps_ephemeral_refs() -> None:
    context = build_semantic_context(_catalog())
    serialized = json.dumps(context.model_input, ensure_ascii=False)

    assert "11111111-1111-4111-8111-111111111111" not in serialized
    assert "22222222-2222-4222-8222-222222222222" not in serialized
    assert "33333333-3333-4333-8333-333333333333" not in serialized
    assert context.resolve("S1", expected_kind="service") == (
        "11111111-1111-4111-8111-111111111111"
    )
    assert context.resolve("D1", expected_kind="doctor") == (
        "22222222-2222-4222-8222-222222222222"
    )
    assert context.resolve("A1", expected_kind="appointment") == (
        "33333333-3333-4333-8333-333333333333"
    )


def test_interpreter_messages_preserve_native_conversation_roles() -> None:
    context = build_semantic_context(_catalog(), active_task={"type": "booking"})
    history = [
        HumanMessage(content="عايزة أحجز جلسة"),
        AIMessage(content="تمام، أنهي خدمة؟"),
        HumanMessage(content="ليزر الإبط"),
    ]
    messages = _build_interpreter_messages(
        history=history,
        semantic_context=context,
        timezone_name="Africa/Cairo",
        local_now=datetime.fromisoformat("2026-09-11T15:00:00+03:00"),
    )

    assert isinstance(messages[0], SystemMessage)
    system_prompt = str(messages[0].content)
    assert "presented_availability is a server-owned view" in system_prompt
    assert "selection kind=ref" in system_prompt
    assert "Never invent an option ref" in system_prompt
    assert "old appointments and any that were cancelled" in str(messages[0].content)
    assert "own scheduled appointment/date/time" in str(messages[0].content)
    assert "availability means open/bookable" in str(messages[0].content)
    assert "requested_patient_details controls" in str(messages[0].content)
    assert "requested_package_details controls the exact read scope" in str(messages[0].content)
    assert "recent_verified_action is a completed book" in str(messages[0].content)
    assert "customer order: cancel_appointment" in str(messages[0].content)
    assert "then availability" in str(messages[0].content)
    assert isinstance(messages[1], SystemMessage)
    assert isinstance(messages[2], HumanMessage)
    assert isinstance(messages[3], AIMessage)
    assert isinstance(messages[4], HumanMessage)
    assert messages[4].content == "ليزر الإبط"


def test_invented_or_wrong_kind_refs_are_removed_without_text_recovery() -> None:
    context = build_semantic_context(_catalog())
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(text="ليزر الإبط", ref="D1", candidate_refs=["S99"])
                ),
                selection=None,
                package_usage="unspecified",
            )
        ],
        safety_signals=[],
    )

    grounded = ground_turn_references(turn, context)
    entity = grounded.operations[0].entities.service
    assert entity is not None
    assert entity.text == "ليزر الإبط"
    assert entity.ref is None
    assert entity.candidate_refs == []


def test_conditional_fallback_keeps_verified_date_when_previous_availability_succeeded() -> None:
    merged = merge_verified_read_context(
        _next_available_turn(condition="if_previous_no_availability"),
        _context_with_verified_availability(option_count=2),
    )

    date = merged.operations[0].entities.date
    assert date is not None
    assert date.mode == "range"
    assert date.start_date == "2026-09-12"
    assert date.end_date == "2026-09-14"


def test_conditional_fallback_activates_when_previous_availability_was_empty() -> None:
    merged = merge_verified_read_context(
        _next_available_turn(condition="if_previous_no_availability"),
        _context_with_verified_availability(option_count=0),
    )

    date = merged.operations[0].entities.date
    assert date is not None
    assert date.mode == "next_available"


def test_unconditional_nearest_request_is_not_suppressed_by_previous_success() -> None:
    merged = merge_verified_read_context(
        _next_available_turn(condition="always"),
        _context_with_verified_availability(option_count=2),
    )

    date = merged.operations[0].entities.date
    assert date is not None
    assert date.mode == "next_available"


def test_same_turn_pulse_purchase_device_carries_to_laser_booking_only(monkeypatch) -> None:
    context = build_semantic_context(_catalog())
    semantic_result = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="buy_pulse_pack",
                entities=TurnEntities(
                    device=EntityReference(
                        text="Candela Gentle",
                        ref="V1",
                        candidate_refs=[],
                    ),
                    pulse_count=1000,
                ),
                execution_intent="execute",
            ),
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(
                        text="ليزر إبط",
                        ref="S1",
                        candidate_refs=[],
                    ),
                    date=DateConstraint(
                        mode="exact",
                        start_date="2026-09-25",
                    ),
                ),
                package_usage="unspecified",
                execution_intent="execute",
            ),
        ],
        safety_signals=[],
    )
    monkeypatch.setattr(interpreter, "build_realtime_interpreter_model", lambda: object())
    monkeypatch.setattr(
        interpreter,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=semantic_result,
            model_name="test-model",
        ),
    )

    turn = interpret_customer_turn_v2(
        history=[
            HumanMessage(
                content=(
                    "اشتريلي باقة 1000 Pulse على Candela Gentle "
                    "واحجزيلي ليزر الإبط يوم 25 سبتمبر"
                )
            )
        ],
        semantic_context=context,
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
    )

    booking = turn.operations[1]
    assert booking.entities.device is not None
    assert booking.entities.device.ref == "V1"
    assert booking.package_usage == "unspecified"


def test_same_turn_explicit_booking_device_overrides_pulse_purchase_device(monkeypatch) -> None:
    catalog = _catalog()
    catalog["services"][0]["laser_devices"].append(
        {"device_key": "prime_lase", "device_name": "Prime Lase"}
    )
    context = build_semantic_context(catalog)
    semantic_result = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="buy_pulse_pack",
                entities=TurnEntities(
                    device=EntityReference(text="Candela Gentle", ref="V1"),
                    pulse_count=1000,
                ),
                execution_intent="execute",
            ),
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(text="ليزر إبط", ref="S1"),
                    device=EntityReference(text="Prime Lase", ref="V2"),
                    date=DateConstraint(mode="exact", start_date="2026-09-25"),
                ),
                package_usage="unspecified",
                execution_intent="execute",
            ),
        ],
        safety_signals=[],
    )
    monkeypatch.setattr(interpreter, "build_realtime_interpreter_model", lambda: object())
    monkeypatch.setattr(
        interpreter,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=semantic_result,
            model_name="test-model",
        ),
    )

    turn = interpret_customer_turn_v2(
        history=[
            HumanMessage(
                content=(
                    "اشتريلي باقة 1000 Pulse على Candela Gentle "
                    "واحجزيلي ليزر الإبط على Prime Lase يوم 25 سبتمبر"
                )
            )
        ],
        semantic_context=context,
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
    )

    booking = turn.operations[1]
    assert booking.entities.device is not None
    assert booking.entities.device.ref == "V2"
    assert booking.package_usage == "unspecified"


def test_device_followup_uses_canonical_ref_and_inherits_verified_service(monkeypatch) -> None:
    catalog = {
        "services": [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "name": "ليزر إبط",
                "requires_laser_device": True,
                "laser_devices": [
                    {"device_key": "prime_lase", "device_name": "Prime Lase"},
                    {"device_key": "candela_gentle", "device_name": "Candela Gentle"},
                ],
            }
        ],
        "doctors": [],
        "appointments": [],
    }
    base = build_semantic_context(catalog)
    context = SemanticContext(
        model_input={
            **base.model_input,
            "recent_verified_read": {
                "operation_type": "pricing",
                "service_ref": "S1",
            },
        },
        reference_map=base.reference_map,
    )
    semantic_result = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="pricing",
                entities=TurnEntities(
                    device=EntityReference(
                        text="Candela",
                        ref="V2",
                        candidate_refs=[],
                    )
                ),
                selection=None,
                package_usage="unspecified",
                requested_service_details=["price"],
                execution_intent="informational",
                continues_previous=True,
            )
        ],
        safety_signals=[],
    )
    monkeypatch.setattr(interpreter, "build_realtime_interpreter_model", lambda: object())
    monkeypatch.setattr(
        interpreter,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=semantic_result,
            model_name="test-model",
        ),
    )

    turn = interpret_customer_turn_v2(
        history=[
            HumanMessage(content="ليزر الإبط بكام؟"),
            AIMessage(content="Prime Lase — 550 جنيه، Candela Gentle — 650 جنيه. تحب أي جهاز؟"),
            HumanMessage(content="طب Candela؟"),
        ],
        semantic_context=context,
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
    )
    operation = turn.operations[0]
    assert operation.entities.service is not None
    assert operation.entities.service.ref == "S1"
    assert operation.entities.device is not None
    assert operation.entities.device.ref == "V2"

    step = plan_turn(
        turn,
        PlannerContext(
            semantic_context=context,
            active_task=None,
            now=datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
        ),
    ).steps[0]
    assert step.facts["service_id"] == "11111111-1111-4111-8111-111111111111"
    assert step.facts["device_key"] == "candela_gentle"


def _context_with_recent_verified_booking() -> SemanticContext:
    context = build_semantic_context(_catalog())
    return with_safe_action_context(
        context,
        action_context={
            "operation_type": "book",
            "appointment_id": "33333333-3333-4333-8333-333333333333",
            "service_id": "11111111-1111-4111-8111-111111111111",
            "doctor_id": "22222222-2222-4222-8222-222222222222",
            "start_at": "2026-09-17T16:00:00+00:00",
            "status": "confirmed",
            "date": {"mode": "exact", "start_date": "2026-09-17"},
            "time": {"mode": "exact", "start_time": "19:00"},
            "package_usage": "unspecified",
        },
    )


def test_recent_completed_booking_revocation_binds_verified_appointment_target() -> None:
    context = _context_with_recent_verified_booking()
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="cancel_appointment",
                entities=TurnEntities(),
                source_appointment=AppointmentSelector(
                    appointment=EntityReference(
                        text=None,
                        ref=None,
                        candidate_refs=[],
                    )
                ),
                execution_intent="execute",
                continues_previous=True,
            )
        ],
        safety_signals=[],
    )

    merged = merge_verified_action_context(turn, context)

    selector = merged.operations[0].source_appointment
    assert selector is not None
    assert selector.appointment is not None
    assert selector.appointment.ref == "A1"


def test_recent_booking_context_does_not_create_cancellation_for_informational_followup() -> None:
    context = _context_with_recent_verified_booking()
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="pricing",
                entities=TurnEntities(
                    service=EntityReference(text="ليزر إبط", ref="S1")
                ),
                requested_service_details=["price"],
                execution_intent="informational",
                continues_previous=False,
            )
        ],
        safety_signals=[],
    )

    merged = merge_verified_action_context(turn, context)

    assert [operation.type for operation in merged.operations] == ["pricing"]


def test_recent_booking_context_requires_semantic_continuation_before_binding_cancel_target() -> None:
    context = _context_with_recent_verified_booking()
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="cancel_appointment",
                entities=TurnEntities(),
                execution_intent="execute",
                continues_previous=False,
            )
        ],
        safety_signals=[],
    )

    merged = merge_verified_action_context(turn, context)

    assert merged.operations[0].source_appointment is None


def test_recent_booking_context_never_overrides_an_explicit_cancel_target() -> None:
    catalog = _catalog()
    catalog["appointments"].append(
        {
            "appointment_id": "44444444-4444-4444-8444-444444444444",
            "service_id": "11111111-1111-4111-8111-111111111111",
            "doctor_id": "22222222-2222-4222-8222-222222222222",
            "status": "confirmed",
            "start_local": "2026-09-18T19:00:00+03:00",
        }
    )
    context = build_semantic_context(catalog)
    context = with_safe_action_context(
        context,
        action_context={
            "operation_type": "book",
            "appointment_id": "33333333-3333-4333-8333-333333333333",
            "service_id": "11111111-1111-4111-8111-111111111111",
            "doctor_id": "22222222-2222-4222-8222-222222222222",
            "start_at": "2026-09-17T16:00:00+00:00",
            "status": "confirmed",
            "package_usage": "unspecified",
        },
    )
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="cancel_appointment",
                entities=TurnEntities(),
                source_appointment=AppointmentSelector(
                    appointment=EntityReference(ref="A2")
                ),
                execution_intent="execute",
                continues_previous=True,
            )
        ],
        safety_signals=[],
    )

    merged = merge_verified_action_context(turn, context)

    selector = merged.operations[0].source_appointment
    assert selector is not None
    assert selector.appointment is not None
    assert selector.appointment.ref == "A2"


def test_recent_booking_revocation_targets_new_booking_when_older_appointment_exists() -> None:
    context = build_semantic_context(_catalog())
    context = with_safe_action_context(
        context,
        action_context={
            "operation_type": "book",
            "appointment_id": "44444444-4444-4444-8444-444444444444",
            "service_id": "11111111-1111-4111-8111-111111111111",
            "doctor_id": "22222222-2222-4222-8222-222222222222",
            "start_at": "2026-09-18T16:00:00+00:00",
            "status": "confirmed",
            "package_usage": "unspecified",
        },
    )
    recent_ref = context.model_input["recent_verified_action"]["appointment_ref"]
    assert recent_ref == "A2"
    assert context.resolve("A1", expected_kind="appointment") == (
        "33333333-3333-4333-8333-333333333333"
    )
    assert context.resolve("A2", expected_kind="appointment") == (
        "44444444-4444-4444-8444-444444444444"
    )

    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="cancel_appointment",
                entities=TurnEntities(),
                execution_intent="execute",
                continues_previous=True,
            )
        ],
        safety_signals=[],
    )
    merged = merge_verified_action_context(turn, context)

    selector = merged.operations[0].source_appointment
    assert selector is not None
    assert selector.appointment is not None
    assert selector.appointment.ref == "A2"


def test_recent_booking_revocation_preserves_compound_cancel_then_availability_order() -> None:
    context = _context_with_recent_verified_booking()
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="cancel_appointment",
                entities=TurnEntities(),
                execution_intent="execute",
                continues_previous=True,
            ),
            TurnOperation(
                type="availability",
                entities=TurnEntities(
                    service=EntityReference(text="ليزر إبط", ref="S1")
                ),
                execution_intent="informational",
            ),
        ],
        safety_signals=[],
    )

    merged = merge_verified_action_context(turn, context)

    assert [operation.type for operation in merged.operations] == [
        "cancel_appointment",
        "availability",
    ]
    cancel_selector = merged.operations[0].source_appointment
    assert cancel_selector is not None
    assert cancel_selector.appointment is not None
    assert cancel_selector.appointment.ref == "A1"
    assert merged.operations[1].source_appointment is None
