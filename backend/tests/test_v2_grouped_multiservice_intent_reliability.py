from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.time_resolution import resolve_turn_times_by_clinic_hours
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import _normalize_time_semantics_v2
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


def test_g9_singleton_retained_ref_updates_grouped_canonical_set() -> None:
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
                entities=TurnEntities(service=EntityReference(ref=refs[A])),
            )
        ]
    )

    normalized, service_sets = collapse_booking_service_sets(
        turn,
        semantic_context=context,
        catalog=catalog,
        active_task=state,
    )
    plan = tag_booking_service_sets(
        plan_turn(
            normalized,
            PlannerContext(semantic_context=context, active_task=state, now=NOW),
        ),
        service_sets=service_sets,
    )
    assert service_sets == {0: [A]}
    assert len(plan.steps) == 1

    transition = apply_step_state(
        state,
        step=plan.steps[0],
        operation=normalized.operations[0],
        reads=None,
        now=NOW,
        turn_id="g9-singleton-ref",
        catalog=catalog,
    )
    assert isinstance(transition.active_task, BookingTaskState)
    assert transition.active_task.requested_service_ids == [A]
    assert transition.active_task.constraints.service_id == A


def test_g9_informational_continue_active_narrows_grouped_set() -> None:
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
                type="continue_active",
                execution_intent="informational",
                active_task_relationship="unspecified",
                grouped_booking_action="remove_other_components",
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=[refs[A]],
                        candidate_mode="set",
                    )
                ),
            )
        ]
    )

    normalized, service_sets = collapse_booking_service_sets(
        turn,
        semantic_context=context,
        catalog=catalog,
        active_task=state,
    )
    plan = tag_booking_service_sets(
        plan_turn(
            normalized,
            PlannerContext(semantic_context=context, active_task=state, now=NOW),
        ),
        service_sets=service_sets,
    )
    assert service_sets == {0: [A]}

    transition = apply_step_state(
        state,
        step=plan.steps[0],
        operation=normalized.operations[0],
        reads=None,
        now=NOW,
        turn_id="g9-informational-continue",
        catalog=catalog,
    )
    assert isinstance(transition.active_task, BookingTaskState)
    assert transition.active_task.requested_service_ids == [A]
    assert transition.active_task.constraints.service_id == A


def test_g9_informational_continue_active_preserves_pure_addition() -> None:
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
                type="continue_active",
                execution_intent="informational",
                active_task_relationship="continue",
                grouped_booking_action="preserve_group",
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=[refs[A], refs[B], refs[ARMS]],
                        candidate_mode="set",
                    )
                ),
            )
        ]
    )

    normalized, service_sets = collapse_booking_service_sets(
        turn,
        semantic_context=context,
        catalog=catalog,
        active_task=state,
    )
    plan = tag_booking_service_sets(
        plan_turn(
            normalized,
            PlannerContext(semantic_context=context, active_task=state, now=NOW),
        ),
        service_sets=service_sets,
    )
    assert service_sets == {0: [A, B, ARMS]}

    transition = apply_step_state(
        state,
        step=plan.steps[0],
        operation=normalized.operations[0],
        reads=None,
        now=NOW,
        turn_id="g9-informational-add",
        catalog=catalog,
    )
    assert isinstance(transition.active_task, BookingTaskState)
    assert transition.active_task.requested_service_ids == [A, B, ARMS]
    assert transition.active_task.constraints.service_id == ARMS


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


