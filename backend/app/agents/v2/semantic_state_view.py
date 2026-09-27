from __future__ import annotations

from typing import Any

from app.agents.v2.semantic_context import ReferenceKind, SemanticContext


def _reverse_refs(context: SemanticContext, kind: ReferenceKind) -> dict[str, str]:
    return {
        target.canonical_id: ref
        for ref, target in context.reference_map.items()
        if target.kind == kind
    }


def _entity_ref(
    value: object,
    *,
    kind: ReferenceKind,
    context: SemanticContext,
) -> str | None:
    if value in (None, ""):
        return None
    return _reverse_refs(context, kind).get(str(value))


def _entity_refs(
    value: object,
    *,
    kind: ReferenceKind,
    context: SemanticContext,
) -> list[str]:
    if not isinstance(value, list):
        return []
    reverse = _reverse_refs(context, kind)
    return [reverse[str(item)] for item in value if str(item) in reverse]


def _safe_constraints(value: object, context: SemanticContext) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, object] = {}
    mappings: tuple[tuple[str, ReferenceKind, str], ...] = (
        ("service_id", "service", "service_ref"),
        ("doctor_id", "doctor", "doctor_ref"),
        ("device_key", "device", "device_ref"),
        ("package_id", "package", "package_ref"),
    )
    for input_key, kind, output_key in mappings:
        ref = _entity_ref(value.get(input_key), kind=kind, context=context)
        if ref is not None:
            safe[output_key] = ref
    doctor_refs = _entity_refs(value.get("doctor_ids"), kind="doctor", context=context)
    if doctor_refs:
        safe["doctor_refs"] = doctor_refs
    pulse_count = value.get("pulse_count")
    if isinstance(pulse_count, int) and not isinstance(pulse_count, bool) and pulse_count > 0:
        safe["pulse_count"] = pulse_count
    for key in ("date", "time", "package_usage"):
        item = value.get(key)
        if item not in (None, "", {}, []):
            safe[key] = item
    return safe


def _safe_target(value: object, context: SemanticContext) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, object] = {}
    appointment_ref = _entity_ref(
        value.get("appointment_id"),
        kind="appointment",
        context=context,
    )
    if appointment_ref is not None:
        safe["appointment_ref"] = appointment_ref
    safe.update(_safe_constraints(value, context))
    if value.get("start_local") not in (None, ""):
        safe["start_local"] = value.get("start_local")
    return safe


def _safe_option_snapshot(
    value: object,
    *,
    context: SemanticContext,
) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, object] = {}
    purpose = value.get("purpose")
    if purpose not in (None, ""):
        safe["purpose"] = purpose
    lifecycle_action = value.get("lifecycle_action")
    if lifecycle_action in {"cancel_appointment", "confirm_appointment", "reschedule"}:
        safe["lifecycle_action"] = lifecycle_action
    raw_options = value.get("options")
    if not isinstance(raw_options, list):
        return safe
    options: list[dict[str, object]] = []
    for index, raw in enumerate(raw_options, start=1):
        if not isinstance(raw, dict):
            continue
        option: dict[str, object] = {"index": index}
        if raw.get("label") not in (None, ""):
            option["label"] = raw.get("label")
        payload = raw.get("payload")
        if isinstance(payload, dict):
            for key in (
                "start_time_24h",
                "end_time_24h",
                "start_local",
                "doctor_name",
                "laser_device_name",
                "service_name",
            ):
                if payload.get(key) not in (None, ""):
                    option[key] = payload.get(key)
            appointment_ref = _entity_ref(
                payload.get("appointment_id"),
                kind="appointment",
                context=context,
            )
            if appointment_ref is not None:
                option["appointment_ref"] = appointment_ref
            device_ref = _entity_ref(
                payload.get("device_key") or payload.get("laser_device_key"),
                kind="device",
                context=context,
            )
            if device_ref is not None:
                option["device_ref"] = device_ref
        options.append(option)
    if options:
        safe["options"] = options
    return safe


