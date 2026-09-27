from __future__ import annotations

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnOperation

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

    if not visit_groups and expanded == turn.operations:
        return turn, {}
    return turn.model_copy(update={"operations": expanded}), visit_groups
