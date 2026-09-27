from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.compound_turn_policy import (
    compound_visit_group,
    compound_write_group,
    normalize_compound_turn_plan,
)
from app.services.agent_v2.planner import PlanStep, ReadRequest, TurnPlan, WriteIntent
from app.services.agent_v2.turn_normalization import expand_multi_service_operations


def _multi_service_operation(kind: str = "availability") -> TurnOperation:
    return TurnOperation(
        type=kind,
        entities=TurnEntities(
            service=EntityReference(
                text="service A and service B",
                candidate_refs=["S1", "S2"],
                candidate_mode="set",
            ),
            doctor=EntityReference(
                text="any suitable doctor",
                candidate_refs=["D1", "D2"],
                candidate_mode="set",
            ),
            date=DateConstraint(mode="next_available"),
        ),
        execution_intent="execute" if kind == "book" else "informational",
    )


def test_multi_service_availability_expands_before_planning() -> None:
    turn = TiaTurnUnderstanding(operations=[_multi_service_operation()])
    expanded, groups = expand_multi_service_operations(turn)

    assert [operation.entities.service.ref for operation in expanded.operations] == ["S1", "S2"]
    assert all(operation.entities.service.candidate_refs == [] for operation in expanded.operations)
    assert groups == {0: "semantic-multi-service:0", 1: "semantic-multi-service:0"}
    assert all(operation.entities.doctor.candidate_refs == ["D1", "D2"] for operation in expanded.operations)


def test_multi_service_book_expands_and_preserves_execution_intent() -> None:
    turn = TiaTurnUnderstanding(operations=[_multi_service_operation("book")])
    expanded, groups = expand_multi_service_operations(turn)

    assert len(expanded.operations) == 2
    assert all(operation.type == "book" for operation in expanded.operations)
    assert all(operation.execution_intent == "execute" for operation in expanded.operations)
    assert len(set(groups.values())) == 1


def test_non_visit_service_set_is_not_expanded() -> None:
    pricing = _multi_service_operation().model_copy(update={"type": "pricing"})
    turn = TiaTurnUnderstanding(operations=[pricing])

    expanded, groups = expand_multi_service_operations(turn)

    assert expanded is turn
    assert groups == {}


def test_expansion_fails_closed_when_turn_would_exceed_operation_limit() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _multi_service_operation(),
            _multi_service_operation(),
            _multi_service_operation(),
            TurnOperation(type="social", entities=TurnEntities()),
        ]
    )

    expanded, groups = expand_multi_service_operations(turn)

    assert expanded is turn
    assert groups == {}


def _availability_step(index: int, service_id: str) -> PlanStep:
    params = {
        "service_id": service_id,
        "doctor_ids": ["D1", "D2"],
        "date": {"mode": "next_available", "start_date": None, "end_date": None},
    }
    return PlanStep(
        operation_index=index,
        operation_type="availability",
        disposition="read",
        reads=[ReadRequest(kind="availability", parameters=params)],
        response_goal="present_availability",
        facts=params,
    )


def test_semantic_multi_service_availability_gets_one_logical_visit_group() -> None:
    plan = TurnPlan(steps=[_availability_step(0, "S1"), _availability_step(1, "S2")])
    normalized = normalize_compound_turn_plan(
        plan,
        catalog={},
        operation_visit_groups={0: "semantic-multi-service:0", 1: "semantic-multi-service:0"},
    )

    assert {compound_visit_group(step) for step in normalized.steps} == {
        "semantic-multi-service:0"
    }
    assert all(compound_write_group(step) is None for step in normalized.steps)


def _purchase(index: int, service_id: str) -> PlanStep:
    return PlanStep(
        operation_index=index,
        operation_type="buy_package",
        disposition="read",
        write_intent=WriteIntent(
            kind="buy_package",
            authorized=True,
            parameters={"service_id": service_id},
        ),
        facts={"service_id": service_id},
    )


def _booking(index: int, service_id: str) -> PlanStep:
    params = {
        "service_id": service_id,
        "date": {"mode": "next_available", "start_date": None, "end_date": None},
    }
    return PlanStep(
        operation_index=index,
        operation_type="book",
        disposition="read",
        reads=[ReadRequest(kind="availability", parameters=params)],
        write_intent=WriteIntent(kind="booking", authorized=True, parameters=params),
        facts=params,
    )


