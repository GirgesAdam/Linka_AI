from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from app.agents.v2.responder import compose_v2_customer_reply
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_read_context
from app.agents.v2.turn_contract import (
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import merge_verified_read_context
from app.services.agent_v2.live_chat import _verified_read_context_from_turn
from app.services.agent_v2.orchestrator import V2RuntimeStepTrace
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.planner import PlannerContext, plan_turn

NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
APPOINTMENT_ID = "appointment-a"
APPOINTMENT_B_ID = "appointment-b"
SERVICE_ID = "service-hydrafacial"
DOCTOR_ID = "doctor-mariam"


def _catalog(*, start_local: str = "2026-10-02T14:00:00+03:00") -> dict[str, object]:
    return {
        "services": [
            {
                "id": SERVICE_ID,
                "name": "Hydrafacial",
                "category": "facial",
                "requires_laser_device": False,
            }
        ],
        "doctors": [
            {
                "id": DOCTOR_ID,
                "name": "دكتورة مريم",
                "service_ids": [SERVICE_ID],
            }
        ],
        "appointments": [
            {
                "appointment_id": APPOINTMENT_ID,
                "service_id": SERVICE_ID,
                "service_name": "Hydrafacial",
                "doctor_id": DOCTOR_ID,
                "doctor_name": "دكتورة مريم",
                "status": "confirmed",
                "start_local": start_local,
            },
            {
                "appointment_id": APPOINTMENT_B_ID,
                "service_id": SERVICE_ID,
                "service_name": "Hydrafacial",
                "doctor_id": DOCTOR_ID,
                "doctor_name": "دكتورة مريم",
                "status": "confirmed",
                "start_local": "2026-10-03T11:00:00+03:00",
            },
        ],
    }


def _semantic_context(*, start_local: str = "2026-10-02T14:00:00+03:00"):
    return build_semantic_context(_catalog(start_local=start_local))


def _time(value: str) -> TimeConstraint:
    return TimeConstraint(mode="exact", start_time=value)


def _challenge_operation(claimed_time: str = "18:00") -> TurnOperation:
    return TurnOperation(
        type="appointment_list",
        entities=TurnEntities(time=_time(claimed_time)),
        execution_intent="informational",
        appointment_fact_challenge="time",
        continues_previous=True,
    )


def _appointment_outcome(
    *,
    start_local: str,
    claimed_time: str,
) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_customer_history",
        facts={
            "appointments": {
                "visits": [
                    {
                        "status": "confirmed",
                        "start_local": start_local,
                        "end_local": "2026-10-02T15:00:00+03:00",
                        "doctor_name": "دكتورة مريم",
                        "services": [{"service_name": "Hydrafacial"}],
                    }
                ],
                "visit_count": 1,
                "presentation_unit": "visit",
                "complete_set": True,
            },
            "appointment_fact_challenge": {
                "field": "time",
                "claimed_time": claimed_time,
            },
        },
    )


def _turn_with_read(
    *,
    operation_type: str,
    read_kind: str,
    parameters: dict[str, object],
    verified_parameters: dict[str, object] | None = None,
    execution_intent: str = "informational",
    state_action: str = "none",
    active_task: object | None = None,
) -> SimpleNamespace:
    step = SimpleNamespace(
        operation_index=0,
        operation_type=operation_type,
        disposition="read",
        write_intent=None,
        state_action=state_action,
        reads=(SimpleNamespace(kind=read_kind, parameters=parameters),),
        facts=dict(parameters),
    )
    trace = SimpleNamespace(
        operation_index=0,
        operation_type=operation_type,
        outcome=None,
        verified_parameters=verified_parameters or {},
    )
    return SimpleNamespace(
        plan=SimpleNamespace(steps=(step,)),
        understanding=SimpleNamespace(
            operations=(SimpleNamespace(execution_intent=execution_intent),)
        ),
        traces=(trace,),
        outcomes=(),
        active_task=active_task,
        pending_write=None,
    )


