from __future__ import annotations

from datetime import datetime, time
from typing import Literal

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import TurnOperation
from app.services.agent_v2.planner import PlanStep, ReadRequest, WriteIntent
from app.services.agent_v2.state import ActiveTaskState, BookingTaskState, RescheduleTaskState


def _canonical_ref(
    operation: TurnOperation,
    *,
    field: str,
    kind: str,
    context: SemanticContext,
) -> str | None:
    entity = getattr(operation.entities, field)
    if entity is None or entity.ref is None:
        return None
    return context.resolve(entity.ref, expected_kind=kind)


def resolved_operation_parameters(
    operation: TurnOperation,
    *,
    context: SemanticContext,
) -> dict[str, object]:
    """Resolve only structured ephemeral refs; never inspect customer text."""
    params: dict[str, object] = {}
    for field, kind, key in (
        ("service", "service", "service_id"),
        ("doctor", "doctor", "doctor_id"),
        ("device", "device", "device_key"),
        ("appointment", "appointment", "appointment_id"),
        ("package", "package", "package_id"),
    ):
        value = _canonical_ref(operation, field=field, kind=kind, context=context)
        if value is not None:
            params[key] = value
    if operation.entities.date is not None:
        params["date"] = operation.entities.date.model_dump(mode="json")
    if operation.entities.time is not None:
        params["time"] = operation.entities.time.model_dump(mode="json")
    if operation.entities.package_sessions is not None:
        params["package_sessions"] = operation.entities.package_sessions
    if operation.package_usage != "unspecified":
        params["package_usage"] = operation.package_usage
    return params


def _drop_implicit_identity_refs(
    params: dict[str, object],
    *,
    operation: TurnOperation,
) -> dict[str, object]:
    """Keep context-only refs from overwriting a verified active-task identity."""
    cleaned = dict(params)
    for field, key in (
        ("service", "service_id"),
        ("doctor", "doctor_id"),
        ("device", "device_key"),
    ):
        entity = getattr(operation.entities, field)
        if entity is not None and entity.ref is not None and not entity.text:
            cleaned.pop(key, None)
    return cleaned


def _canonical_ref_for_identity(
    context: SemanticContext,
    *,
    kind: str,
    canonical_id: str,
) -> str | None:
    return next(
        (
            ref
            for ref, target in context.reference_map.items()
            if target.kind == kind and target.canonical_id == canonical_id
        ),
        None,
    )


def _identity_compatible_with_service(
    context: SemanticContext,
    *,
    service_id: str,
    identity_kind: str,
    identity_id: str,
) -> bool | None:
    """Return False only when current server metadata proves incompatibility."""
    service_ref = _canonical_ref_for_identity(
        context,
        kind="service",
        canonical_id=service_id,
    )
    identity_ref = _canonical_ref_for_identity(
        context,
        kind=identity_kind,
        canonical_id=identity_id,
    )
    if service_ref is None or identity_ref is None:
        return None

    service_target = context.reference_map.get(service_ref)
    if (
        identity_kind == "device"
        and service_target is not None
        and service_target.metadata.get("requires_laser_device") is False
    ):
        return False

    raw_focus = context.server_metadata.get("focus_details")
    focus = raw_focus if isinstance(raw_focus, dict) else {}
    relationship_key = "doctor_refs" if identity_kind == "doctor" else "device_refs"

    service_detail = focus.get(service_ref)
    if isinstance(service_detail, dict) and relationship_key in service_detail:
        refs = service_detail.get(relationship_key)
        if isinstance(refs, list):
            return identity_ref in refs

    identity_detail = focus.get(identity_ref)
    if isinstance(identity_detail, dict) and "service_refs" in identity_detail:
        refs = identity_detail.get("service_refs")
        if isinstance(refs, list):
            return service_ref in refs
    return None


def _invalidate_incompatible_booking_identities(
    params: dict[str, object],
    *,
    active_task: BookingTaskState,
    context: SemanticContext,
) -> dict[str, object]:
    """Clear only server-proven invalid doctor/device dependencies after service replacement."""
    new_service = params.get("service_id")
    if not isinstance(new_service, str) or new_service == active_task.constraints.service_id:
        return params

    cleaned = dict(params)
    for key, kind, existing in (
        ("doctor_id", "doctor", active_task.constraints.doctor_id),
        ("device_key", "device", active_task.constraints.device_key),
    ):
        candidate = cleaned.get(key) if key in cleaned else existing
        if not isinstance(candidate, str):
            continue
        compatible = _identity_compatible_with_service(
            context,
            service_id=new_service,
            identity_kind=kind,
            identity_id=candidate,
        )
        if compatible is False:
            cleaned[key] = None
    return cleaned


