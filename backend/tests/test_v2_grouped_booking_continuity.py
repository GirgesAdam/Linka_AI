from __future__ import annotations

from datetime import UTC, datetime

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import active_task_semantic_view
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.compound_turn_policy import (
    compound_write_group,
    normalize_compound_turn_plan,
)
from app.services.agent_v2.grouped_booking_continuity import (
    enforce_grouped_booking_write_guard,
    explicitly_narrow_grouped_booking_task,
    grouped_booking_task_from_plan,
    restore_grouped_booking_understanding,
)
from app.services.agent_v2.planner import (
    PlannerContext,
    PlanStep,
    TurnPlan,
    WriteIntent,
    plan_turn,
)
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    GroupedBookingState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state

NOW = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
DATE = DateConstraint(mode="exact", start_date="2026-09-28")
TIME_A = TimeConstraint(mode="exact", start_time="10:00")
TIME_B = TimeConstraint(mode="exact", start_time="11:15")


def _context():
    return build_semantic_context(
        {
            "services": [
                {"id": "svc-a", "name": "Hydrafacial", "requires_laser_device": False},
                {
                    "id": "svc-b",
                    "name": "Underarm Laser",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_key": "prime_lase", "device_name": "Prime Lase"}
                    ],
                },
                {"id": "svc-c", "name": "Deep Cleansing", "requires_laser_device": False},
            ],
            "doctors": [
                {
                    "id": "doc-1",
                    "name": "Dr Maryam",
                    "service_ids": ["svc-a", "svc-b", "svc-c"],
                }
            ],
            "appointments": [],
        }
    )


def _ref(kind: str, canonical_id: str) -> str:
    context = _context()
    return next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == kind and target.canonical_id == canonical_id
    )


def _operation(
    service_id: str,
    *,
    time: TimeConstraint | None = None,
    grouped_booking_action: str = "preserve_group",
) -> TurnOperation:
    device = (
        EntityReference(ref=_ref("device", "prime_lase"))
        if service_id == "svc-b"
        else None
    )
    return TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(ref=_ref("service", service_id)),
            doctor=EntityReference(ref=_ref("doctor", "doc-1")),
            device=device,
            date=DATE,
            time=time,
        ),
        execution_intent="execute",
        grouped_booking_action=grouped_booking_action,
    )


def _facts(service_id: str, time: TimeConstraint) -> dict[str, object]:
    facts: dict[str, object] = {
        "service_id": service_id,
        "doctor_id": "doc-1",
        "date": DATE.model_dump(mode="json"),
        "time": time.model_dump(mode="json"),
        "package_usage": "unspecified",
        "compound_visit_group": "compound:0,1",
        "compound_write_group": "compound:0,1",
        "compound_visit_grouped": True,
    }
    if service_id == "svc-b":
        facts["device_key"] = "prime_lase"
    return facts


def _step(
    index: int,
    service_id: str,
    time: TimeConstraint,
    *,
    with_write: bool = False,
) -> PlanStep:
    facts = _facts(service_id, time)
    return PlanStep(
        operation_index=index,
        operation_type="book",
        disposition="write_ready" if with_write else "read",
        write_intent=(
            WriteIntent(
                kind="booking",
                authorized=True,
                parameters={
                    key: value
                    for key, value in facts.items()
                    if key
                    in {
                        "service_id",
                        "doctor_id",
                        "device_key",
                        "date",
                        "time",
                        "package_usage",
                    }
                },
                requires_verification=True,
            )
            if with_write
            else None
        ),
        state_action="start_booking",
        response_goal="present_availability",
        facts=facts,
    )


def _grouped_state() -> BookingTaskState:
    components = [
        CustomerConstraints(
            service_id="svc-a",
            doctor_id="doc-1",
            date=DATE,
            time=TIME_A,
        ),
        CustomerConstraints(
            service_id="svc-b",
            doctor_id="doc-1",
            device_key="prime_lase",
            date=DATE,
            time=TIME_B,
        ),
    ]
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=components[0],
        grouped=GroupedBookingState(components=components),
    )