def test_stateful_harness_applies_same_explicit_group_narrowing_as_runtime(monkeypatch) -> None:
    from langchain_core.messages import HumanMessage

    import app.services.agent_v2.stateful_test_harness as stateful_harness
    from app.services.agent_v2.test_harness import V2FixtureEnvironment

    state = BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id=B),
        requested_service_ids=[A, B],
    )
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                execution_intent="informational",
                active_task_relationship="continue",
                grouped_booking_action="remove_other_components",
                entities=TurnEntities(service=EntityReference(ref="S1")),
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
        lambda **_kwargs: ("underarm only", "deterministic:test"),
    )

    result = stateful_harness.run_v2_stateful_fixture_turn(
        history=[HumanMessage(content="keep underarm only")],
        local_now=NOW,
        active_task=state,
        env=V2FixtureEnvironment(catalog=_catalog()),
        simulate_writes=False,
        turn_id="g-harness-narrow",
    )

    assert isinstance(result.active_task, BookingTaskState)
    assert result.active_task.requested_service_ids == [A]
    assert result.active_task.constraints.service_id == A
    assert result.active_task.write_authorization.authorized is False
    assert all(trace.simulated_write is None for trace in result.traces)

def _ra6_book_turn(context, *, execution_intent: str) -> TiaTurnUnderstanding:
    refs = _refs(context)
    doctor_ref = next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == "doctor" and target.canonical_id == "doc-common"
    )
    device_ref = next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == "device" and target.canonical_id == "v1"
    )
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                execution_intent=execution_intent,
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=[refs[A], refs[B]],
                        candidate_mode="set",
                    ),
                    doctor=EntityReference(ref=doctor_ref),
                    device=EntityReference(ref=device_ref),
                    date=DateConstraint(mode="exact", start_date="2026-10-15"),
                    time=TimeConstraint(
                        mode="exact",
                        start_time="16:30",
                    ),
                ),
            )
        ]
    )


@pytest.mark.parametrize(
    ("execution_intent", "expected_authorized"),
    [("execute", True), ("informational", False)],
)
def test_ra6_book_set_meaning_is_independent_from_write_authorization(
    execution_intent: str,
    expected_authorized: bool,
) -> None:
    catalog = _catalog(common_devices=1)
    context = build_semantic_context(catalog)
    turn = _ra6_book_turn(context, execution_intent=execution_intent)

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

    assert service_sets == {0: [A, B]}
    assert groups == {}
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.facts["requested_service_ids"] == [A, B]
    assert step.facts["multi_service_single_appointment"] is True
    assert step.facts["booking_proposal_required"] is True
    assert step.state_action == "start_booking"
    availability_reads = [read for read in step.reads if read.kind == "availability"]
    assert len(availability_reads) == 1
    assert availability_reads[0].parameters["service_id"] == B
    assert availability_reads[0].parameters["device_key"] == "v1"
    assert availability_reads[0].parameters["time"]["start_time"] == "16:30"
    if execution_intent == "informational":
        assert step.write_intent is None

    transition = apply_step_state(
        None,
        step=step,
        operation=expanded.operations[0],
        reads=None,
        now=NOW,
        turn_id=f"ra6-{execution_intent}",
        catalog=catalog,
    )
    state = transition.active_task
    assert isinstance(state, BookingTaskState)
    assert state.requested_service_ids == [A, B]
    assert state.constraints.service_id == B
    assert state.constraints.doctor_id == "doc-common"
    assert state.constraints.device_key == "v1"
    assert state.constraints.time is not None
    assert state.constraints.time.start_time == "16:30"
    assert state.write_authorization.authorized is expected_authorized


def test_ra6_informational_book_preserves_task_but_has_zero_write_authority() -> None:
    catalog = _catalog(common_devices=1)
    context = build_semantic_context(catalog)
    turn = _ra6_book_turn(context, execution_intent="informational")
    state = _start_state(turn, catalog)

    assert state.requested_service_ids == [A, B]
    assert state.constraints.service_id == B
    assert state.constraints.device_key == "v1"
    assert state.write_authorization.authorized is False
    assert state.proposal is None


def test_true_service_info_comparison_does_not_become_booking_task() -> None:
    catalog = _catalog()
    context = build_semantic_context(catalog)
    refs = _refs(context)
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="service_info",
                execution_intent="informational",
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=[refs[A], refs[B]],
                        candidate_mode="set",
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
    assert plan.steps[0].state_action != "start_booking"
    assert plan.steps[0].write_intent is None