def _past_temporal_resume_updates(
    active_task: BookingTaskState,
    *,
    now: datetime | None,
) -> dict[str, object]:
    """Drop only temporal constraints that can no longer describe a future booking."""
    if now is None:
        return {}

    date_constraint = active_task.constraints.date
    time_constraint = active_task.constraints.time
    updates: dict[str, object] = {}
    today = now.date()

    if date_constraint is not None:
        start_date = (
            datetime.fromisoformat(date_constraint.start_date).date()
            if date_constraint.start_date
            else None
        )
        end_date = (
            datetime.fromisoformat(date_constraint.end_date).date()
            if date_constraint.end_date
            else None
        )
        fully_past = (
            date_constraint.mode == "exact"
            and start_date is not None
            and start_date < today
        ) or (
            date_constraint.mode == "range"
            and end_date is not None
            and end_date < today
        )
        if fully_past:
            return {"date": None, "time": None}

        if (
            date_constraint.mode == "exact"
            and start_date == today
            and time_constraint is not None
        ):
            now_time = now.timetz().replace(tzinfo=None)
            start = (
                time.fromisoformat(time_constraint.start_time)
                if time_constraint.start_time
                else None
            )
            end = (
                time.fromisoformat(time_constraint.end_time)
                if time_constraint.end_time
                else None
            )
            time_fully_past = (
                time_constraint.mode in {"exact", "before"}
                and start is not None
                and start <= now_time
            ) or (
                time_constraint.mode == "range"
                and end is not None
                and end <= now_time
            ) or (
                time_constraint.mode == "nearest"
                and start is not None
                and start <= now_time
            )
            if time_fully_past:
                updates["time"] = None
    return updates


def _booking_followup_parameters(
    operation: TurnOperation,
    *,
    active_task: BookingTaskState,
    context: SemanticContext,
    now: datetime | None = None,
) -> dict[str, object]:
    params = _drop_implicit_identity_refs(
        resolved_operation_parameters(operation, context=context),
        operation=operation,
    )
    params = {
        **_past_temporal_resume_updates(active_task, now=now),
        **params,
    }
    return _invalidate_incompatible_booking_identities(
        params,
        active_task=active_task,
        context=context,
    )


ActiveTaskLifecycle = Literal["preserve", "continue", "replace"]


def classify_active_task_lifecycle(
    operation: TurnOperation,
    *,
    active_task: ActiveTaskState | None,
) -> ActiveTaskLifecycle:
    """Classify task-local lifecycle from typed semantics, never customer text."""
    if active_task is None or operation.execution_intent != "execute":
        return "preserve"

    requested_task_type = (
        "booking"
        if operation.type == "book"
        else "reschedule"
        if operation.type == "reschedule"
        else None
    )
    if requested_task_type is None:
        return "preserve"

    # A booking cannot continue a reschedule and vice versa. This deterministic
    # boundary prevents a stale task from blocking a clearly different primary goal.
    if requested_task_type != active_task.task_type:
        return "replace"

    if operation.fresh_task or operation.active_task_relationship == "replace":
        return "replace"
    return "continue"