def _persisted_context_after(
    turn: SimpleNamespace,
    *,
    previous: dict[str, object] | None,
) -> dict[str, object] | None:
    return _verified_read_context_from_turn(
        SimpleNamespace(),
        workspace=SimpleNamespace(),
        turn=turn,
        previous_read_context=previous,
    )


def _ref_for_appointment(context, appointment_id: str) -> str:
    return next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == "appointment" and target.canonical_id == appointment_id
    )


def _assert_challenge_rereads(
    read_context: dict[str, object],
    *,
    expected_appointment_id: str,
) -> None:
    context = with_safe_read_context(_semantic_context(), read_context=read_context)
    recent = context.model_input["recent_verified_read"]
    appointment_ref = recent.get("appointment_ref")
    assert isinstance(appointment_ref, str)
    assert context.resolve(appointment_ref, expected_kind="appointment") == expected_appointment_id

    turn = TiaTurnUnderstanding(operations=[_challenge_operation("11:00")])
    merged = merge_verified_read_context(turn, context)
    operation = merged.operations[0]
    assert operation.appointment_fact_challenge == "time"
    assert operation.continues_previous is True
    assert operation.entities.time == _time("11:00")
    assert operation.entities.appointment == EntityReference(ref=appointment_ref)

    plan = plan_turn(
        merged,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    step = plan.steps[0]
    assert step.reads[0].kind == "appointments"
    assert step.reads[0].parameters == {"appointment_id": expected_appointment_id}
    assert step.write_intent is None


def test_unique_appointment_read_persists_verified_result_identity() -> None:
    context = _semantic_context()
    operation = TurnOperation(
        type="appointment_list",
        entities=TurnEntities(),
        execution_intent="informational",
    )
    understanding = TiaTurnUnderstanding(operations=[operation])
    plan = plan_turn(
        understanding,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    turn = SimpleNamespace(
        plan=plan,
        understanding=understanding,
        traces=(
            V2RuntimeStepTrace(
                operation_index=0,
                operation_type="appointment_list",
                disposition_before="read",
                disposition_after="read",
                read_kinds=("appointments",),
                verified_parameters={
                    "appointment_id": APPOINTMENT_ID,
                    "service_id": SERVICE_ID,
                    "doctor_id": DOCTOR_ID,
                },
            ),
        ),
    )

    persisted = _verified_read_context_from_turn(
        SimpleNamespace(),
        workspace=SimpleNamespace(),
        turn=turn,
    )

    assert persisted == {
        "operation_type": "appointment_list",
        "appointment_id": APPOINTMENT_ID,
    }


def test_verified_appointment_identity_is_exposed_only_as_opaque_ref() -> None:
    context = with_safe_read_context(
        _semantic_context(),
        read_context={
            "operation_type": "appointment_list",
            "appointment_id": APPOINTMENT_ID,
        },
    )

    recent = context.model_input["recent_verified_read"]
    assert recent == {
        "operation_type": "appointment_list",
        "appointment_ref": "A1",
    }
    assert "appointment_id" not in recent
    assert context.model_input["appointments"] == [
        {
            "ref": "A1",
            "service_ref": "S1",
            "doctor_ref": "D1",
            "status": "confirmed",
            "start_local": "2026-10-02T14:00:00+03:00",
        }
    ]


def test_verified_appointment_identity_gets_ephemeral_ref_without_catalog_appointments() -> None:
    context = build_semantic_context(
        {
            "services": [
                {
                    "id": SERVICE_ID,
                    "name": "Hydrafacial",
                    "category": "facial",
                    "requires_laser_device": False,
                }
            ],
            "doctors": [
                {
                    "id": DOCTOR_ID,
                    "name": "دكتورة مريم",
                    "service_ids": [SERVICE_ID],
                }
            ],
            "branches": [{"id": "branch-1", "name": "Linka Clinic"}],
        }
    )

    safe = with_safe_read_context(
        context,
        read_context={
            "operation_type": "appointment_list",
            "appointment_id": APPOINTMENT_ID,
        },
    )

    recent = safe.model_input["recent_verified_read"]
    assert recent == {
        "operation_type": "appointment_list",
        "appointment_ref": "A1",
    }
    assert safe.resolve("A1", expected_kind="appointment") == APPOINTMENT_ID
    assert "appointment_id" not in recent
    assert safe.server_metadata.get("semantic_focus_blocked") is False
    assert safe.model_input.get("focused_context") == {
        "appointments": [{"ref": "A1"}]
    }


def test_time_challenge_binds_verified_target_and_rereads_by_id_only() -> None:
    context = with_safe_read_context(
        _semantic_context(),
        read_context={
            "operation_type": "appointment_list",
            "appointment_id": APPOINTMENT_ID,
        },
    )
    turn = TiaTurnUnderstanding(operations=[_challenge_operation()])

    merged = merge_verified_read_context(turn, context)
    operation = merged.operations[0]
    assert operation.entities.appointment == EntityReference(ref="A1")

    plan = plan_turn(
        merged,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    step = plan.steps[0]

    assert step.disposition == "read"
    assert step.reads[0].kind == "appointments"
    assert step.reads[0].parameters == {"appointment_id": APPOINTMENT_ID}
    assert step.write_intent is None
    assert step.state_action == "none"
    assert step.facts == {
        "appointment_fact_challenge": {
            "field": "time",
            "claimed_time": "18:00",
        }
    }


def test_f8_a_price_side_read_preserves_unique_appointment_for_time_challenge() -> None:
    previous = {
        "operation_type": "appointment_list",
        "appointment_id": APPOINTMENT_ID,
    }
    side_context = _persisted_context_after(
        _turn_with_read(
            operation_type="pricing",
            read_kind="service_catalog",
            parameters={"service_id": SERVICE_ID},
        ),
        previous=previous,
    )

    assert side_context == {
        "operation_type": "pricing",
        "service_id": SERVICE_ID,
        "appointment_id": APPOINTMENT_ID,
    }
    _assert_challenge_rereads(
        side_context,
        expected_appointment_id=APPOINTMENT_ID,
    )


def test_f8_b_service_info_side_read_preserves_unique_appointment_for_challenge() -> None:
    previous = {
        "operation_type": "appointment_list",
        "appointment_id": APPOINTMENT_ID,
    }
    side_context = _persisted_context_after(
        _turn_with_read(
            operation_type="service_info",
            read_kind="service_catalog",
            parameters={"service_id": SERVICE_ID},
        ),
        previous=previous,
    )

    assert side_context is not None
    assert side_context["operation_type"] == "service_info"
    assert side_context["appointment_id"] == APPOINTMENT_ID
    _assert_challenge_rereads(
        side_context,
        expected_appointment_id=APPOINTMENT_ID,
    )


def test_f8_c_new_unique_appointment_read_replaces_previous_anchor() -> None:
    previous = {
        "operation_type": "appointment_list",
        "appointment_id": APPOINTMENT_ID,
    }
    current = _persisted_context_after(
        _turn_with_read(
            operation_type="appointment_list",
            read_kind="appointments",
            parameters={},
            verified_parameters={"appointment_id": APPOINTMENT_B_ID},
        ),
        previous=previous,
    )

    assert current == {
        "operation_type": "appointment_list",
        "appointment_id": APPOINTMENT_B_ID,
    }
    _assert_challenge_rereads(
        current,
        expected_appointment_id=APPOINTMENT_B_ID,
    )


def test_f8_d_multi_appointment_read_clears_unique_anchor_authority() -> None:
    previous = {
        "operation_type": "appointment_list",
        "appointment_id": APPOINTMENT_ID,
    }
    current = _persisted_context_after(
        _turn_with_read(
            operation_type="appointment_list",
            read_kind="appointments",
            parameters={},
            verified_parameters={
                "appointment_ids": [APPOINTMENT_ID, APPOINTMENT_B_ID],
            },
        ),
        previous=previous,
    )

    assert current == {"operation_type": "appointment_list"}
    context = with_safe_read_context(_semantic_context(), read_context=current)
    assert "appointment_ref" not in context.model_input["recent_verified_read"]

    merged = merge_verified_read_context(
        TiaTurnUnderstanding(operations=[_challenge_operation("11:00")]),
        context,
    )
    assert merged.operations[0].entities.appointment is None
    step = plan_turn(
        merged,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    ).steps[0]
    assert step.disposition == "clarify"
    assert step.clarification_field == "appointment"
    assert step.write_intent is None


def test_f8_e_fresh_unrelated_task_does_not_retain_old_appointment_anchor() -> None:
    previous = {
        "operation_type": "appointment_list",
        "appointment_id": APPOINTMENT_ID,
    }
    current = _persisted_context_after(
        _turn_with_read(
            operation_type="book",
            read_kind="availability",
            parameters={"service_id": SERVICE_ID},
            execution_intent="execute",
            state_action="replace_active",
            active_task=SimpleNamespace(task_type="booking"),
        ),
        previous=previous,
    )

    assert current is not None
    assert current["operation_type"] == "book"
    assert "appointment_id" not in current
    context = with_safe_read_context(_semantic_context(), read_context=current)
    assert "appointment_ref" not in context.model_input["recent_verified_read"]

    merged = merge_verified_read_context(
        TiaTurnUnderstanding(operations=[_challenge_operation("11:00")]),
        context,
    )
    assert merged.operations[0].entities.appointment is None
    step = plan_turn(
        merged,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    ).steps[0]
    assert step.disposition == "clarify"
    assert step.write_intent is None


def test_f8_f_explicit_different_appointment_reference_wins_over_carried_anchor() -> None:
    side_context = _persisted_context_after(
        _turn_with_read(
            operation_type="pricing",
            read_kind="service_catalog",
            parameters={"service_id": SERVICE_ID},
        ),
        previous={
            "operation_type": "appointment_list",
            "appointment_id": APPOINTMENT_ID,
        },
    )
    assert side_context is not None
    context = with_safe_read_context(_semantic_context(), read_context=side_context)
    b_ref = _ref_for_appointment(context, APPOINTMENT_B_ID)

    operation = TurnOperation(
        type="appointment_list",
        entities=TurnEntities(
            appointment=EntityReference(ref=b_ref),
            time=_time("11:00"),
        ),
        execution_intent="informational",
        appointment_fact_challenge="none",
        continues_previous=False,
    )
    plan = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    step = plan.steps[0]
    assert step.reads[0].parameters["appointment_id"] == APPOINTMENT_B_ID
    assert step.reads[0].parameters["appointment_id"] != APPOINTMENT_ID
    assert step.write_intent is None


def test_f8_g_immediate_challenge_control_still_rereads_same_appointment() -> None:
    _assert_challenge_rereads(
        {
            "operation_type": "appointment_list",
            "appointment_id": APPOINTMENT_ID,
        },
        expected_appointment_id=APPOINTMENT_ID,
    )


def test_verified_appointment_anchor_is_bounded_to_one_harmless_side_read() -> None:
    first_side = _persisted_context_after(
        _turn_with_read(
            operation_type="pricing",
            read_kind="service_catalog",
            parameters={"service_id": SERVICE_ID},
        ),
        previous={
            "operation_type": "appointment_list",
            "appointment_id": APPOINTMENT_ID,
        },
    )
    assert first_side is not None
    assert first_side["appointment_id"] == APPOINTMENT_ID

    second_side = _persisted_context_after(
        _turn_with_read(
            operation_type="service_info",
            read_kind="service_catalog",
            parameters={"service_id": SERVICE_ID},
        ),
        previous=first_side,
    )
    assert second_side is not None
    assert "appointment_id" not in second_side


def test_time_challenge_without_verified_target_fails_non_destructively() -> None:
    context = _semantic_context()
    turn = TiaTurnUnderstanding(operations=[_challenge_operation()])

    plan = plan_turn(
        turn,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    step = plan.steps[0]

    assert step.disposition == "clarify"
    assert step.clarification_field == "appointment"
    assert step.write_intent is None


def test_new_filtered_query_remains_exact_time_filter() -> None:
    context = with_safe_read_context(
        _semantic_context(),
        read_context={
            "operation_type": "appointment_list",
            "appointment_id": APPOINTMENT_ID,
        },
    )
    operation = TurnOperation(
        type="appointment_list",
        entities=TurnEntities(time=_time("18:00")),
        execution_intent="informational",
        appointment_fact_challenge="none",
        continues_previous=False,
    )
    plan = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    step = plan.steps[0]

    assert step.reads[0].parameters == {
        "time": {
            "mode": "exact",
            "start_time": "18:00",
            "end_time": None,
            "start_time_ambiguity": "none",
            "end_time_ambiguity": "none",
        }
    }
    assert step.write_intent is None


def test_fresh_exact_time_query_remains_normal_filter() -> None:
    context = _semantic_context()
    operation = TurnOperation(
        type="appointment_list",
        entities=TurnEntities(time=_time("18:00")),
        execution_intent="informational",
    )
    plan = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )

    assert plan.steps[0].reads[0].parameters["time"]["start_time"] == "18:00"
    assert plan.steps[0].write_intent is None


def test_fact_challenge_marker_cannot_authorize_lifecycle_or_execute_intent() -> None:
    with pytest.raises(ValidationError):
        TurnOperation(
            type="reschedule",
            entities=TurnEntities(time=_time("18:00")),
            execution_intent="informational",
            appointment_fact_challenge="time",
            continues_previous=True,
        )

    with pytest.raises(ValidationError):
        TurnOperation(
            type="appointment_list",
            entities=TurnEntities(time=_time("18:00")),
            execution_intent="execute",
            appointment_fact_challenge="time",
            continues_previous=True,
        )


def test_false_time_challenge_deterministically_corrects_verified_truth() -> None:
    reply, model = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="مش كان الساعة 18:00؟")],
        outcomes=[
            _appointment_outcome(
                start_local="2026-10-02T14:00:00+03:00",
                claimed_time="18:00",
            )
        ],
    )

    assert model == "deterministic:appointment-info-contract"
    assert "مش الساعة 6:00 مساءً" in reply
    assert "الساعة 2:00 مساءً" in reply
    assert "مفيش مواعيد" not in reply


def test_correct_time_challenge_deterministically_confirms_verified_truth() -> None:
    reply, model = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="مش كان الساعة 18:00؟")],
        outcomes=[
            _appointment_outcome(
                start_local="2026-10-02T18:00:00+03:00",
                claimed_time="18:00",
            )
        ],
    )

    assert model == "deterministic:appointment-info-contract"
    assert reply.startswith("أيوه، ده الوقت المؤكد للموعد:")
    assert "الساعة 6:00 مساءً" in reply


def test_missing_reread_target_never_claims_global_no_appointments() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_customer_history",
        facts={
            "appointments": {
                "visits": [],
                "visit_count": 0,
                "presentation_unit": "visit",
                "complete_set": True,
            },
            "appointment_fact_challenge": {
                "field": "time",
                "claimed_time": "18:00",
            },
        },
    )

    reply, model = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="مش كان الساعة 18:00؟")],
        outcomes=[outcome],
    )

    assert model == "deterministic:appointment-info-contract"
    assert "الموعد اللي راجعناه" in reply
    assert "مفيش مواعيد جاية" not in reply
