from __future__ import annotations

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnOperation
from app.services.agent_v2.multi_service_booking import select_main_service_id
from app.services.agent_v2.planner import TurnPlan
from app.services.agent_v2.state import BookingTaskState


def _same_visit_signature(operation: TurnOperation) -> tuple[object, ...]:
    entities = operation.entities
    return (
        entities.doctor.model_dump_json() if entities.doctor is not None else None,
        entities.branch.model_dump_json() if entities.branch is not None else None,
        entities.device.model_dump_json() if entities.device is not None else None,
        entities.date.model_dump_json() if entities.date is not None else None,
        entities.time.model_dump_json() if entities.time is not None else None,
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
    active_task: BookingTaskState | None = None,
) -> tuple[TiaTurnUnderstanding, dict[int, list[str]]]:
    """Bind one same-visit canonical service set to a backend-selected main service.

    The model chooses only the minimal non-overlapping catalog set. Python owns the
    duration-based main-service choice; the full ordered service set remains server metadata.
    """
    normalized: list[TurnOperation] = []
    service_sets: dict[int, list[str]] = {}
    for index, operation in enumerate(turn.operations):
        entity = operation.entities.service
        operation_type = getattr(operation, "type", None)
        execution_intent = getattr(operation, "execution_intent", "execute")
        canonical_booking_set = (
            operation_type == "book"
            or (operation_type == "continue_active" and execution_intent == "execute")
        )
        fresh_grouped_availability = (
            operation_type == "availability"
            and execution_intent == "informational"
            and active_task is None
        )
        explicit_grouped_service_mutation = (
            operation_type in {"book", "continue_active"}
            and active_task is not None
            and len(active_task.requested_service_ids) > 1
            and operation.active_task_relationship != "replace"
            and not operation.fresh_task
            and operation.grouped_booking_action in {"preserve_group", "remove_other_components"}
        )
        explicit_grouped_service_correction = (
            explicit_grouped_service_mutation
            and operation.grouped_booking_action == "remove_other_components"
        )
        explicit_singleton_narrowing = (
            explicit_grouped_service_correction
            and entity is not None
            and isinstance(entity.ref, str)
            and bool(entity.ref)
            and not entity.candidate_refs
        )
        if (
            not (
                canonical_booking_set
                or explicit_grouped_service_mutation
                or fresh_grouped_availability
            )
            or entity is None
            or (entity.candidate_mode != "set" and not explicit_singleton_narrowing)
        ):
            normalized.append(operation)
            continue

        service_refs = list(
            dict.fromkeys(
                ref
                for ref in [entity.ref, *entity.candidate_refs]
                if isinstance(ref, str) and ref
            )
        )
        if not service_refs:
            normalized.append(operation)
            continue

        resolved: list[tuple[str, str]] = []
        for ref in service_refs:
            canonical = semantic_context.resolve(ref, expected_kind="service")
            if canonical is not None and all(item[1] != canonical for item in resolved):
                resolved.append((ref, canonical))
        if not resolved:
            normalized.append(operation)
            continue

        canonical_ids = [canonical for _, canonical in resolved]
        if (
            active_task is not None
            and operation.active_task_relationship != "replace"
            and not operation.fresh_task
        ):
            current_ids = list(active_task.requested_service_ids)
            if not current_ids and active_task.constraints.service_id:
                current_ids = [active_task.constraints.service_id]
            if operation.grouped_booking_action == "preserve_group":
                canonical_ids = [
                    *current_ids,
                    *(service_id for service_id in canonical_ids if service_id not in current_ids),
                ]
            elif operation.grouped_booking_action == "remove_other_components":
                selected = set(canonical_ids)
                canonical_ids = [
                    *(service_id for service_id in current_ids if service_id in selected),
                    *(service_id for service_id in canonical_ids if service_id not in current_ids),
                ]
        main_id = select_main_service_id(catalog, canonical_ids)
        ref_by_id = {canonical: ref for ref, canonical in resolved}
        for ref, target in semantic_context.reference_map.items():
            if target.kind == "service" and target.canonical_id in canonical_ids:
                ref_by_id.setdefault(target.canonical_id, ref)
        main_ref = ref_by_id.get(main_id)
        if main_ref is None:
            normalized.append(operation)
            continue
        # Equivalent provider shapes must not change single-service business semantics.
        # A one-item candidate_mode=set becomes the same grounded singleton ref unless
        # it is authoritatively narrowing an existing grouped booking.
        if len(canonical_ids) > 1 or explicit_grouped_service_correction:
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

    if all(current is original for current, original in zip(normalized, turn.operations, strict=True)):
        return turn, service_sets
    normalized_turn = turn.model_copy(update={"operations": normalized})
    return normalized_turn, service_sets


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
        state_action = step.state_action
        if (
            step.operation_type in {"availability", "book"}
            and len(requested) > 1
            and state_action == "none"
            and step.write_intent is None
        ):
            # A typed same-visit set remains durable even when this turn is read-only.
            # Authorization stays separate and may only be granted by execution semantics.
            state_action = "start_booking"
            facts["grouped_read_only_task"] = True
        steps.append(
            step.model_copy(
                update={
                    "facts": facts,
                    "write_intent": write_intent,
                    "state_action": state_action,
                }
            )
        )
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
    preserve_visit_device: bool = False,
) -> TurnOperation:
    if (
        preserve_visit_device
        or semantic_context is None
        or service_ref is None
        or operation.entities.device is None
    ):
        return operation
    target = semantic_context.reference_map.get(service_ref)
    if target is None or target.kind != "service":
        return operation
    if target.metadata.get("requires_laser_device") is not False:
        return operation
    return operation.model_copy(
        update={"entities": operation.entities.model_copy(update={"device": None})}
    )


def _service_set_requires_device(
    service_ids: list[str],
    semantic_context: SemanticContext | None,
) -> bool:
    if semantic_context is None or not service_ids:
        return False
    current = set(service_ids)
    return any(
        target.kind == "service"
        and target.canonical_id in current
        and target.metadata.get("requires_laser_device") is True
        for target in semantic_context.reference_map.values()
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
    booking_service_sets: dict[int, list[str]] | None = None,
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
            visit_service_ids = (booking_service_sets or {}).get(source_index, [])
            expanded.append(
                _scope_component_device(
                    operation,
                    service_ref=service_ref,
                    semantic_context=semantic_context,
                    preserve_visit_device=_service_set_requires_device(
                        visit_service_ids, semantic_context
                    ),
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
