from __future__ import annotations

from datetime import datetime

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import EntityReference, TiaTurnUnderstanding, TurnOperation
from app.services.agent_v2.active_task_progress import resolved_operation_parameters
from app.services.agent_v2.compound_turn_policy import (
    compound_visit_group,
    compound_write_group,
)
from app.services.agent_v2.planner import PlanStep, TurnPlan
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    GroupedBookingState,
    WriteAuthorization,
)

_BOOKING_OPERATION_TYPES = frozenset({"book", "continue_active"})


def _constraints_from_step(step: PlanStep) -> CustomerConstraints:
    facts = step.facts
    return CustomerConstraints.model_validate(
        {
            "service_id": str(facts["service_id"]) if facts.get("service_id") else None,
            "doctor_id": str(facts["doctor_id"]) if facts.get("doctor_id") else None,
            "device_key": str(facts["device_key"]) if facts.get("device_key") else None,
            "date": facts.get("date"),
            "time": facts.get("time"),
            "package_usage": facts.get("package_usage") or "unspecified",
        }
    )


def _grouped_booking_steps(plan: TurnPlan) -> list[PlanStep] | None:
    groups: dict[str, list[PlanStep]] = {}
    for step in plan.steps:
        if step.operation_type not in _BOOKING_OPERATION_TYPES:
            continue
        group = compound_visit_group(step)
        if group is None or step.facts.get("service_id") in (None, ""):
            continue
        groups.setdefault(group, []).append(step)

    candidates = [
        steps
        for steps in groups.values()
        if len({str(step.facts["service_id"]) for step in steps}) >= 2
    ]
    return candidates[0] if len(candidates) == 1 else None


def _authorized_group(
    steps: list[PlanStep],
    understanding: TiaTurnUnderstanding,
) -> bool:
    for step in steps:
        if not (0 <= step.operation_index < len(understanding.operations)):
            return False
        operation = understanding.operations[step.operation_index]
        if operation.type != "book" or operation.execution_intent != "execute":
            return False
    return True


def grouped_booking_task_from_plan(
    plan: TurnPlan,
    *,
    understanding: TiaTurnUnderstanding,
    existing: BookingTaskState | None,
    now: datetime,
    turn_id: str,
) -> BookingTaskState | None:
    """Capture one same-visit component set as durable booking state."""
    steps = _grouped_booking_steps(plan)
    if steps is None:
        return None

    components = [_constraints_from_step(step) for step in steps]
    grouped = GroupedBookingState(components=components)
    if existing is not None and existing.grouped is not None:
        if existing.grouped == grouped and existing.constraints == components[0]:
            return existing
        return existing.model_copy(
            update={
                "status": "collecting",
                "constraints": components[0],
                "grouped": grouped,
                "derived": DerivedBookingState(),
                "option_snapshot": None,
                "version": existing.version + 1,
            }
        )

    authorized = _authorized_group(steps, understanding)
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=authorized,
            source_turn_id=turn_id if authorized else None,
            granted_at=now if authorized else None,
        ),
        constraints=components[0],
        grouped=grouped,
    )


def _canonical_ref(
    context: SemanticContext,
    *,
    kind: str,
    canonical_id: str | None,
) -> str | None:
    if canonical_id is None:
        return None
    matches = [
        ref
        for ref, target in context.reference_map.items()
        if target.kind == kind and str(target.canonical_id) == str(canonical_id)
    ]
    return matches[0] if len(matches) == 1 else None


def _entity_ref(ref: str | None) -> EntityReference | None:
    if ref is None:
        return None
    return EntityReference(
        text=None,
        ref=ref,
        candidate_refs=[],
        candidate_mode="ambiguous",
    )


def _operation_for_component(
    template: TurnOperation,
    component: CustomerConstraints,
    *,
    context: SemanticContext,
) -> TurnOperation | None:
    service_ref = _canonical_ref(
        context,
        kind="service",
        canonical_id=component.service_id,
    )
    if service_ref is None:
        return None
    doctor_ref = _canonical_ref(
        context,
        kind="doctor",
        canonical_id=component.doctor_id,
    )
    if component.doctor_id is not None and doctor_ref is None:
        return None
    device_ref = _canonical_ref(
        context,
        kind="device",
        canonical_id=component.device_key,
    )
    if component.device_key is not None and device_ref is None:
        return None

    entities = template.entities.model_copy(
        update={
            "service": _entity_ref(service_ref),
            "doctor": _entity_ref(doctor_ref),
            "device": _entity_ref(device_ref),
            "appointment": None,
            "package": None,
            "date": component.date,
            "time": component.time,
        }
    )
    return template.model_copy(
        update={
            "type": "book",
            "entities": entities,
            "source_appointment": None,
            "selection": None,
            "package_usage": component.package_usage,
            "execution_intent": "execute",
            "grouped_booking_action": "preserve_group",
        }
    )


def _merge_component(
    component: CustomerConstraints,
    *,
    operation: TurnOperation,
    params: dict[str, object],
    apply_to_all: bool,
) -> CustomerConstraints:
    updates: dict[str, object] = {}
    for key in ("doctor_id", "date", "time"):
        if key in params:
            updates[key] = params[key]
    if not apply_to_all and "device_key" in params:
        updates["device_key"] = params["device_key"]
    if operation.package_usage != "unspecified":
        updates["package_usage"] = operation.package_usage
    payload = component.model_dump(mode="json")
    payload.update(updates)
    return CustomerConstraints.model_validate(payload)


