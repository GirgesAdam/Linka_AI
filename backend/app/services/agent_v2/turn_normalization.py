from __future__ import annotations

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnOperation
from app.services.agent_v2.multi_service_booking import select_main_service_id
from app.services.agent_v2.planner import TurnPlan


def _same_visit_signature(operation: TurnOperation) -> tuple[object, ...]:
    entities = operation.entities
    return (
        entities.doctor.model_dump(mode="json") if entities.doctor is not None else None,
        entities.branch.model_dump(mode="json") if entities.branch is not None else None,
        entities.device.model_dump(mode="json") if entities.device is not None else None,
        entities.date.model_dump(mode="json") if entities.date is not None else None,
        entities.time.model_dump(mode="json") if entities.time is not None else None,
        operation.package_usage,
        operation.continuation_condition,
    )


def merge_same_visit_booking_operations(
    turn: TiaTurnUnderstanding,
) -> TiaTurnUnderstanding:
    """Normalize legacy one-operation-per-service semantics without guessing from text.

    Only executable booking operations with identical typed scheduling constraints are merged.
    Different dates/doctors/branches remain independent and are later guarded from accidental
    multi-write execution.
    """
    groups: dict[tuple[object, ...], list[int]] = {}
    for index, operation in enumerate(turn.operations):
        if (
            getattr(operation, "type", None) != "book"
            or getattr(operation, "execution_intent", "execute") != "execute"
        ):
            continue
        service = operation.entities.service
        if service is None:
            continue
        refs = ([service.ref] if service.ref else list(service.candidate_refs))
        if not refs:
            continue
        groups.setdefault(_same_visit_signature(operation), []).append(index)

    replacements: dict[int, TurnOperation] = {}
    removed: set[int] = set()
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        refs: list[str] = []
        for index in indexes:
            service = turn.operations[index].entities.service
            assert service is not None
            for ref in ([service.ref] if service.ref else list(service.candidate_refs)):
                if ref and ref not in refs:
                    refs.append(ref)
        if len(refs) < 2:
            continue
        first_index = indexes[0]
        first = turn.operations[first_index]
        service = first.entities.service
        assert service is not None
        replacements[first_index] = first.model_copy(
            update={
                "entities": first.entities.model_copy(
                    update={
                        "service": service.model_copy(
                            update={
                                "ref": None,
                                "candidate_refs": refs,
                                "candidate_mode": "set",
                            }
                        )
                    }
                )
            }
        )
        removed.update(indexes[1:])

    if not replacements:
        return turn
    operations = [
        replacements.get(index, operation)
        for index, operation in enumerate(turn.operations)
        if index not in removed
    ]
    return turn.model_copy(update={"operations": operations})


def guard_multiple_booking_writes(plan: TurnPlan) -> TurnPlan:
    """Never fall back to legacy multiple Appointment writes for one Agent turn."""
    bookings = [
        step
        for step in plan.steps
        if step.write_intent is not None and step.write_intent.kind == "booking"
    ]
    if len(bookings) < 2:
        return plan
    first = bookings[0]
    guarded = first.model_copy(
        update={
            "disposition": "clarify",
            "reads": [],
            "write_intent": None,
            "state_action": "none",
            "response_goal": "clarification",
            "clarification_field": "service",
            "facts": {
                **first.facts,
                "multi_service_canonicalization_required": True,
                "multiple_appointment_write_blocked": True,
            },
        }
    )
    non_booking = [
        step
        for step in plan.steps
        if step.write_intent is None or step.write_intent.kind != "booking"
    ]
    return plan.model_copy(update={"steps": [*non_booking, guarded]})


def collapse_booking_service_sets(
    turn: TiaTurnUnderstanding,
    *,
    semantic_context: SemanticContext,
    catalog: dict[str, object],
) -> tuple[TiaTurnUnderstanding, dict[int, list[str]]]:
    """Bind one same-visit canonical service set to a backend-selected main service.

    The model chooses only the minimal non-overlapping catalog set. Python owns the
    duration-based main-service choice; the full ordered service set remains server metadata.
    """
    normalized: list[TurnOperation] = []
    service_sets: dict[int, list[str]] = {}
    for index, operation in enumerate(turn.operations):
        entity = operation.entities.service
        if (
            getattr(operation, "type", None) not in {"book", "continue_active"}
            or getattr(operation, "execution_intent", "execute") != "execute"
            or entity is None
            or entity.candidate_mode != "set"
            or not entity.candidate_refs
        ):
            normalized.append(operation)
            continue

        resolved: list[tuple[str, str]] = []
        for ref in entity.candidate_refs:
            canonical = semantic_context.resolve(ref, expected_kind="service")
            if canonical is not None and all(item[1] != canonical for item in resolved):
                resolved.append((ref, canonical))
        if not resolved:
            normalized.append(operation)
            continue

        canonical_ids = [canonical for _, canonical in resolved]
        main_id = select_main_service_id(catalog, canonical_ids)
        main_ref = next(ref for ref, canonical in resolved if canonical == main_id)
        service_sets[index] = canonical_ids
        normalized.append(
            operation.model_copy(
                update={
                    "entities": operation.entities.model_copy(
                        update={
                            "service": entity.model_copy(
                                update={
                                    "ref": main_ref,
                                    "candidate_refs": [],
                                    "candidate_mode": "ambiguous",
                                }
                            )
                        }
                    )
                }
            )
        )

    if not service_sets:
        return turn, {}
    return turn.model_copy(update={"operations": normalized}), service_sets