def test_grouped_plan_captures_durable_component_state() -> None:
    plan = TurnPlan(
        steps=[
            _step(0, "svc-a", TIME_A),
            _step(1, "svc-b", TIME_B),
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            _operation("svc-a", time=TIME_A),
            _operation("svc-b", time=TIME_B),
        ]
    )

    task = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=None,
        now=NOW,
        turn_id="turn-1",
    )

    assert task is not None
    assert task.grouped is not None
    assert [item.service_id for item in task.grouped.components] == ["svc-a", "svc-b"]
    assert task.write_authorization.authorized is True


def test_side_read_preserves_full_group_unchanged() -> None:
    state = _grouped_state()
    pricing = TurnOperation(
        type="pricing",
        entities=TurnEntities(
            service=EntityReference(ref=_ref("service", "svc-b")),
            device=EntityReference(ref=_ref("device", "prime_lase")),
        ),
        execution_intent="informational",
        requested_service_details=["price"],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        state_action="none",
        response_goal="answer_price",
    )

    transition = apply_step_state(
        state,
        step=step,
        operation=pricing,
        reads=None,
        now=NOW,
        turn_id="turn-2",
    )

    assert transition.reason == "side_read_preserved"
    assert transition.changed is False
    assert transition.active_task == state
    assert transition.active_task is not None
    assert transition.active_task.grouped is not None
    assert len(transition.active_task.grouped.components) == 2
def test_resume_reconstructs_all_grouped_components() -> None:
    state = _grouped_state()
    resume = TiaTurnUnderstanding(
        operations=[_operation("svc-b", time=TIME_A)]
    )

    restored, groups = restore_grouped_booking_understanding(
        resume,
        active_task=state,
        context=_context(),
    )

    assert len(restored.operations) == 2
    assert set(groups) == {0, 1}
    services = [
        _context().resolve(operation.entities.service.ref, expected_kind="service")
        for operation in restored.operations
        if operation.entities.service is not None
    ]
    assert services == ["svc-a", "svc-b"]
    assert restored.operations[0].entities.time == TIME_A
    assert restored.operations[1].entities.time == TIME_A


def test_semantic_view_exposes_all_grouped_components_as_refs() -> None:
    state = _grouped_state()

    safe = active_task_semantic_view(
        state.model_dump(mode="json"),
        context=_context(),
    )

    grouped = safe["grouped_components"]
    assert isinstance(grouped, list)
    assert len(grouped) == 2
    assert {item["service_ref"] for item in grouped} == {
        _ref("service", "svc-a"),
        _ref("service", "svc-b"),
    }


def test_implicit_component_loss_cannot_authorize_single_write() -> None:
    state = _grouped_state()
    plan = TurnPlan(steps=[_step(0, "svc-b", TIME_A, with_write=True)])

    guarded = enforce_grouped_booking_write_guard(
        plan,
        active_task=state,
        candidate_task=None,
        explicit_narrowing=False,
    )

    step = guarded.steps[0]
    assert step.write_intent is None
    assert step.disposition == "clarify"
    assert step.facts["grouped_continuity_guard_blocked"] is True


def test_explicit_component_removal_can_collapse_to_single_booking() -> None:
    state = _grouped_state()
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "svc-a",
                time=TIME_A,
                grouped_booking_action="remove_other_components",
            )
        ]
    )

    narrowed = explicitly_narrow_grouped_booking_task(
        turn,
        active_task=state,
        context=_context(),
    )

    assert narrowed is not None
    assert narrowed.grouped is None
    assert narrowed.constraints.service_id == "svc-a"
    assert narrowed.write_authorization.authorized is True


def test_service_replacement_full_group_becomes_a_plus_c() -> None:
    existing = _grouped_state()
    plan = TurnPlan(
        steps=[
            _step(0, "svc-a", TIME_A),
            _step(1, "svc-c", TIME_B),
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            _operation("svc-a", time=TIME_A),
            _operation("svc-c", time=TIME_B),
        ]
    )

    updated = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=existing,
        now=NOW,
        turn_id="turn-replace",
    )

    assert updated is not None
    assert updated.grouped is not None
    assert [item.service_id for item in updated.grouped.components] == ["svc-a", "svc-c"]
    assert updated.version == existing.version + 1


