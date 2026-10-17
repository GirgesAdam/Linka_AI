from __future__ import annotations

from datetime import datetime

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import (
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnOperation,
)
from app.services.agent_v2.active_task_progress import resolved_operation_parameters
from app.services.agent_v2.compound_turn_policy import (
    compound_visit_group,
    compound_write_group,
)
from app.services.agent_v2.multi_service_booking import (
    common_laser_device_keys,
    laser_service_ids,
    select_main_service_id,
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


def _grouped_state_time(
    value: object,
    *,
    semantic: TimeConstraint | None,
) -> object:
    """Carry only unresolved 10/11 AM/PM meaning into durable grouped state."""
    if not isinstance(value, dict) or semantic is None:
        return value
    try:
        constraint = TimeConstraint.model_validate(value)
    except ValueError:
        return value
    if (
        constraint.start_time_ambiguity != "twelve_hour"
        or semantic.start_time_ambiguity != "twelve_hour"
        or constraint.start_time != semantic.start_time
        or semantic.spoken_period_hint not in {"am", "pm"}
    ):
        return value
    try:
        hour = int(str(constraint.start_time).split(":", 1)[0])
    except (TypeError, ValueError):
        return value
    if hour not in {10, 11}:
        return value
    return {**value, "spoken_period_hint": semantic.spoken_period_hint}


def _constraints_from_step(
    step: PlanStep,
    *,
    operation: TurnOperation,
) -> CustomerConstraints:
    facts = step.facts
    return CustomerConstraints.model_validate(
        {
            "service_id": str(facts["service_id"]) if facts.get("service_id") else None,
            "doctor_id": str(facts["doctor_id"]) if facts.get("doctor_id") else None,
            "branch_id": str(facts["branch_id"]) if facts.get("branch_id") else None,
            "device_key": str(facts["device_key"]) if facts.get("device_key") else None,
            "date": facts.get("date"),
            "time": _grouped_state_time(
                facts.get("time"), semantic=operation.entities.time
            ),
            "time_clock_source": operation.clock_source,
            "package_usage": facts.get("package_usage") or "unspecified",
        }
    )


def _grouped_booking_steps(plan: TurnPlan) -> list[PlanStep] | None:
    groups: dict[str, list[PlanStep]] = {}
    for step in plan.steps:
        if step.operation_type not in _BOOKING_OPERATION_TYPES:
            continue
        if step.facts.get("compound_visit_independent_category_resources") is True:
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

    components: list[CustomerConstraints] = []
    seen_service_ids: set[str] = set()
    for step in steps:
        constraints = _constraints_from_step(
            step,
            operation=understanding.operations[step.operation_index],
        )
        requested_raw = step.facts.get("requested_service_ids")
        requested = (
            list(dict.fromkeys(requested_raw))
            if isinstance(requested_raw, list)
            and all(isinstance(item, str) and item for item in requested_raw)
            else []
        )
        service_ids = (
            requested
            if len(requested) > 1 and constraints.service_id in requested
            else [constraints.service_id]
        )
        for service_id in service_ids:
            if service_id is None or service_id in seen_service_ids:
                continue
            components.append(constraints.model_copy(update={"service_id": service_id}))
            seen_service_ids.add(service_id)
    grouped = GroupedBookingState(components=components)
    requested_service_ids = [
        component.service_id
        for component in components
        if component.service_id is not None
    ]
    if existing is not None:
        main_constraints = components[0]
        if existing.grouped is None and existing.constraints.service_id is not None:
            main_constraints = next(
                (
                    component
                    for component in components
                    if component.service_id == existing.constraints.service_id
                ),
                main_constraints,
            )
        if existing.grouped == grouped and existing.constraints == main_constraints:
            return existing
        requested = list(existing.requested_service_ids) or [
            component.service_id
            for component in components
            if component.service_id is not None
        ]
        service_device_keys = {
            key: value
            for key, value in existing.service_device_keys.items()
            if key in requested
        }
        service_device_keys.update(
            {
                component.service_id: component.device_key
                for component in components
                if component.service_id is not None and component.device_key is not None
            }
        )
        return existing.model_copy(
            update={
                "status": "collecting",
                "constraints": main_constraints,
                "requested_service_ids": requested,
                "service_device_keys": service_device_keys,
                "grouped": grouped,
                "proposal": None,
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
        requested_service_ids=requested_service_ids,
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
    branch_ref = _canonical_ref(
        context,
        kind="branch",
        canonical_id=component.branch_id,
    )
    if component.branch_id is not None and branch_ref is None:
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
            "branch": _entity_ref(branch_ref),
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
            "execution_intent": template.execution_intent,
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
    for key in ("doctor_id", "branch_id", "date"):
        if key in params:
            updates[key] = params[key]
    if "time" in params:
        updates["time"] = _grouped_state_time(
            params["time"], semantic=operation.entities.time
        )
    if not apply_to_all and "device_key" in params:
        updates["device_key"] = params["device_key"]
    if operation.package_usage != "unspecified":
        updates["package_usage"] = operation.package_usage
    payload = component.model_dump(mode="json")
    if "time" not in updates and component.time is not None:
        # Normal serialization excludes spoken_period_hint.  An omitted-time
        # continuation must retain the durable TimeConstraint object unchanged.
        payload["time"] = component.time
    payload.update(updates)
    return CustomerConstraints.model_validate(payload)


def restore_grouped_booking_understanding(
    turn: TiaTurnUnderstanding,
    *,
    active_task: BookingTaskState | None,
    context: SemanticContext,
    booking_service_sets: dict[int, list[str]] | None = None,
) -> tuple[TiaTurnUnderstanding, dict[int, str]]:
    """Restore all durable components when a continuation mentions only one of them."""
    if active_task is None:
        return turn, {}
    if active_task.grouped is not None:
        durable_components = [
            component.model_copy(
                update={
                    "device_key": active_task.service_device_keys[component.service_id]
                }
            )
            if component.device_key is None
            and component.service_id in active_task.service_device_keys
            else component
            for component in active_task.grouped.components
        ]
    elif len(active_task.requested_service_ids) > 1:
        durable_components = [
            active_task.constraints.model_copy(
                update={
                    "service_id": service_id,
                    "device_key": active_task.service_device_keys.get(service_id),
                }
            )
            for service_id in active_task.requested_service_ids
        ]
    else:
        return turn, {}

    executable = [
        (index, operation)
        for index, operation in enumerate(turn.operations)
        if operation.type in _BOOKING_OPERATION_TYPES
        and operation.execution_intent in {"execute", "informational"}
    ]
    if len(executable) > 1:
        durable_devices = {
            component.service_id: component.device_key
            for component in durable_components
            if component.service_id is not None
        }
        operations = list(turn.operations)
        changed = False
        for operation_index, operation in executable:
            if (
                operation.grouped_booking_action != "preserve_group"
                or operation.fresh_task
                or operation.active_task_relationship == "replace"
                or operation.entities.device is not None
            ):
                continue
            params = resolved_operation_parameters(operation, context=context)
            representative_service = params.get("service_id")
            if not isinstance(representative_service, str) or not representative_service:
                continue
            represented_raw = (
                booking_service_sets.get(operation_index)
                if booking_service_sets is not None
                else None
            )
            represented_services = (
                list(dict.fromkeys(represented_raw))
                if isinstance(represented_raw, list)
                and represented_raw
                and all(isinstance(item, str) and item for item in represented_raw)
                and representative_service in represented_raw
                else [representative_service]
            )
            represented_devices = [
                durable_devices.get(service_id) for service_id in represented_services
            ]
            if any(device_key is None for device_key in represented_devices):
                continue
            common_devices = {str(device_key) for device_key in represented_devices}
            if len(common_devices) != 1:
                continue
            device_key = next(iter(common_devices))
            device_ref = _canonical_ref(
                context,
                kind="device",
                canonical_id=device_key,
            )
            if device_ref is None:
                continue
            operations[operation_index] = operation.model_copy(
                update={
                    "entities": operation.entities.model_copy(
                        update={"device": _entity_ref(device_ref)}
                    )
                }
            )
            changed = True
        return (
            turn.model_copy(update={"operations": operations}) if changed else turn,
            {},
        )
    if len(executable) != 1:
        return turn, {}
    source_index, operation = executable[0]
    if operation.grouped_booking_action == "remove_other_components":
        return turn, {}

    params = resolved_operation_parameters(operation, context=context)
    target_service = params.get("service_id")
    service_ids = {
        component.service_id
        for component in durable_components
        if component.service_id is not None
    }
    representative_set = (
        booking_service_sets.get(source_index)
        if booking_service_sets is not None
        else None
    )
    representative_is_full_group = (
        operation.grouped_booking_action == "preserve_group"
        and representative_set is not None
        and len(representative_set) > 1
        and {str(service_id) for service_id in representative_set} == service_ids
    )
    if representative_is_full_group:
        target_service = None
    if target_service is not None and str(target_service) not in service_ids:
        return turn, {}

    rebuilt: list[TurnOperation] = []
    for component in durable_components:
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
    catalog: dict[str, object] | None = None,
    booking_service_sets: dict[int, list[str]] | None = None,
) -> BookingTaskState | None:
    """Collapse a group only when structured semantics explicitly authorize narrowing."""
    if active_task is None:
        return None
    has_legacy_group = active_task.grouped is not None
    has_requested_set = len(active_task.requested_service_ids) > 1
    if not has_legacy_group and not has_requested_set:
        return None
    corrections = [
        (index, operation)
        for index, operation in enumerate(turn.operations)
        if operation.type in _BOOKING_OPERATION_TYPES
    ]
    if not corrections or not any(
        operation.grouped_booking_action == "remove_other_components"
        for _, operation in corrections
    ):
        return None

    if active_task.grouped is not None:
        components_by_service = {
            component.service_id: component
            for component in active_task.grouped.components
            if component.service_id is not None
        }
    else:
        components_by_service: dict[str, CustomerConstraints] = {}
        for service_id in active_task.requested_service_ids:
            device_key = active_task.service_device_keys.get(service_id)
            if device_key is None and active_task.constraints.service_id == service_id:
                device_key = active_task.constraints.device_key
            components_by_service[service_id] = active_task.constraints.model_copy(
                update={"service_id": service_id, "device_key": device_key}
            )

    narrowed_components: list[CustomerConstraints] = []
    seen_services: set[str] = set()
    for operation_index, operation in corrections:
        params = resolved_operation_parameters(operation, context=context)
        authoritative_ids = (booking_service_sets or {}).get(operation_index)
        service_ids = (
            list(authoritative_ids)
            if operation.grouped_booking_action == "remove_other_components" and authoritative_ids
            else [params.get("service_id")]
        )
        for service_id in service_ids:
            if not isinstance(service_id, str) or service_id in seen_services:
                return None
            component = components_by_service.get(service_id)
            if component is None:
                if catalog is None:
                    return None
                # A removal-authorized correction is an authoritative complete service set.
                # New replacement components inherit only visit-level context; service-specific
                # authority (device/package compatibility) is re-derived from the final set.
                component = CustomerConstraints(
                    service_id=service_id,
                    doctor_id=active_task.constraints.doctor_id,
                    branch_id=active_task.constraints.branch_id,
                    date=active_task.constraints.date,
                    time=active_task.constraints.time,
                    time_clock_source=active_task.constraints.time_clock_source,
                )
            narrowed_components.append(
                _merge_component(
                    component,
                    operation=operation,
                    params=params,
                    apply_to_all=False,
                )
            )
            seen_services.add(service_id)

    if not narrowed_components:
        return None
    retained_service_ids = [
        component.service_id
        for component in narrowed_components
        if component.service_id is not None
    ]
    if catalog is not None:
        laser_ids = laser_service_ids(catalog, retained_service_ids)
        common_devices = common_laser_device_keys(catalog, retained_service_ids)
        selected_device = active_task.constraints.device_key
        if not laser_ids:
            selected_device = None
        elif selected_device not in common_devices:
            selected_device = common_devices[0] if len(common_devices) == 1 else None
        main_service_id = select_main_service_id(
            catalog, retained_service_ids, device_key=selected_device
        )

        normalized_components = [
            component.model_copy(
                update={
                    "device_key": (
                        selected_device if component.service_id in laser_ids else None
                    )
                }
            )
            for component in narrowed_components
        ]
        components_by_service = {
            component.service_id: component
            for component in normalized_components
            if component.service_id is not None
        }
        main_constraints = components_by_service[main_service_id]
        retained_device_keys = (
            {service_id: selected_device for service_id in laser_ids}
            if selected_device is not None
            else {}
        )
    else:
        # Direct legacy callers without a catalog can still narrow only to already-known
        # components. Production orchestration always supplies the canonical catalog.
        normalized_components = narrowed_components
        main_constraints = normalized_components[0]
        retained_device_keys = {
            component.service_id: component.device_key
            for component in normalized_components
            if component.service_id is not None and component.device_key is not None
        }
    narrowed_group = (
        GroupedBookingState(components=normalized_components)
        if has_legacy_group and len(normalized_components) > 1
        else None
    )
    return active_task.model_copy(
        update={
            "status": "collecting",
            "constraints": main_constraints,
            "requested_service_ids": (
                retained_service_ids
                if has_requested_set
                else active_task.requested_service_ids
            ),
            "service_device_keys": (
                retained_device_keys
                if has_requested_set
                else active_task.service_device_keys
            ),
            "grouped": narrowed_group,
            "proposal": None,
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