def adapt_matching_active_task_step(
    step: PlanStep,
    *,
    operation: TurnOperation,
    active_task: ActiveTaskState | None,
    context: SemanticContext,
    now: datetime | None = None,
) -> PlanStep:
    """Merge continuations or mark an explicit fresh task for safe replacement."""
    lifecycle = classify_active_task_lifecycle(operation, active_task=active_task)
    if lifecycle == "replace":
        params = resolved_operation_parameters(operation, context=context)
        return step.model_copy(
            update={
                "state_action": "replace_active",
                "facts": {**step.facts, **params, "fresh_task_started": True},
            }
        )
    if isinstance(active_task, BookingTaskState):
        if active_task.grouped is not None and operation.type in {"continue_active", "book"}:
            return step.model_copy(update={"state_action": "none"})
        if operation.type == "continue_active":
            params = _booking_followup_parameters(
                operation,
                active_task=active_task,
                context=context,
                now=now,
            )
            return step.model_copy(update={"facts": params})
        if operation.type == "book":
            # A persisted booking task is the single authoritative in-progress booking.
            # The interpreter's continues_previous flag describes semantic read continuity
            # and is not reliable evidence that an active booking correction is a new task.
            # A genuinely separate booking starts only after the old task is completed or
            # explicitly cancelled, so any book operation while this state exists is a patch.
            params = _booking_followup_parameters(
                operation,
                active_task=active_task,
                context=context,
                now=now,
            )
            return PlanStep(
                operation_index=step.operation_index,
                operation_type=operation.type,
                disposition="state_update",
                state_action="update_active",
                response_goal="clarification",
                facts=params,
            )

    if not isinstance(active_task, RescheduleTaskState) or operation.type != "reschedule":
        return step
    if step.clarification_field == "appointment":
        return step

    appointment = operation.entities.appointment
    if appointment is not None and appointment.candidate_refs:
        return step

    params = resolved_operation_parameters(operation, context=context)
    explicit_target = params.pop("appointment_id", None)
    if explicit_target is not None and str(explicit_target) != active_task.target.appointment_id:
        return step

    # ``next_available`` is a search scope, not a concrete reschedule date.  If the
    # durable task still has no replacement date while an exact time is already
    # known, do not let a same-turn model default silently fill the missing date and
    # become write authority.  A legitimate next-available request without a
    # concrete time is still persisted and searched; after that earlier turn it is
    # valid inherited state, and a later exact time may continue normally.
    candidate_date = params.get("date")
    candidate_time = params.get("time")
    inherited_time = active_task.replacement.time
    effective_time_mode = (
        str(candidate_time.get("mode"))
        if isinstance(candidate_time, dict)
        else inherited_time.mode
        if inherited_time is not None
        else None
    )
    if (
        active_task.replacement.date is None
        and isinstance(candidate_date, dict)
        and candidate_date.get("mode") == "next_available"
        and effective_time_mode == "exact"
        and "date" not in set(operation.active_task_explicit_fields)
    ):
        params["date"] = None

    return PlanStep(
        operation_index=step.operation_index,
        operation_type=operation.type,
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
        facts=params,
    )


def persist_initial_task_intent(
    step: PlanStep,
    *,
    operation: TurnOperation,
    context: SemanticContext,
) -> PlanStep:
    """Keep an explicit booking intent alive even when the first turn needs clarification."""
    if step.disposition != "clarify" or step.state_action != "none":
        return step
    if operation.type != "book":
        return step
    params = resolved_operation_parameters(operation, context=context)
    facts = {**params, **step.facts}
    if operation.fresh_task:
        facts["fresh_task_started"] = True
    return step.model_copy(
        update={
            "state_action": "start_booking",
            "facts": facts,
        }
    )


def _constraint_parameters(state: BookingTaskState | RescheduleTaskState) -> dict[str, object]:
    constraints = state.constraints if state.task_type == "booking" else state.replacement
    dumped = constraints.model_dump(mode="json")
    dumped.pop("pulse_usage", None)
    return {
        key: value
        for key, value in dumped.items()
        if value not in (None, "", {}, [])
    }


def _service_requires_device(service_id: str | None, context: SemanticContext) -> bool:
    if service_id is None:
        return False
    target = next(
        (
            target
            for target in context.reference_map.values()
            if target.kind == "service" and target.canonical_id == service_id
        ),
        None,
    )
    if target is None:
        return False
    return target.metadata.get("requires_laser_device") is True


def _commercial_basis_key(state: BookingTaskState) -> str | None:
    service_id = state.constraints.service_id
    if service_id is None:
        return None
    path = "package" if state.constraints.package_usage == "use_existing" else "standalone"
    device = state.constraints.device_key or "none"
    return f"{service_id}|{device}|{path}"