def tag_booking_service_sets(
    plan: TurnPlan,
    *,
    service_sets: dict[int, list[str]],
) -> TurnPlan:
    if not service_sets:
        return plan
    steps = []
    for step in plan.steps:
        requested = service_sets.get(step.operation_index)
        if not requested:
            steps.append(step)
            continue
        facts = {
            **step.facts,
            "requested_service_ids": list(requested),
            "multi_service_single_appointment": True,
            "booking_proposal_required": True,
        }
        write_intent = step.write_intent
        if write_intent is not None and write_intent.kind == "booking":
            write_intent = write_intent.model_copy(
                update={
                    "parameters": {
                        **write_intent.parameters,
                        "requested_service_ids": list(requested),
                    }
                }
            )
        steps.append(step.model_copy(update={"facts": facts, "write_intent": write_intent}))
    return plan.model_copy(update={"steps": steps})


_EXPANDABLE_OPERATION_TYPES = frozenset({"availability", "book"})
_MAX_OPERATIONS = 6


def _service_set(operation: TurnOperation) -> list[str]:
    if operation.type not in _EXPANDABLE_OPERATION_TYPES:
        return []
    service = operation.entities.service
    if (
        service is None
        or service.ref is not None
        or service.candidate_mode != "set"
    ):
        return []
    return list(dict.fromkeys(ref for ref in service.candidate_refs if ref))


def _scope_component_device(
    operation: TurnOperation,
    *,
    service_ref: str | None,
    semantic_context: SemanticContext | None,
) -> TurnOperation:
    if semantic_context is None or service_ref is None or operation.entities.device is None:
        return operation
    target = semantic_context.reference_map.get(service_ref)
    if target is None or target.kind != "service":
        return operation
    if target.metadata.get("requires_laser_device") is not False:
        return operation
    return operation.model_copy(
        update={"entities": operation.entities.model_copy(update={"device": None})}
    )


def _constraint_signature(operation: TurnOperation) -> tuple[object, ...]:
    entities = operation.entities
    date = entities.date.model_dump(mode="json") if entities.date is not None else None
    time = entities.time.model_dump(mode="json") if entities.time is not None else None
    branch_ref = entities.branch.ref if entities.branch is not None else None
    return (date, time, branch_ref)


def _implicit_multi_book_visit_group(
    operations: list[TurnOperation],
) -> dict[int, str]:
    # Preserve one same-visit request when interpretation emits separate book ops.
    if len(operations) < 2:
        return {}
    if any(
        operation.type != "book"
        or operation.execution_intent != "execute"
        or operation.entities.service is None
        or operation.entities.service.ref is None
        or operation.source_appointment is not None
        for operation in operations
    ):
        return {}
    service_refs = {operation.entities.service.ref for operation in operations}
    if len(service_refs) != len(operations):
        return {}
    signatures = {
        repr(_constraint_signature(operation))
        for operation in operations
    }
    if len(signatures) != 1:
        return {}
    actions = {operation.grouped_booking_action for operation in operations}
    if len(actions) != 1 or next(iter(actions)) not in {
        "preserve_group",
        "remove_other_components",
    }:
        return {}
    group_key = "semantic-multi-book:0"
    return {index: group_key for index in range(len(operations))}


def expand_multi_service_operations(
    turn: TiaTurnUnderstanding,
    *,
    semantic_context: SemanticContext | None = None,
) -> tuple[TiaTurnUnderstanding, dict[int, str]]:
    """Expand one semantic multi-service visit into addressable component operations.

    The interpreter is allowed to represent a requested same-visit service set as one
    availability/book operation. Runtime planning, verification, and write ownership are
    operation-index based, so expand only those visit-shaped operations before planning.
    Pricing/comparison/service-info sets remain untouched. The returned index map is runtime
    metadata only; it does not change the provider-facing structured-output contract.
    """
    projected = 0
    service_sets: list[list[str]] = []
    for operation in turn.operations:
        refs = _service_set(operation)
        service_sets.append(refs)
        projected += len(refs) if len(refs) > 1 else 1
    if projected > _MAX_OPERATIONS:
        return turn, {}

    expanded: list[TurnOperation] = []
    visit_groups: dict[int, str] = {}
    for source_index, (operation, refs) in enumerate(zip(turn.operations, service_sets, strict=True)):
        if len(refs) <= 1:
            service_ref = (
                operation.entities.service.ref
                if operation.entities.service is not None
                else None
            )
            expanded.append(
                _scope_component_device(
                    operation,
                    service_ref=service_ref,
                    semantic_context=semantic_context,
                )
            )
            continue
        group_key = f"semantic-multi-service:{source_index}"
        assert operation.entities.service is not None
        for ref in refs:
            service = operation.entities.service.model_copy(
                update={"ref": ref, "candidate_refs": []}
            )
            component = operation.model_copy(
                update={
                    "entities": operation.entities.model_copy(update={"service": service})
                }
            )
            component = _scope_component_device(
                component,
                service_ref=ref,
                semantic_context=semantic_context,
            )
            visit_groups[len(expanded)] = group_key
            expanded.append(component)

    if not visit_groups:
        visit_groups = _implicit_multi_book_visit_group(expanded)
    if not visit_groups and expanded == turn.operations:
        return turn, {}
    return turn.model_copy(update={"operations": expanded}), visit_groups