def active_task_semantic_view(
    active_task: dict[str, Any] | None,
    *,
    context: SemanticContext,
) -> dict[str, object]:
    """Expose only ephemeral refs and conversational task facts to the interpreter."""
    if not isinstance(active_task, dict):
        return {}
    task_type = active_task.get("task_type")
    if task_type not in {"booking", "reschedule"}:
        return {}

    safe: dict[str, object] = {"task_type": task_type}
    if active_task.get("status") not in (None, ""):
        safe["status"] = active_task.get("status")

    if task_type == "booking":
        constraints = _safe_constraints(active_task.get("constraints"), context)
        if constraints:
            safe["constraints"] = constraints
        grouped = active_task.get("grouped")
        if isinstance(grouped, dict):
            raw_components = grouped.get("components")
            if isinstance(raw_components, list):
                components = [
                    _safe_constraints(component, context)
                    for component in raw_components
                    if isinstance(component, dict)
                ]
                components = [component for component in components if component]
                if len(components) >= 2:
                    safe["grouped_components"] = components
    else:
        target = _safe_target(active_task.get("target"), context)
        replacement = _safe_constraints(active_task.get("replacement"), context)
        if target:
            safe["target"] = target
        if replacement:
            safe["replacement"] = replacement

    snapshot = _safe_option_snapshot(
        active_task.get("option_snapshot"),
        context=context,
    )
    if snapshot:
        safe["pending_choice"] = snapshot
    return safe


def verified_read_semantic_view(
    read_context: dict[str, Any] | None,
    *,
    context: SemanticContext,
) -> dict[str, object]:
    """Expose only the immediately previous verified read scope and safe result summary."""
    if not isinstance(read_context, dict):
        return {}
    operation_type = read_context.get("operation_type")
    if not isinstance(operation_type, str) or not operation_type:
        return {}
    safe: dict[str, object] = {"operation_type": operation_type}
    safe.update(_safe_constraints(read_context, context))
    option_count = read_context.get("availability_option_count")
    if isinstance(option_count, int) and option_count >= 0:
        safe["availability_option_count"] = option_count
        safe["availability_found"] = option_count > 0
    return safe


def verified_action_semantic_view(
    action_context: dict[str, Any] | None,
    *,
    context: SemanticContext,
) -> dict[str, object]:
    """Expose a minimal immediately previous completed action scope."""
    if not isinstance(action_context, dict):
        return {}
    operation_type = action_context.get("operation_type")
    if operation_type not in {"buy_pulse_pack", "book", "cancel_appointment"}:
        return {}

    safe: dict[str, object] = {"operation_type": operation_type}
    if action_context.get("status") not in (None, ""):
        safe["status"] = action_context["status"]

    if operation_type == "cancel_appointment":
        appointment_ref = _entity_ref(
            action_context.get("appointment_id"),
            kind="appointment",
            context=context,
        )
        if appointment_ref is not None:
            safe["appointment_ref"] = appointment_ref
        return safe

    safe.update(_safe_constraints(action_context, context))
    if operation_type == "book":
        appointment_ref = _entity_ref(
            action_context.get("appointment_id"),
            kind="appointment",
            context=context,
        )
        if appointment_ref is not None:
            safe["appointment_ref"] = appointment_ref
        if action_context.get("start_at") not in (None, ""):
            safe["start_at"] = action_context["start_at"]
    return safe


def pending_choice_semantic_view(
    value: dict[str, Any] | None,
    *,
    context: SemanticContext,
) -> dict[str, object]:
    return _safe_option_snapshot(value, context=context)



_STATE_ID_KINDS: dict[str, ReferenceKind] = {
    "service_id": "service",
    "doctor_id": "doctor",
    "device_key": "device",
    "appointment_id": "appointment",
    "package_id": "package",
}


def _state_has_stale_refs(value: object, *, context: SemanticContext) -> bool:
    """Return True when persisted canonical state points outside the current catalog."""
    if isinstance(value, dict):
        for key, item in value.items():
            kind = _STATE_ID_KINDS.get(key)
            if kind is not None and item not in (None, ""):
                if _entity_ref(item, kind=kind, context=context) is None:
                    return True
            if key == "doctor_ids" and isinstance(item, list):
                reverse = _reverse_refs(context, "doctor")
                if any(str(doctor_id) not in reverse for doctor_id in item):
                    return True
            if _state_has_stale_refs(item, context=context):
                return True
    elif isinstance(value, list):
        return any(_state_has_stale_refs(item, context=context) for item in value)
    return False


