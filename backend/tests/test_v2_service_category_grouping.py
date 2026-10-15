from __future__ import annotations

from app.services.agent_v2.multi_service_booking import (
    partition_service_ids_by_operational_category,
    service_operational_category,
)


def _catalog():
    return {
        "services": [
            {"id": "laser-a", "name": "A", "category": "free", "operational_category": "laser", "duration_minutes": 20, "requires_laser_device": True},
            {"id": "laser-b", "name": "B", "category": "different wording", "operational_category": "laser", "duration_minutes": 30, "requires_laser_device": True},
            {"id": "derm-c", "name": "C", "category": "laser-looking", "operational_category": "dermatology", "duration_minutes": 45, "requires_laser_device": False},
            {"id": "unknown-d", "name": "Laser by name only", "category": "laser", "duration_minutes": 25, "requires_laser_device": True},
            {"id": "unknown-e", "name": "Another", "category": "laser", "duration_minutes": 35, "requires_laser_device": True},
        ]
    }


def test_operational_category_is_only_grouping_authority() -> None:
    catalog = _catalog()
    assert service_operational_category(catalog, "laser-a") == "laser"
    assert service_operational_category(catalog, "derm-c") == "dermatology"
    assert service_operational_category(catalog, "unknown-d") is None


def test_partition_preserves_peer_order_with_same_category_grouped() -> None:
    assert partition_service_ids_by_operational_category(
        _catalog(), ["laser-a", "laser-b", "derm-c"]
    ) == [("laser", ["laser-a", "laser-b"]), ("dermatology", ["derm-c"])]


def test_unknown_categories_fail_closed_as_independent_singletons() -> None:
    assert partition_service_ids_by_operational_category(
        _catalog(), ["unknown-d", "unknown-e"]
    ) == [(None, ["unknown-d"]), (None, ["unknown-e"])]


def test_free_form_category_and_requires_laser_do_not_override_canonical_category() -> None:
    assert partition_service_ids_by_operational_category(
        _catalog(), ["laser-a", "derm-c", "unknown-d"]
    ) == [
        ("laser", ["laser-a"]),
        ("dermatology", ["derm-c"]),
        (None, ["unknown-d"]),
    ]


def test_mixed_category_set_collapses_to_one_operation_per_category_group() -> None:
    from app.agents.v2.semantic_context import build_semantic_context
    from app.agents.v2.turn_contract import EntityReference, TiaTurnUnderstanding, TurnEntities, TurnOperation
    from app.services.agent_v2.turn_normalization import collapse_booking_service_sets

    catalog = _catalog()
    context = build_semantic_context(catalog)
    refs = {
        target.canonical_id: ref
        for ref, target in context.reference_map.items()
        if target.kind == "service"
    }
    turn = TiaTurnUnderstanding(operations=[TurnOperation(
        type="book",
        execution_intent="execute",
        entities=TurnEntities(service=EntityReference(
            candidate_refs=[refs["laser-a"], refs["laser-b"], refs["derm-c"]],
            candidate_mode="set",
        )),
    )])

    collapsed, service_sets = collapse_booking_service_sets(
        turn, semantic_context=context, catalog=catalog
    )

    assert len(collapsed.operations) == 2
    canonical = [
        context.resolve(op.entities.service.ref, expected_kind="service")
        for op in collapsed.operations
        if op.entities.service is not None
    ]
    assert canonical == ["laser-b", "derm-c"]
    assert service_sets == {0: ["laser-a", "laser-b"]}


def test_same_category_set_stays_one_booking_operation() -> None:
    from app.agents.v2.semantic_context import build_semantic_context
    from app.agents.v2.turn_contract import EntityReference, TiaTurnUnderstanding, TurnEntities, TurnOperation
    from app.services.agent_v2.turn_normalization import collapse_booking_service_sets

    catalog = _catalog()
    context = build_semantic_context(catalog)
    refs = {target.canonical_id: ref for ref, target in context.reference_map.items() if target.kind == "service"}
    turn = TiaTurnUnderstanding(operations=[TurnOperation(
        type="book", execution_intent="execute",
        entities=TurnEntities(service=EntityReference(
            candidate_refs=[refs["laser-a"], refs["laser-b"]], candidate_mode="set"
        )),
    )])
    collapsed, service_sets = collapse_booking_service_sets(turn, semantic_context=context, catalog=catalog)
    assert len(collapsed.operations) == 1
    assert service_sets == {0: ["laser-a", "laser-b"]}


def test_unknown_category_services_are_split_into_independent_operations() -> None:
    from app.agents.v2.semantic_context import build_semantic_context
    from app.agents.v2.turn_contract import EntityReference, TiaTurnUnderstanding, TurnEntities, TurnOperation
    from app.services.agent_v2.turn_normalization import collapse_booking_service_sets

    catalog = _catalog()
    context = build_semantic_context(catalog)
    refs = {target.canonical_id: ref for ref, target in context.reference_map.items() if target.kind == "service"}
    turn = TiaTurnUnderstanding(operations=[TurnOperation(
        type="book", execution_intent="execute",
        entities=TurnEntities(service=EntityReference(
            candidate_refs=[refs["unknown-d"], refs["unknown-e"]], candidate_mode="set"
        )),
    )])
    collapsed, service_sets = collapse_booking_service_sets(turn, semantic_context=context, catalog=catalog)
    assert len(collapsed.operations) == 2
    assert service_sets == {}