def test_two_package_purchases_and_expanded_bookings_share_atomic_write_group() -> None:
    plan = TurnPlan(
        steps=[
            _purchase(0, "S1"),
            _purchase(1, "S2"),
            _booking(2, "S1"),
            _booking(3, "S2"),
        ]
    )
    normalized = normalize_compound_turn_plan(
        plan,
        catalog={},
        operation_visit_groups={2: "semantic-multi-service:2", 3: "semantic-multi-service:2"},
    )

    groups = [compound_write_group(step) for step in normalized.steps]
    assert all(group is not None for group in groups)
    assert len(set(groups)) == 1
    assert {compound_visit_group(step) for step in normalized.steps if step.operation_type == "book"} == {
        "semantic-multi-service:2"
    }


def _component_device_context():
    return build_semantic_context(
        {
            "services": [
                {"id": "standard", "name": "Standard", "requires_laser_device": False},
                {
                    "id": "laser-a",
                    "name": "Laser A",
                    "requires_laser_device": True,
                    "laser_devices": [{"device_key": "device-a", "device_name": "Device A"}],
                },
                {
                    "id": "laser-b",
                    "name": "Laser B",
                    "requires_laser_device": True,
                    "laser_devices": [{"device_key": "device-b", "device_name": "Device B"}],
                },
            ],
            "doctors": [],
            "appointments": [],
        }
    )


def _service_set_with_device(*refs: str, device_ref: str | None) -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(candidate_refs=list(refs), candidate_mode="set"),
                    device=(EntityReference(ref=device_ref) if device_ref else None),
                    date=DateConstraint(mode="next_available"),
                ),
                execution_intent="execute",
            )
        ]
    )


def test_component_expansion_scopes_explicit_device_to_laser_service() -> None:
    context = _component_device_context()
    turn = _service_set_with_device("S1", "S2", device_ref="V1")

    expanded, groups = expand_multi_service_operations(turn, semantic_context=context)

    assert expanded.operations[0].entities.device is None
    assert expanded.operations[1].entities.device is not None
    assert expanded.operations[1].entities.device.ref == "V1"
    assert groups == {0: "semantic-multi-service:0", 1: "semantic-multi-service:0"}


def test_component_expansion_preserves_missing_device_on_laser_component() -> None:
    context = _component_device_context()
    turn = _service_set_with_device("S1", "S2", device_ref=None)

    expanded, _ = expand_multi_service_operations(turn, semantic_context=context)

    assert expanded.operations[0].entities.device is None
    assert expanded.operations[1].entities.device is None


def test_two_laser_components_do_not_silently_drop_incompatible_explicit_device() -> None:
    context = _component_device_context()
    turn = _service_set_with_device("S2", "S3", device_ref="V1")

    expanded, _ = expand_multi_service_operations(turn, semantic_context=context)

    assert [operation.entities.device.ref for operation in expanded.operations] == ["V1", "V1"]


def test_single_standard_operation_clears_inherited_laser_device() -> None:
    context = _component_device_context()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(ref="S1"),
            device=EntityReference(ref="V1"),
            date=DateConstraint(mode="next_available"),
        ),
        execution_intent="execute",
    )
    normalized, groups = expand_multi_service_operations(
        TiaTurnUnderstanding(operations=[operation]),
        semantic_context=context,
    )

    assert normalized.operations[0].entities.device is None
    assert groups == {}


def test_single_laser_operation_preserves_explicit_incompatible_device_for_compatibility_path() -> None:
    context = _component_device_context()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(ref="S3"),
            device=EntityReference(ref="V1"),
            date=DateConstraint(mode="next_available"),
        ),
        execution_intent="execute",
    )

    normalized, groups = expand_multi_service_operations(
        TiaTurnUnderstanding(operations=[operation]),
        semantic_context=context,
    )

    assert normalized.operations[0].entities.device is not None
    assert normalized.operations[0].entities.device.ref == "V1"
    assert groups == {}
