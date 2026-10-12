from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.planner import PlannerContext, PlanStep, plan_turn
from app.services.agent_v2.state import BookingTaskState, CustomerConstraints, WriteAuthorization
from app.services.agent_v2.state_executor import apply_step_state
from app.services.agent_v2.state_rules import apply_booking_requested_services_change
from app.services.agent_v2.turn_normalization import (
    collapse_booking_service_sets,
    expand_multi_service_operations,
    tag_booking_service_sets,
)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
A = "svc-underarm"
B = "svc-bikini"
ARMS = "svc-full-arms"
COMBO = "svc-underarm-bikini-combo"


def _device(key: str) -> dict[str, object]:
    return {
        "device_key": key,
        "device_name": key,
        "price_minor": 50_000,
        "duration_minutes": 20,
        "configured": True,
    }


def _catalog(*, common_devices: int = 1) -> dict[str, object]:
    a_devices = [_device("v1"), _device("v2")]
    b_devices = [_device("v1")] if common_devices == 1 else [_device("v1"), _device("v2")]
    return {
        "services": [
            {
                "id": A,
                "name": "Underarm",
                "duration_minutes": 20,
                "requires_laser_device": True,
                "laser_devices": a_devices,
            },
            {
                "id": B,
                "name": "Bikini",
                "duration_minutes": 30,
                "requires_laser_device": True,
                "laser_devices": b_devices,
            },
            {
                "id": ARMS,
                "name": "Full Arms",
                "duration_minutes": 40,
                "requires_laser_device": True,
                "laser_devices": [_device("v1"), _device("v2")],
            },
            {
                "id": COMBO,
                "name": "Underarm + Bikini",
                "duration_minutes": 50,
                "requires_laser_device": True,
                "laser_devices": [_device("v1")],
            },
        ],
        "doctors": [
            {"id": "doc-common", "name": "Common Doctor", "service_ids": [A, B, ARMS, COMBO]},
            {"id": "doc-underarm", "name": "Underarm Doctor", "service_ids": [A]},
        ],
        "branches": [],
        "appointments": [],
    }


def _refs(context) -> dict[str, str]:
    return {
        str(target.canonical_id): ref
        for ref, target in context.reference_map.items()
        if target.kind == "service"
    }


def _grouped_availability(
    context,
    service_ids: list[str],
    *,
    doctor_ref: str | None = None,
    date: DateConstraint | None = None,
) -> TiaTurnUnderstanding:
    refs = _refs(context)
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                execution_intent="informational",
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=[refs[service_id] for service_id in service_ids],
                        candidate_mode="set",
                    ),
                    doctor=EntityReference(ref=doctor_ref) if doctor_ref else None,
                    date=date or DateConstraint(mode="next_available"),
                ),
            )
        ],
        safety_signals=[],
    )


def _plan_grouped(turn: TiaTurnUnderstanding, catalog: dict[str, object]):
    context = build_semantic_context(catalog)
    collapsed, service_sets = collapse_booking_service_sets(
        turn,
        semantic_context=context,
        catalog=catalog,
        active_task=None,
    )
    expanded, groups = expand_multi_service_operations(
        collapsed,
        semantic_context=context,
        booking_service_sets=service_sets,
    )
    plan = tag_booking_service_sets(
        plan_turn(
            expanded,
            PlannerContext(semantic_context=context, active_task=None, now=NOW),
        ),
        service_sets=service_sets,
    )
    return context, expanded, groups, plan


def _start_state(turn: TiaTurnUnderstanding, catalog: dict[str, object]) -> BookingTaskState:
    context, normalized, _groups, plan = _plan_grouped(turn, catalog)
    assert len(plan.steps) == 1
    step = plan.steps[0]
    transition = apply_step_state(
        None,
        step=step,
        operation=normalized.operations[0],
        reads=None,
        now=NOW,
        turn_id="g-contract",
        catalog=catalog,
    )
    assert isinstance(transition.active_task, BookingTaskState)
    return transition.active_task


@pytest.mark.parametrize("contract_phrase", ["G1: احجز إبط وبكيني مع بعض", "G2: عايزة إبط وبكيني في نفس الزيارة"])
def test_g1_g2_explicit_grouped_set_establishes_one_canonical_task(contract_phrase: str) -> None:
    del contract_phrase
    catalog = _catalog()
    context = build_semantic_context(catalog)
    turn = _grouped_availability(context, [A, B])
    state = _start_state(turn, catalog)

    assert state.requested_service_ids == [A, B]
    assert state.constraints.service_id == B  # longest service is backend-selected main service
    assert state.write_authorization.authorized is False


def test_g3_grouped_availability_reads_without_write() -> None:
    catalog = _catalog()
    context = build_semantic_context(catalog)
    turn = _grouped_availability(context, [A, B])
    _context, _normalized, groups, plan = _plan_grouped(turn, catalog)

    assert groups == {}
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.disposition == "read"
    assert step.state_action == "start_booking"
    assert [read.kind for read in step.reads] == ["availability"]
    assert step.write_intent is None
    assert step.facts["requested_service_ids"] == [A, B]


def test_g4_one_common_device_is_auto_selected_for_visit() -> None:
    catalog = _catalog(common_devices=1)
    context = build_semantic_context(catalog)
    state = _start_state(_grouped_availability(context, [A, B]), catalog)

    assert state.constraints.device_key == "v1"
    assert state.service_device_keys == {A: "v1", B: "v1"}


