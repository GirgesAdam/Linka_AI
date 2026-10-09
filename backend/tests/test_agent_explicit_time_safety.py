from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage

import app.agents.turn_interpreter as turn_interpreter
import app.services.agent_chat as agent_chat
from app.agents.capability_policy import resolve_capability_policy
from app.agents.explicit_time import (
    extract_explicit_hhmm_values,
    extract_single_explicit_hhmm,
)
from app.agents.turn_interpreter import UnifiedTurnDecision
from app.agents.turn_models import SemanticEntityHints
from app.agents.v2.turn_contract import (
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import _preserve_explicit_clock_constraints_v2
from app.services.agent_v2.planner import (
    _EXPLICIT_TIME_AUTHORITY_PARAM,
    PlannerContext,
    _explicit_time_authority_parameters,
)
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    WriteAuthorization,
)

SERVICE_ID = "22222222-2222-4222-8222-222222222222"
DOCTOR_ID = "11111111-1111-4111-8111-111111111111"
DATE = "2026-10-10"


def _hints(**updates) -> SemanticEntityHints:
    values = {
        "service_query": "PRP ??????",
        "branch_query": None,
        "doctor_query": "???",
        "service_id": SERVICE_ID,
        "service_candidate_ids": [],
        "branch_id": None,
        "branch_candidate_ids": [],
        "doctor_id": DOCTOR_ID,
        "doctor_candidate_ids": [],
        "laser_device_key": None,
        "package_sessions_count": None,
        "requested_items": [],
        "appointment_id": None,
        "requested_date": DATE,
        "requested_start_time": "15:00",
        "not_before_time": None,
        "not_after_time": None,
        "appointment_reference": None,
    }
    values.update(updates)
    return SemanticEntityHints(**values)


def _decision(
    *,
    requested_start_time: str | None = "15:00",
    capability: str = "appointment_creation",
    selection_time: str | None = None,
    action: str = "modify",
) -> UnifiedTurnDecision:
    return UnifiedTurnDecision(
        domains=["booking"],
        capabilities=["availability_discovery", capability]
        if capability != "availability_discovery"
        else ["availability_discovery"],
        risk_flags=[],
        flow_signal="start_reschedule" if capability == "appointment_reschedule" else "start_booking",
        package_intent="none",
        action=action,
        entity_hints=_hints(requested_start_time=requested_start_time),
        clear_entity_fields=[],
        selection_index=None,
        selection_time=selection_time,
        missing_information=[],
        recommended_handoff_category="other",
        recommended_handoff_priority="normal",
        confidence=0.9,
        reason="test",
    )


def _catalog() -> dict[str, object]:
    return {
        "services": [{"id": SERVICE_ID, "name": "PRP ??????"}],
        "branches": [],
        "doctors": [
            {
                "id": DOCTOR_ID,
                "name": "???",
                "service_ids": [SERVICE_ID],
            }
        ],
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("?????? 03:00", "03:00"),
        ("?????? 15:00", "15:00"),
        ("?????? 03:30", "03:30"),
        ("?????? 23:00", "23:00"),
        ("\u0627\u0644\u0633\u0627\u0639\u0629 \u0660\u0663:\u0660\u0660", "03:00"),
        ("\u0627\u0644\u0633\u0627\u0639\u0629 \u0661\u0665:\u0663\u0660", "15:30"),
        ("?????? 3:05", "03:05"),
    ],
)
def test_explicit_hhmm_extraction_preserves_clock_value(text: str, expected: str) -> None:
    assert extract_single_explicit_hhmm(text) == expected


@pytest.mark.parametrize("text", ["?????? 3", "3 ?????", "3 ?????", "????? 3", "??? 3"])
def test_colloquial_hours_are_not_claimed_by_explicit_hhmm_guard(text: str) -> None:
    assert extract_explicit_hhmm_values(text) == []


def test_semantic_post_validation_overrides_llm_03_to_15_drift(monkeypatch) -> None:
    bad = _decision(requested_start_time="15:00")
    monkeypatch.setattr(turn_interpreter, "build_realtime_interpreter_model", lambda: object())
    monkeypatch.setattr(
        turn_interpreter,
        "invoke_with_model_chain",
        lambda **kwargs: SimpleNamespace(value=bad),
    )

    result = turn_interpreter.interpret_customer_turn(
        flow=None,
        history=[HumanMessage(content="????? PRP ?????? ???? ?????? 03:00 ?? ???")],
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 10, 9, 21, 0),
        clinic_catalog=_catalog(),
    )

    assert result.entity_hints.requested_start_time == "03:00"


def test_semantic_post_validation_normalizes_arabic_indic_time(monkeypatch) -> None:
    bad = _decision(requested_start_time="15:30")
    monkeypatch.setattr(turn_interpreter, "build_realtime_interpreter_model", lambda: object())
    monkeypatch.setattr(
        turn_interpreter,
        "invoke_with_model_chain",
        lambda **kwargs: SimpleNamespace(value=bad),
    )

    result = turn_interpreter.interpret_customer_turn(
        flow=None,
        history=[HumanMessage(content="\u0639\u0627\u064a\u0632\u0629 PRP \u0628\u0643\u0631\u0629 \u0627\u0644\u0633\u0627\u0639\u0629 \u0660\u0663:\u0660\u0660")],
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 10, 9, 21, 0),
        clinic_catalog=_catalog(),
    )

    assert result.entity_hints.requested_start_time == "03:00"