def test_g13_singleton_ambiguous_preserve_group_unions_new_service() -> None:
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
                type="continue_active",
                execution_intent="execute",
                active_task_relationship="continue",
                grouped_booking_action="preserve_group",
                entities=TurnEntities(
                    service=EntityReference(
                        ref=refs[ARMS],
                        candidate_mode="ambiguous",
                    )
                ),
            )
        ]
    )
    normalized, service_sets = collapse_booking_service_sets(
        turn,
        semantic_context=context,
        catalog=catalog,
        active_task=state,
    )
    assert service_sets == {0: [A, B, ARMS]}
    service = normalized.operations[0].entities.service
    assert service is not None
    assert service.ref == refs[ARMS]


def test_g14_existing_singleton_preserve_group_does_not_duplicate_service() -> None:
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
                type="continue_active",
                execution_intent="execute",
                active_task_relationship="continue",
                grouped_booking_action="preserve_group",
                entities=TurnEntities(
                    service=EntityReference(ref=refs[B], candidate_mode="ambiguous")
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
    assert service_sets == {0: [A, B]}



def test_integrated_grouped_spoken_clock_addition_preserves_canonical_time() -> None:
    catalog = _catalog(common_devices=1)
    catalog["branches"] = [
        {
            "id": "branch-1",
            "name": "Main",
            "working_hours": [
                {"weekday": weekday, "start": "10:00", "end": "22:00"}
                for weekday in range(7)
            ],
        }
    ]
    context = build_semantic_context(catalog)
    refs = _refs(context)
    branch_ref = next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == "branch" and target.canonical_id == "branch-1"
    )
    initial = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                execution_intent="execute",
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=[refs[A], refs[B]],
                        candidate_mode="set",
                    ),
                    branch=EntityReference(ref=branch_ref),
                    date=DateConstraint(mode="exact", start_date="2026-10-15"),
                    time=TimeConstraint(
                        mode="exact",
                        start_time="03:00",
                        spoken_period_hint="am",
                    ),
                ),
            )
        ]
    )
    time_safe = _normalize_time_semantics_v2(initial, latest_customer_text="spoken three")
    resolved = resolve_turn_times_by_clinic_hours(time_safe, context)
    initial_time = resolved.operations[0].entities.time
    assert initial_time is not None
    assert initial_time.start_time == "15:00"

    collapsed, service_sets = collapse_booking_service_sets(
        resolved,
        semantic_context=context,
        catalog=catalog,
        active_task=None,
    )
    plan = tag_booking_service_sets(
        plan_turn(
            collapsed,
            PlannerContext(semantic_context=context, active_task=None, now=NOW),
        ),
        service_sets=service_sets,
    )
    started = apply_step_state(
        None,
        step=plan.steps[0],
        operation=collapsed.operations[0],
        reads=None,
        now=NOW,
        turn_id="integrated-start",
        catalog=catalog,
    ).active_task
    assert isinstance(started, BookingTaskState)
    assert started.requested_service_ids == [A, B]
    assert started.constraints.time is not None
    assert started.constraints.time.start_time == "15:00"

    addition = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="continue_active",
                execution_intent="execute",
                active_task_relationship="continue",
                grouped_booking_action="preserve_group",
                entities=TurnEntities(
                    service=EntityReference(ref=refs[ARMS], candidate_mode="ambiguous")
                ),
            )
        ]
    )
    normalized, service_sets = collapse_booking_service_sets(
        addition,
        semantic_context=context,
        catalog=catalog,
        active_task=started,
    )
    assert service_sets == {0: [A, B, ARMS]}
    addition_plan = tag_booking_service_sets(
        plan_turn(
            normalized,
            PlannerContext(semantic_context=context, active_task=started, now=NOW),
        ),
        service_sets=service_sets,
    )
    updated = apply_step_state(
        started,
        step=addition_plan.steps[0],
        operation=normalized.operations[0],
        reads=None,
        now=NOW,
        turn_id="integrated-add",
        catalog=catalog,
    ).active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.requested_service_ids == [A, B, ARMS]
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "15:00"
    assert updated.proposal is None