def test_g5_two_common_devices_remain_one_visit_level_choice() -> None:
    catalog = _catalog(common_devices=2)
    context = build_semantic_context(catalog)
    state = _start_state(_grouped_availability(context, [A, B]), catalog)

    assert state.constraints.device_key is None
    assert state.service_device_keys == {}
    assert state.requested_service_ids == [A, B]


def test_g6_doctor_request_preserves_both_services() -> None:
    catalog = _catalog()
    context = build_semantic_context(catalog)
    doctor_ref = next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == "doctor" and target.canonical_id == "doc-common"
    )
    state = _start_state(
        _grouped_availability(context, [A, B], doctor_ref=doctor_ref),
        catalog,
    )

    assert state.requested_service_ids == [A, B]
    assert state.constraints.doctor_id == "doc-common"


def test_g7_explicit_date_preserves_service_set_and_date() -> None:
    catalog = _catalog()
    context = build_semantic_context(catalog)
    state = _start_state(
        _grouped_availability(
            context,
            [A, B],
            date=DateConstraint(mode="exact", start_date="2026-10-17"),
        ),
        catalog,
    )

    assert state.requested_service_ids == [A, B]
    assert state.constraints.date is not None
    assert state.constraints.date.start_date == "2026-10-17"


def test_g8_side_read_cannot_erase_active_service_set() -> None:
    catalog = _catalog()
    context = build_semantic_context(catalog)
    state = _start_state(_grouped_availability(context, [A, B]), catalog)
    pricing = TurnOperation(type="pricing", execution_intent="informational", entities=TurnEntities())
    side_step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        state_action="none",
        response_goal="answer_price",
    )

    transition = apply_step_state(
        state,
        step=side_step,
        operation=pricing,
        reads=None,
        now=NOW,
        turn_id="g8",
        catalog=catalog,
    )
    assert isinstance(transition.active_task, BookingTaskState)
    assert transition.active_task.requested_service_ids == [A, B]


def test_g9_remove_one_service_updates_canonical_set() -> None:
    state = BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id=B),
        requested_service_ids=[A, B],
    )
    updated = apply_booking_requested_services_change(state, service_ids=[A])
    assert updated.requested_service_ids == [A]


def test_g10_replace_one_service_keeps_retained_and_replacement_only() -> None:
    catalog = _catalog()
    context = build_semantic_context(catalog)
    refs = _refs(context)
    state = BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id=B),
        requested_service_ids=[A, B],
    )
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                execution_intent="execute",
                active_task_relationship="continue",
                grouped_booking_action="remove_other_components",
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=[refs[A], refs[ARMS]],
                        candidate_mode="set",
                    )
                ),
            )
        ]
    )
    _normalized, service_sets = collapse_booking_service_sets(
        turn,
        semantic_context=context,
        catalog=catalog,
        active_task=state,
    )
    assert service_sets == {0: [A, ARMS]}


def test_g11_combo_replacement_does_not_retain_constituents() -> None:
    catalog = _catalog()
    context = build_semantic_context(catalog)
    refs = _refs(context)
    state = BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id=B),
        requested_service_ids=[A, B],
    )
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                execution_intent="execute",
                active_task_relationship="continue",
                grouped_booking_action="remove_other_components",
                entities=TurnEntities(service=EntityReference(ref=refs[COMBO], candidate_mode="set")),
            )
        ]
    )
    _normalized, service_sets = collapse_booking_service_sets(
        turn,
        semantic_context=context,
        catalog=catalog,
        active_task=state,
    )
    assert service_sets == {0: [COMBO]}


def test_g12_ambiguous_services_still_require_clarification() -> None:
    catalog = _catalog()
    context = build_semantic_context(catalog)
    refs = _refs(context)
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                execution_intent="informational",
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=[refs[A], refs[B]],
                        candidate_mode="ambiguous",
                    )
                ),
            )
        ]
    )
    collapsed, service_sets = collapse_booking_service_sets(
        turn,
        semantic_context=context,
        catalog=catalog,
        active_task=None,
    )
    assert service_sets == {}
    plan = plan_turn(
        collapsed,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    assert plan.steps[0].disposition == "clarify"
    assert plan.steps[0].clarification_field == "service"
    assert plan.steps[0].state_action == "none"


def test_stateful_harness_uses_same_grouped_normalization_as_runtime(monkeypatch) -> None:
    from langchain_core.messages import HumanMessage

    import app.services.agent_v2.stateful_test_harness as stateful_harness
    from app.services.agent_v2.test_harness import V2FixtureEnvironment

    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                execution_intent="informational",
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=["S1", "S2"],
                        candidate_mode="set",
                    ),
                    date=DateConstraint(mode="next_available"),
                ),
            )
        ]
    )
    monkeypatch.setattr(
        stateful_harness,
        "interpret_customer_turn_v2",
        lambda **_kwargs: turn,
    )
    monkeypatch.setattr(
        stateful_harness,
        "compose_v2_customer_reply",
        lambda **_kwargs: ("grouped availability", "deterministic:test"),
    )

    result = stateful_harness.run_v2_stateful_fixture_turn(
        history=[HumanMessage(content="explicit grouped request")],
        local_now=NOW,
        env=V2FixtureEnvironment(catalog=_catalog()),
        simulate_writes=False,
        turn_id="g-harness",
    )

    assert isinstance(result.active_task, BookingTaskState)
    assert result.active_task.requested_service_ids == [A, B]
    assert result.active_task.write_authorization.authorized is False
    assert len(result.traces) == 1
    assert result.traces[0].read_kinds == ("availability",)
    assert result.traces[0].simulated_write is None