def test_explicit_range_bound_keeps_its_exact_colon_time() -> None:
    decision = _decision(requested_start_time=None, capability="availability_discovery")
    decision = decision.model_copy(
        update={
            "entity_hints": decision.entity_hints.model_copy(
                update={"not_before_time": "15:00"}
            )
        }
    )
    corrected = turn_interpreter._preserve_explicit_clock_constraint(
        decision, latest_customer_text="????? ???????? ??? 03:00"
    )
    assert corrected.entity_hints.requested_start_time is None
    assert corrected.entity_hints.not_before_time == "03:00"


def test_critical_03_prefetch_uses_03_and_cannot_select_15(monkeypatch) -> None:
    corrected = turn_interpreter._preserve_explicit_clock_constraint(
        _decision(requested_start_time="15:00"),
        latest_customer_text="????? PRP ?????? ???? ?????? 03:00 ?? ???",
    ).as_semantic_decision()
    policy = resolve_capability_policy(corrected)
    calls: list[tuple[str, dict]] = []

    def fake_invoke_authorized_tool(*, tool_context, policy, tool_name, arguments):
        calls.append((tool_name, dict(arguments)))
        assert tool_name != "book_appointment"
        return {
            "ok": True,
            "date": DATE,
            "requested_start_time": "03:00",
            "requested_time_unavailable": True,
            "matching_slot_count": 0,
            "slots": [
                {
                    "start_time_24h": "15:00",
                    "start_local": "2026-10-10T15:00:00+03:00",
                    "doctor_id": DOCTOR_ID,
                }
            ],
        }

    monkeypatch.setattr(agent_chat, "_invoke_authorized_tool", fake_invoke_authorized_tool)
    context = SimpleNamespace(
        db=None,
        workspace=SimpleNamespace(id=uuid4(), timezone="Africa/Cairo"),
        patient=None,
        conversation=None,
        run_id=uuid4(),
    )
    results, _ = agent_chat._prefetch_read_tools(
        tool_context=context,
        policy=policy,
        decision=corrected,
        flow=None,
        grounded_mode=True,
    )

    assert calls[0][0] == "get_booking_options"
    assert calls[0][1]["requested_start_time"] == "03:00"
    assert agent_chat._exact_action_selection_index(
        decision=corrected,
        payload=results["get_booking_options"],
        required_capability="appointment_creation",
    ) is None
    assert all(name != "book_appointment" for name, _ in calls)


@pytest.mark.parametrize("flow_type", ["booking", "appointment_reschedule"])
def test_write_guard_blocks_slot_different_from_authoritative_explicit_time(flow_type: str) -> None:
    flow = SimpleNamespace(
        flow_type=flow_type,
        entity_state={agent_chat._EXPLICIT_TIME_AUTHORITY_KEY: "03:00"},
    )
    turn = _decision(requested_start_time="03:00").as_flow_turn_decision().model_copy(
        update={"action": "select_option", "selection_index": 1}
    )
    wrong_slot = {
        "start_time_24h": "15:00",
        "start_local": "2026-10-10T15:00:00+03:00",
    }
    assert not agent_chat._write_respects_explicit_time_authority(
        flow=flow, turn=turn, slot=wrong_slot
    )


def test_later_explicit_alternative_selection_replaces_authority() -> None:
    flow_state = {agent_chat._EXPLICIT_TIME_AUTHORITY_KEY: "03:00"}
    later = _decision(requested_start_time="15:00", action="select_option").as_flow_turn_decision().model_copy(
        update={"selection_index": 1}
    )
    updated = agent_chat._apply_explicit_time_authority(
        flow_state, turn=later, explicit_exact_time="15:00"
    )
    flow = SimpleNamespace(flow_type="booking", entity_state=updated)
    slot = {
        "start_time_24h": "15:00",
        "start_local": "2026-10-10T15:00:00+03:00",
    }
    assert updated[agent_chat._EXPLICIT_TIME_AUTHORITY_KEY] == "15:00"
    assert agent_chat._write_respects_explicit_time_authority(
        flow=flow, turn=later, slot=slot
    )


def test_positional_alternative_selection_is_not_blocked_after_unavailable_exact_time() -> None:
    flow = SimpleNamespace(
        flow_type="booking",
        entity_state={agent_chat._EXPLICIT_TIME_AUTHORITY_KEY: "03:00"},
    )
    turn = _decision(requested_start_time=None, action="select_option").as_flow_turn_decision().model_copy(
        update={"selection_index": 1}
    )
    alternative = {
        "start_time_24h": "15:00",
        "start_local": "2026-10-10T15:00:00+03:00",
    }
    assert agent_chat._write_respects_explicit_time_authority(
        flow=flow, turn=turn, slot=alternative
    )