def _booking_progress(
    state: BookingTaskState,
    *,
    operation_index: int,
    context: SemanticContext,
) -> PlanStep:
    params = _constraint_parameters(state)
    service_id = state.constraints.service_id
    if service_id is None:
        return PlanStep(
            operation_index=operation_index,
            operation_type="continue_active",
            disposition="clarify",
            state_action="update_active",
            response_goal="clarification",
            clarification_field="service",
            facts=params,
        )
    requires_device = _service_requires_device(service_id, context)
    if requires_device and state.constraints.device_key is None:
        return PlanStep(
            operation_index=operation_index,
            operation_type="continue_active",
            disposition="read",
            reads=[
                ReadRequest(
                    kind="service_catalog",
                    parameters={"service_id": service_id},
                )
            ],
            state_action="update_active",
            response_goal="answer_price",
            facts={
                **params,
                "service_requires_laser_device": True,
                "booking_device_price_step": True,
            },
        )
    commercial_key = _commercial_basis_key(state)
    commercial_presented = (
        commercial_key is not None
        and state.derived.commercial_basis_presented_key == commercial_key
    )
    # Backward-compatible laser tasks persisted by the first iteration already
    # proved the same standalone device price to the customer.
    if (
        not commercial_presented
        and requires_device
        and state.constraints.package_usage != "use_existing"
        and state.derived.commercial_basis_presented_device_key == state.constraints.device_key
    ):
        commercial_presented = True
    if not commercial_presented:
        commercial_read = ReadRequest(
            kind=(
                "customer_packages"
                if state.constraints.package_usage == "use_existing"
                else "service_catalog"
            ),
            parameters={
                key: params[key]
                for key in ("service_id", "device_key")
                if key in params
            },
        )
        reads = [commercial_read]
        if state.constraints.date is not None:
            reads.append(ReadRequest(kind="availability", parameters=params))
        return PlanStep(
            operation_index=operation_index,
            operation_type="continue_active",
            disposition="read",
            reads=reads,
            state_action="update_active",
            response_goal=(
                "package_information"
                if state.constraints.package_usage == "use_existing"
                else "answer_price"
            ),
            facts={
                **params,
                "service_requires_laser_device": requires_device,
                "booking_device_price_step": requires_device,
                "booking_commercial_basis_step": True,
                "commercial_basis_key": commercial_key,
                "exact_time_requested": (
                    state.constraints.time is not None
                    and state.constraints.time.mode == "exact"
                ),
                "booking_next_field": "date" if state.constraints.date is None else "booking",
            },
        )
    if state.constraints.date is None:
        return PlanStep(
            operation_index=operation_index,
            operation_type="continue_active",
            disposition="clarify",
            state_action="update_active",
            response_goal="clarification",
            clarification_field="date",
            facts=params,
        )

    exact_time = state.constraints.time is not None and state.constraints.time.mode == "exact"
    return PlanStep(
        operation_index=operation_index,
        operation_type="book",
        disposition="read",
        reads=[ReadRequest(kind="availability", parameters=params)],
        write_intent=WriteIntent(
            kind="booking",
            authorized=state.write_authorization.authorized,
            parameters=params,
            requires_verification=True,
        ),
        state_action="update_active",
        response_goal="present_availability",
        facts={
            **params,
            "exact_time_requested": exact_time,
            "service_requires_laser_device": requires_device,
        },
    )


def _reschedule_progress(
    state: RescheduleTaskState,
    *,
    operation_index: int,
    context: SemanticContext,
) -> PlanStep:
    params = _constraint_parameters(state)
    params["appointment_id"] = state.target.appointment_id
    if state.replacement.service_id is None:
        return PlanStep(
            operation_index=operation_index,
            operation_type="continue_active",
            disposition="clarify",
            state_action="update_active",
            response_goal="clarification",
            clarification_field="service",
            facts=params,
        )
    if state.replacement.date is None:
        return PlanStep(
            operation_index=operation_index,
            operation_type="continue_active",
            disposition="clarify",
            state_action="update_active",
            response_goal="clarification",
            clarification_field="date",
            facts=params,
        )

    exact_time = state.replacement.time is not None and state.replacement.time.mode == "exact"
    requires_device = _service_requires_device(state.replacement.service_id, context)
    read_params = {**params, "reschedule": True}
    return PlanStep(
        operation_index=operation_index,
        operation_type="reschedule",
        disposition="read",
        reads=[
            ReadRequest(kind="appointments", parameters={"appointment_id": state.target.appointment_id}),
            ReadRequest(kind="availability", parameters=read_params),
        ],
        write_intent=WriteIntent(
            kind="reschedule",
            authorized=state.write_authorization.authorized,
            parameters=params,
            requires_verification=True,
        ),
        state_action="update_active",
        response_goal="present_availability",
        facts={
            **params,
            "exact_time_requested": exact_time,
            "service_requires_laser_device": requires_device,
        },
    )


def plan_active_task_progress(
    state: ActiveTaskState,
    *,
    operation_index: int,
    context: SemanticContext,
) -> PlanStep:
    """Plan the next deterministic workflow step from persisted state only."""
    if state.task_type == "booking":
        return _booking_progress(state, operation_index=operation_index, context=context)
    return _reschedule_progress(state, operation_index=operation_index, context=context)