def test_unknown_single_component_does_not_silently_replace_group() -> None:
    state = _grouped_state()
    turn = TiaTurnUnderstanding(operations=[_operation("svc-c", time=TIME_A)])

    restored, groups = restore_grouped_booking_understanding(
        turn,
        active_task=state,
        context=_context(),
    )
    assert restored == turn
    assert groups == {}

    plan = TurnPlan(steps=[_step(0, "svc-c", TIME_A, with_write=True)])
    guarded = enforce_grouped_booking_write_guard(
        plan,
        active_task=state,
        candidate_task=None,
        explicit_narrowing=False,
    )
    assert guarded.steps[0].write_intent is None


def test_group_survives_persist_side_read_reload_and_resume_without_llm() -> None:
    initial_turn = TiaTurnUnderstanding(
        operations=[
            _operation("svc-a", time=TIME_A),
            _operation("svc-b", time=TIME_B),
        ]
    )
    initial_plan = TurnPlan(
        steps=[
            _step(0, "svc-a", TIME_A),
            _step(1, "svc-b", TIME_B),
        ]
    )
    task = grouped_booking_task_from_plan(
        initial_plan,
        understanding=initial_turn,
        existing=None,
        now=NOW,
        turn_id="turn-1",
    )
    assert task is not None
    reloaded = BookingTaskState.model_validate_json(task.model_dump_json())
    assert reloaded.grouped is not None
    assert len(reloaded.grouped.components) == 2

    price_operation = TurnOperation(
        type="pricing",
        entities=TurnEntities(
            service=EntityReference(ref=_ref("service", "svc-b")),
            device=EntityReference(ref=_ref("device", "prime_lase")),
        ),
        execution_intent="informational",
        requested_service_details=["price"],
    )
    side_step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        state_action="none",
        response_goal="answer_price",
    )
    preserved = apply_step_state(
        reloaded,
        step=side_step,
        operation=price_operation,
        reads=None,
        now=NOW,
        turn_id="turn-2",
    ).active_task
    assert isinstance(preserved, BookingTaskState)
    after_side_read = BookingTaskState.model_validate_json(
        preserved.model_dump_json()
    )
    assert after_side_read.grouped is not None
    assert len(after_side_read.grouped.components) == 2

    resume = TiaTurnUnderstanding(
        operations=[_operation("svc-b", time=TIME_A)]
    )
    reconstructed, groups = restore_grouped_booking_understanding(
        resume,
        active_task=after_side_read,
        context=_context(),
    )
    assert len(reconstructed.operations) == 2

    planned = plan_turn(
        reconstructed,
        PlannerContext(
            semantic_context=_context(),
            active_task=after_side_read,
            now=NOW,
        ),
    )
    normalized = normalize_compound_turn_plan(
        planned,
        catalog={
            "services": [
                {"id": "svc-a", "duration_minutes": 60},
                {"id": "svc-b", "duration_minutes": 15},
            ]
        },
        operation_visit_groups=groups,
    )
    candidate = grouped_booking_task_from_plan(
        normalized,
        understanding=reconstructed,
        existing=after_side_read,
        now=NOW,
        turn_id="turn-3",
    )
    guarded = enforce_grouped_booking_write_guard(
        normalized,
        active_task=after_side_read,
        candidate_task=candidate,
        explicit_narrowing=False,
    )

    assert len(guarded.steps) == 2
    # Single-service commercial/device gating must not intercept restored grouped writes.
    assert all(step.response_goal == "present_availability" for step in guarded.steps)
    assert all(step.write_intent is not None for step in guarded.steps)
    write_groups = [compound_write_group(step) for step in guarded.steps]
    assert write_groups[0] is not None
    assert write_groups[0] == write_groups[1]
    assert {
        str(step.facts["service_id"])
        for step in guarded.steps
    } == {"svc-a", "svc-b"}