def _v2_turn(*, value: str, ambiguity: str = "twelve_hour", operation_type: str = "book") -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type=operation_type,
                entities=TurnEntities(
                    time=TimeConstraint(
                        mode="exact",
                        start_time=value,
                        start_time_ambiguity=ambiguity,
                    )
                ),
            )
        ]
    )


def test_v2_explicit_03_disables_twelve_hour_resolution_before_clinic_hours() -> None:
    corrected = _preserve_explicit_clock_constraints_v2(
        _v2_turn(value="03:00", ambiguity="twelve_hour"),
        latest_customer_text="????? PRP ?????? ???? ?????? 03:00 ?? ???",
    )
    constraint = corrected.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "03:00"
    assert constraint.start_time_ambiguity == "none"


def test_v2_explicit_arabic_indic_time_is_canonical_and_unambiguous() -> None:
    corrected = _preserve_explicit_clock_constraints_v2(
        _v2_turn(value="15:30", ambiguity="twelve_hour"),
        latest_customer_text="\u0627\u0644\u0633\u0627\u0639\u0629 \u0660\u0663:\u0660\u0660",
    )
    constraint = corrected.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "03:00"
    assert constraint.start_time_ambiguity == "none"


def test_v2_colloquial_three_keeps_existing_ambiguity_contract() -> None:
    original = _v2_turn(value="03:00", ambiguity="twelve_hour")
    corrected = _preserve_explicit_clock_constraints_v2(
        original, latest_customer_text="?????? 3"
    )
    assert corrected == original


def test_v2_planner_carries_server_owned_explicit_time_only_for_matching_exact_intent() -> None:
    operation = _v2_turn(value="03:00", ambiguity="none").operations[0]
    context = PlannerContext(
        semantic_context=SimpleNamespace(),
        active_task=None,
        now=datetime(2026, 10, 9, 21, 0),
        explicit_user_time="03:00",
    )
    assert _explicit_time_authority_parameters(operation, context) == {
        _EXPLICIT_TIME_AUTHORITY_PARAM: "03:00"
    }


def test_v2_planner_does_not_carry_stale_authority_for_different_time() -> None:
    operation = _v2_turn(value="15:00", ambiguity="none").operations[0]
    context = PlannerContext(
        semantic_context=SimpleNamespace(),
        active_task=None,
        now=datetime(2026, 10, 9, 21, 0),
        explicit_user_time="03:00",
    )
    assert _explicit_time_authority_parameters(operation, context) == {}


def test_v2_confirmation_turn_keeps_persisted_exact_time_as_write_authority() -> None:
    active = BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking"),
        constraints=CustomerConstraints(
            time=TimeConstraint(
                mode="exact",
                start_time="15:00",
                start_time_ambiguity="none",
            )
        ),
    )
    operation = TurnOperation(type="book", execution_intent="execute", entities=TurnEntities())
    context = PlannerContext(
        semantic_context=SimpleNamespace(),
        active_task=active,
        now=datetime(2026, 10, 9, 21, 0),
        explicit_user_time=None,
    )
    assert _explicit_time_authority_parameters(operation, context) == {
        _EXPLICIT_TIME_AUTHORITY_PARAM: "15:00"
    }


def test_v2_new_explicit_alternative_replaces_persisted_exact_authority() -> None:
    active = BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking"),
        constraints=CustomerConstraints(
            time=TimeConstraint(
                mode="exact",
                start_time="03:00",
                start_time_ambiguity="none",
            )
        ),
    )
    operation = _v2_turn(value="15:00", ambiguity="none").operations[0]
    context = PlannerContext(
        semantic_context=SimpleNamespace(),
        active_task=active,
        now=datetime(2026, 10, 9, 21, 0),
        explicit_user_time="15:00",
    )
    assert _explicit_time_authority_parameters(operation, context) == {
        _EXPLICIT_TIME_AUTHORITY_PARAM: "15:00"
    }


def test_v2_single_explicit_time_does_not_leak_into_unrelated_compound_operation() -> None:
    first = TurnOperation(
        type="book",
        entities=TurnEntities(
            time=TimeConstraint(
                mode="exact",
                start_time="15:00",
                start_time_ambiguity="twelve_hour",
            )
        ),
    )
    second = TurnOperation(type="book", entities=TurnEntities())
    turn = TiaTurnUnderstanding(operations=[first, second])
    corrected = _preserve_explicit_clock_constraints_v2(
        turn, latest_customer_text="?????? ?????? 03:00 ???????? ??? ?????"
    )
    assert corrected.operations[0].entities.time is not None
    assert corrected.operations[0].entities.time.start_time == "03:00"
    assert corrected.operations[1].entities.time is None


@pytest.mark.parametrize("operation_type", ["availability", "reschedule"])
def test_v2_explicit_03_is_preserved_for_read_and_reschedule_semantics(operation_type: str) -> None:
    corrected = _preserve_explicit_clock_constraints_v2(
        _v2_turn(value="15:00", ambiguity="twelve_hour", operation_type=operation_type),
        latest_customer_text="????? ???? ?????? 03:00",
    )
    constraint = corrected.operations[0].entities.time
    assert constraint is not None
    assert constraint.start_time == "03:00"
    assert constraint.start_time_ambiguity == "none"