def _collect_verified_refs(value: object, *, context: SemanticContext) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_ref") and isinstance(item, str) and item in context.reference_map:
                refs.add(item)
                continue
            if key.endswith("_refs") and isinstance(item, list):
                refs.update(
                    str(ref)
                    for ref in item
                    if isinstance(ref, str) and ref in context.reference_map
                )
                continue
            refs.update(_collect_verified_refs(item, context=context))
    elif isinstance(value, list):
        for item in value:
            refs.update(_collect_verified_refs(item, context=context))
    return refs


def _apply_verified_focus(
    context: SemanticContext,
    model_input: dict[str, object],
    *,
    block_focus: bool = False,
) -> SemanticContext:
    """Keep broad identity discovery while focusing verified operational detail."""
    metadata = dict(context.server_metadata)
    blocked = bool(metadata.get("semantic_focus_blocked")) or block_focus
    metadata["semantic_focus_blocked"] = blocked

    broad_appointments = list(metadata.get("broad_appointments") or ())
    broad_packages = list(metadata.get("broad_packages") or ())
    if blocked:
        model_input["appointments"] = broad_appointments
        model_input["packages"] = broad_packages
        model_input["focused_context"] = {}
        return SemanticContext(
            model_input=model_input,
            reference_map=context.reference_map,
            server_metadata=metadata,
        )

    refs: set[str] = set()
    for key in (
        "active_task",
        "pending_choice",
        "recent_verified_read",
        "recent_verified_action",
    ):
        refs.update(_collect_verified_refs(model_input.get(key), context=context))

    if not refs:
        model_input["appointments"] = broad_appointments
        model_input["packages"] = broad_packages
        model_input["focused_context"] = {}
        return SemanticContext(
            model_input=model_input,
            reference_map=context.reference_map,
            server_metadata=metadata,
        )

    raw_details = metadata.get("focus_details")
    details = raw_details if isinstance(raw_details, dict) else {}
    grouped: dict[str, list[dict[str, object]]] = {}
    appointment_refs: set[str] = set()
    package_refs: set[str] = set()
    for ref in sorted(refs):
        target = context.reference_map.get(ref)
        detail = details.get(ref)
        if target is None or not isinstance(detail, dict):
            continue
        grouped.setdefault(f"{target.kind}s", []).append(dict(detail))
        if target.kind == "appointment":
            appointment_refs.add(ref)
        elif target.kind == "package":
            package_refs.add(ref)

    model_input["appointments"] = [
        row for row in broad_appointments
        if isinstance(row, dict) and row.get("ref") in appointment_refs
    ]
    model_input["packages"] = [
        row for row in broad_packages
        if isinstance(row, dict) and row.get("ref") in package_refs
    ]
    model_input["focused_context"] = grouped
    return SemanticContext(
        model_input=model_input,
        reference_map=context.reference_map,
        server_metadata=metadata,
    )


def with_safe_task_context(
    context: SemanticContext,
    *,
    active_task: dict[str, Any] | None = None,
    pending_choice: dict[str, Any] | None = None,
) -> SemanticContext:
    model_input = dict(context.model_input)
    model_input["active_task"] = active_task_semantic_view(active_task, context=context)
    model_input["pending_choice"] = pending_choice_semantic_view(
        pending_choice,
        context=context,
    )
    stale = _state_has_stale_refs(active_task, context=context) or _state_has_stale_refs(
        pending_choice,
        context=context,
    )
    return _apply_verified_focus(context, model_input, block_focus=stale)

def with_safe_read_context(
    context: SemanticContext,
    *,
    read_context: dict[str, Any] | None = None,
) -> SemanticContext:
    if read_context is None:
        return context
    model_input = dict(context.model_input)
    model_input["recent_verified_read"] = verified_read_semantic_view(
        read_context,
        context=context,
    )
    stale = _state_has_stale_refs(read_context, context=context)
    return _apply_verified_focus(context, model_input, block_focus=stale)


def with_safe_action_context(
    context: SemanticContext,
    *,
    action_context: dict[str, Any] | None = None,
) -> SemanticContext:
    if action_context is None:
        return context
    model_input = dict(context.model_input)
    model_input["recent_verified_action"] = verified_action_semantic_view(
        action_context,
        context=context,
    )
    stale = _state_has_stale_refs(action_context, context=context)
    return _apply_verified_focus(context, model_input, block_focus=stale)