def restore_grouped_booking_understanding(
    turn: TiaTurnUnderstanding,
    *,
    active_task: BookingTaskState | None,
    context: SemanticContext,
) -> tuple[TiaTurnUnderstanding, dict[int, str]]:
    """Restore all durable components when a continuation mentions only one of them."""
    if active_task is None or active_task.grouped is None:
        return turn, {}

    executable = [
        (index, operation)
        for index, operation in enumerate(turn.operations)
        if operation.type in _BOOKING_OPERATION_TYPES
        and operation.execution_intent == "execute"
    ]
    if len(executable) != 1:
        return turn, {}
    source_index, operation = executable[0]
    if operation.grouped_booking_action == "remove_other_components":
        return turn, {}

    params = resolved_operation_parameters(operation, context=context)
    target_service = params.get("service_id")
    service_ids = {
        component.service_id
        for component in active_task.grouped.components
        if component.service_id is not None
    }
    if target_service is not None and str(target_service) not in service_ids:
        return turn, {}

    rebuilt: list[TurnOperation] = []
    for component in active_task.grouped.components:
        apply_to_all = target_service is None
        if target_service is not None and component.service_id != str(target_service):
            merged = component
        else:
            merged = _merge_component(
                component,
                operation=operation,
                params=params,
                apply_to_all=apply_to_all,
            )
        rebuilt_operation = _operation_for_component(
            operation,
            merged,
            context=context,
        )
        if rebuilt_operation is None:
            return turn, {}
        rebuilt.append(rebuilt_operation)

    if len(turn.operations) - 1 + len(rebuilt) > 6:
        return turn, {}
    operations: list[TurnOperation] = []
    group_indexes: dict[int, str] = {}
    group_key = f"persisted-group:{active_task.version}"
    for index, current in enumerate(turn.operations):
        if index != source_index:
            operations.append(current)
            continue
        for rebuilt_operation in rebuilt:
            group_indexes[len(operations)] = group_key
            operations.append(rebuilt_operation)
    return turn.model_copy(update={"operations": operations}), group_indexes


def explicitly_narrow_grouped_booking_task(
    turn: TiaTurnUnderstanding,
    *,
    active_task: BookingTaskState | None,
    context: SemanticContext,
) -> BookingTaskState | None:
    """Collapse a group only when structured semantics explicitly authorize narrowing."""
    if active_task is None or active_task.grouped is None:
        return None
    executable = [
        operation
        for operation in turn.operations
        if operation.type in _BOOKING_OPERATION_TYPES
        and operation.execution_intent == "execute"
    ]
    if len(executable) != 1:
        return None
    operation = executable[0]
    if operation.grouped_booking_action != "remove_other_components":
        return None
    params = resolved_operation_parameters(operation, context=context)
    service_id = params.get("service_id")
    if not isinstance(service_id, str):
        return None
    component = next(
        (
            item
            for item in active_task.grouped.components
            if item.service_id == service_id
        ),
        None,
    )
    if component is None:
        return None
    narrowed = _merge_component(
        component,
        operation=operation,
        params=params,
        apply_to_all=False,
    )
    return active_task.model_copy(
        update={
            "status": "collecting",
            "constraints": narrowed,
            "grouped": None,
            "derived": DerivedBookingState(),
            "option_snapshot": None,
            "version": active_task.version + 1,
        }
    )


def _expected_group_services(
    task: BookingTaskState | None,
) -> set[str]:
    if task is None or task.grouped is None:
        return set()
    return {
        component.service_id
        for component in task.grouped.components
        if component.service_id is not None
    }


def enforce_grouped_booking_write_guard(
    plan: TurnPlan,
    *,
    active_task: BookingTaskState | None,
    candidate_task: BookingTaskState | None,
    explicit_narrowing: bool,
) -> TurnPlan:
    """Never authorize a single-component write while a multi-component task is active."""
    if explicit_narrowing:
        return plan

    expected = _expected_group_services(candidate_task) or _expected_group_services(active_task)
    if len(expected) < 2:
        return plan

    booking_steps = [
        step
        for step in plan.steps
        if step.operation_type in _BOOKING_OPERATION_TYPES
        and step.facts.get("service_id") not in (None, "")
    ]
    actual = {str(step.facts["service_id"]) for step in booking_steps}
    groups = {compound_write_group(step) for step in booking_steps}
    complete_shape = (
        actual == expected
        and len(booking_steps) == len(expected)
        and len(groups) == 1
        and None not in groups
    )
    if complete_shape:
        return plan
    if not any(step.write_intent is not None for step in booking_steps):
        return plan

    guarded: list[PlanStep] = []
    for step in plan.steps:
        if step not in booking_steps or step.write_intent is None:
            guarded.append(step)
            continue
        update: dict[str, object] = {
            "write_intent": None,
            "facts": {
                **step.facts,
                "grouped_continuity_guard_blocked": True,
                "grouped_continuity_expected_services": sorted(expected),
            },
        }
        if step.disposition == "write_ready":
            update.update(
                {
                    "disposition": "clarify",
                    "response_goal": "clarification",
                    "clarification_field": "intent",
                }
            )
        guarded.append(step.model_copy(update=update))
    return plan.model_copy(update={"steps": guarded})
