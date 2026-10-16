from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from app.agents.v2.availability_pagination import (
    availability_windows_from_outcome_facts,
    select_availability_window_page,
)
from app.agents.v2.availability_reference_interpreter import (
    interpret_availability_reference_turn,
)
from app.agents.v2.responder import compose_v2_customer_reply
from app.agents.v2.semantic_context import SemanticContext, build_semantic_context
from app.agents.v2.semantic_state_view import (
    with_safe_availability_reference_context,
    with_safe_read_context,
    with_safe_task_context,
)
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.agents.v2.turn_interpreter import interpret_customer_turn_v2
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    persist_initial_task_intent,
    plan_active_task_progress,
    resolve_pending_bare_clock_period,
)
from app.services.agent_v2.compound_turn_policy import normalize_compound_turn_plan
from app.services.agent_v2.grouped_booking_continuity import (
    explicitly_narrow_grouped_booking_task,
    restore_grouped_booking_understanding,
)
from app.services.agent_v2.orchestrator import _grouped_visit_reference_understanding
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import build_handoff_outcome, build_step_outcome
from app.services.agent_v2.planner import (
    PlannerContext,
    PlanStep,
    TurnPlan,
    advance_step_after_verification,
    plan_turn,
)
from app.services.agent_v2.read_executor import ReadExecutionBundle
from app.services.agent_v2.reference_resolution import build_availability_reference_options
from app.services.agent_v2.state import ActiveTaskState, BookingTaskState
from app.services.agent_v2.state_executor import (
    apply_step_state,
    complete_state_after_action,
    finalize_step_after_state_transition,
)
from app.services.agent_v2.test_harness import (
    V2FixtureEnvironment,
    V2HarnessStepTrace,
    execute_fixture_reads,
)
from app.services.agent_v2.turn_normalization import (
    collapse_booking_service_sets,
    expand_multi_service_operations,
    guard_multiple_booking_writes,
    merge_same_turn_grouped_availability_operations,
    merge_same_visit_booking_operations,
    recollapse_restored_booking_visit_groups,
    tag_booking_service_sets,
)


@dataclass(frozen=True)
class V2StatefulHarnessResult:
    understanding: TiaTurnUnderstanding
    plan: TurnPlan
    traces: tuple[V2HarnessStepTrace, ...]
    outcomes: tuple[TurnOutcome, ...]
    reply: str
    responder_model: str
    active_task: ActiveTaskState | None
    recent_read_context: dict[str, Any] | None = None
    availability_reference_context: dict[str, Any] | None = None
    reference_semantic_path_used: bool = False
    reference_action: str | None = None
    selected_option_ref: str | None = None
    reference_exact_time: str | None = None
    full_interpreter_called: bool = True
    reference_structured_output_error: bool = False



def _fixture_availability_context(
    *,
    plan: TurnPlan,
    traces: list[V2HarnessStepTrace],
    fixture: V2FixtureEnvironment,
    active_task: ActiveTaskState | None,
) -> dict[str, Any] | None:
    requested = (
        list(dict.fromkeys(active_task.requested_service_ids))
        if isinstance(active_task, BookingTaskState)
        else []
    )
    canonical_grouped_requested: list[str] = []
    components: list[tuple[str, dict[str, object], list[dict[str, object]], list[str]]] = []
    for trace in traces:
        if trace.outcome.response_goal != "present_availability":
            continue
        step = next(
            (item for item in plan.steps if item.operation_index == trace.operation_index),
            None,
        )
        if step is None:
            continue
        step_requested = step.facts.get("requested_service_ids")
        if (
            step.facts.get("multi_service_single_appointment") is True
            and isinstance(step_requested, list)
        ):
            canonical_requested = list(
                dict.fromkeys(
                    str(item) for item in step_requested if item not in (None, "")
                )
            )
            if len(canonical_requested) > 1 and set(canonical_requested) == set(requested):
                canonical_grouped_requested = canonical_requested
        availability_read = next(
            (request for request in step.reads if request.kind == "availability"),
            None,
        )
        if availability_read is None:
            continue
        service_id = str(availability_read.parameters.get("service_id") or "")
        service_name, windows = availability_windows_from_outcome_facts(trace.outcome.facts)
        if not service_id or not windows:
            continue
        selected_windows, selected_keys, _ = select_availability_window_page(
            windows,
            service_name=service_name,
            shown_keys=set(),
        )
        bundle = execute_fixture_reads(step, fixture)
        verified_slots: list[dict[str, object]] = []
        for result in bundle.results:
            if result.kind != "availability":
                continue
            raw_slots = result.payload.get("slots")
            if isinstance(raw_slots, list):
                verified_slots.extend(dict(item) for item in raw_slots if isinstance(item, dict))
        options = build_availability_reference_options(
            displayed_windows=selected_windows,
            verified_slots=verified_slots,
        )
        if options:
            components.append((service_id, availability_read.parameters, options, selected_keys))
    if not components:
        return None

    if canonical_grouped_requested:
        selected_components = [
            item for item in components if item[0] in canonical_grouped_requested
        ]
        requested = canonical_grouped_requested
    elif len(requested) > 1 and {item[0] for item in components} >= set(requested):
        selected_components = [item for item in components if item[0] in requested]
    else:
        selected_components = [components[-1]]
        requested = []

    options: list[dict[str, object]] = []
    keys: set[str] = set()
    common_scope: dict[str, object] | None = None
    for service_id, params, component_options, selected_keys in selected_components:
        keys.update(selected_keys)
        scope = {
            key: value
            for key in ("branch_id", "date", "package_usage")
            if (value := params.get(key)) not in (None, "", [], {})
        }
        common_scope = scope if common_scope is None else {
            key: value
            for key, value in common_scope.items()
            if scope.get(key) == value
        }
        for option in component_options:
            item = dict(option)
            item["index"] = len(options) + 1
            item["option_ref"] = f"opt_{len(options) + 1}"
            item["service_id"] = service_id
            options.append(item)
    context: dict[str, Any] = {
        "operation_type": "availability",
        "availability_reference_options": options,
        "availability_option_count": len(options),
        **(common_scope or {}),
    }
    if requested:
        context["requested_service_ids"] = requested
        context["multi_service_single_appointment"] = True
    if keys:
        context["availability_presented_window_keys"] = sorted(keys)
    return context


def _task_dict(active_task: ActiveTaskState | None) -> dict[str, Any] | None:
    return active_task.model_dump(mode="json") if active_task is not None else None


def _advance(step: PlanStep, bundle: ReadExecutionBundle) -> PlanStep:
    if step.write_intent is None or not step.reads:
        return step
    return advance_step_after_verification(step, bundle.verification)


def _simulated_action(step: PlanStep, *, simulate_writes: bool) -> tuple[str | None, dict[str, object] | None]:
    if step.disposition != "write_ready":
        return None, None
    if not simulate_writes:
        raise RuntimeError(
            f"V2 stateful test reached write-ready operation {step.operation_type}; "
            "enable simulation or test the write executor separately."
        )
    kind = step.write_intent.kind if step.write_intent is not None else None
    return kind, {"ok": True}


def _operation_for_step(
    understanding: TiaTurnUnderstanding,
    step: PlanStep,
) -> TurnOperation:
    try:
        return understanding.operations[step.operation_index]
    except IndexError as exc:
        raise RuntimeError("V2 plan referenced an operation outside the structured turn.") from exc


def _execute_step(
    *,
    step: PlanStep,
    operation: TurnOperation,
    understanding: TiaTurnUnderstanding,
    semantic_context: SemanticContext,
    fixture: V2FixtureEnvironment,
    current_task: ActiveTaskState | None,
    local_now: datetime,
    turn_id: str,
    simulate_writes: bool,
) -> tuple[V2HarnessStepTrace, TurnOutcome, ActiveTaskState | None]:
    bundle = execute_fixture_reads(step, fixture) if step.reads else ReadExecutionBundle()
    advanced = _advance(step, bundle)
    transition = apply_step_state(
        current_task,
        step=advanced,
        operation=operation,
        reads=bundle if bundle.results else None,
        now=local_now,
        turn_id=turn_id,
        catalog=fixture.catalog,
    )
    advanced = finalize_step_after_state_transition(advanced, transition)
    task_after_transition = transition.active_task
    simulated_write, action_result = _simulated_action(
        advanced,
        simulate_writes=simulate_writes,
    )
    if action_result is not None:
        task_after_transition = complete_state_after_action(
            task_after_transition,
            step=advanced,
            action_result=action_result,
        ).active_task

    outcome = build_step_outcome(
        advanced,
        turn=understanding,
        semantic_context=semantic_context,
        reads=bundle if bundle.results else None,
        action_result=action_result,
        active_task_summary=None,
    )
    trace = V2HarnessStepTrace(
        operation_index=advanced.operation_index,
        operation_type=advanced.operation_type,
        disposition_before=step.disposition,
        disposition_after=advanced.disposition,
        read_kinds=tuple(result.kind for result in bundle.results),
        simulated_write=simulated_write,
        outcome=outcome,
    )
    return trace, outcome, task_after_transition


def run_v2_stateful_fixture_turn(
    *,
    history: list[BaseMessage],
    local_now: datetime,
    timezone_name: str = "Africa/Cairo",
    clinic_name: str = "Linka Test Clinic",
    env: V2FixtureEnvironment | None = None,
    active_task: ActiveTaskState | None = None,
    recent_read_context: dict[str, Any] | None = None,
    availability_reference_context: dict[str, Any] | None = None,
    simulate_writes: bool = True,
    turn_id: str | None = None,
) -> V2StatefulHarnessResult:
    """Run one real V2 semantic turn while persisting workflow state in memory only.

    The state is returned to the caller for the next turn. No database persistence, V1 code,
    clinic write adapter, production customer delivery, or real write action is used.
    """
    fixture = env or V2FixtureEnvironment()
    semantic_context: SemanticContext = build_semantic_context(fixture.catalog)
    semantic_context = with_safe_task_context(
        semantic_context,
        active_task=_task_dict(active_task),
    )
    semantic_context = with_safe_read_context(
        semantic_context,
        read_context=recent_read_context,
    )
    semantic_context = with_safe_availability_reference_context(
        semantic_context,
        availability_context=availability_reference_context,
    )
    latest_customer_text = next(
        (
            str(message.content).strip()
            for message in reversed(history)
            if isinstance(message, HumanMessage)
            and isinstance(message.content, str)
            and message.content.strip()
        ),
        "",
    )
    reference_interpretation = None
    full_interpreter_called = False
    grouped_reference_understanding = None
    grouped_reference_task = None
    if (
        latest_customer_text
        and isinstance(availability_reference_context, dict)
        and isinstance(
            availability_reference_context.get("availability_reference_options"), list
        )
    ):
        reference_interpretation = interpret_availability_reference_turn(
            latest_customer_text=latest_customer_text,
            availability_context=availability_reference_context,
        )
        grouped_reference = _grouped_visit_reference_understanding(
            active_task,
            decision=reference_interpretation.decision,
            availability_context=availability_reference_context,
            catalog=fixture.catalog,
        )
        if grouped_reference is not None:
            grouped_reference_understanding, grouped_reference_task = grouped_reference

    contextual_period_resolution = resolve_pending_bare_clock_period(
        active_task,
        latest_customer_text,
        context=semantic_context,
    )
    if contextual_period_resolution is not None:
        active_task, _resolved_period = contextual_period_resolution
        semantic_context = with_safe_task_context(
            semantic_context,
            active_task=_task_dict(active_task),
        )
        understanding = TiaTurnUnderstanding(
            operations=[
                TurnOperation(
                    type="continue_active",
                    entities=TurnEntities(),
                    execution_intent="execute",
                    active_task_relationship="continue",
                    continues_previous=True,
                )
            ]
        )
    elif grouped_reference_understanding is not None:
        understanding = grouped_reference_understanding
        active_task = grouped_reference_task
    else:
        full_interpreter_called = True
        understanding = interpret_customer_turn_v2(
            history=history,
            semantic_context=semantic_context,
            timezone_name=timezone_name,
            local_now=local_now,
        )
    reference_semantic_path_used = reference_interpretation is not None
    reference_action = (
        reference_interpretation.decision.action
        if reference_interpretation is not None
        else None
    )
    selected_option_ref = (
        reference_interpretation.decision.option_ref
        if reference_interpretation is not None
        else None
    )
    reference_exact_time = (
        reference_interpretation.decision.exact_time
        if reference_interpretation is not None
        else None
    )
    reference_structured_output_error = (
        reference_interpretation.structured_output_error
        if reference_interpretation is not None
        else False
    )
    understanding = merge_same_visit_booking_operations(understanding)
    understanding = merge_same_turn_grouped_availability_operations(
        understanding,
        semantic_context=semantic_context,
        catalog=fixture.catalog,
    )
    initial_booking_task = active_task if isinstance(active_task, BookingTaskState) else None
    understanding, booking_service_sets = collapse_booking_service_sets(
        understanding,
        semantic_context=semantic_context,
        catalog=fixture.catalog,
        active_task=initial_booking_task,
    )
    understanding, semantic_visit_groups = expand_multi_service_operations(
        understanding,
        semantic_context=semantic_context,
        booking_service_sets=booking_service_sets,
    )
    narrowed_booking_task = explicitly_narrow_grouped_booking_task(
        understanding,
        active_task=initial_booking_task,
        context=semantic_context,
        catalog=fixture.catalog,
        booking_service_sets=booking_service_sets,
    )
    understanding, restored_visit_groups = restore_grouped_booking_understanding(
        understanding,
        active_task=initial_booking_task,
        context=semantic_context,
        booking_service_sets=booking_service_sets,
    )
    if restored_visit_groups:
        understanding, restored_service_sets, restored_visit_groups = (
            recollapse_restored_booking_visit_groups(
                understanding,
                semantic_context=semantic_context,
                catalog=fixture.catalog,
                restored_visit_groups=restored_visit_groups,
            )
        )
        booking_service_sets = {**booking_service_sets, **restored_service_sets}
    semantic_visit_groups = {
        **semantic_visit_groups,
        **restored_visit_groups,
    }
    planner_active_task: ActiveTaskState | None = narrowed_booking_task or active_task
    plan = plan_turn(
        understanding,
        PlannerContext(
            semantic_context=semantic_context,
            active_task=planner_active_task,
            now=local_now,
        ),
    )
    plan = tag_booking_service_sets(plan, service_sets=booking_service_sets)
    plan = normalize_compound_turn_plan(
        plan,
        catalog=fixture.catalog,
        operation_visit_groups=semantic_visit_groups,
    )
    plan = guard_multiple_booking_writes(plan)
    resolved_turn_id = turn_id or f"fixture-turn-{len(history)}"

    if plan.handoff_category is not None:
        outcome = build_handoff_outcome(plan)
        reply, model = compose_v2_customer_reply(
            clinic_name=clinic_name,
            timezone_name=timezone_name,
            local_now=local_now,
            history=history,
            outcomes=[outcome],
        )
        return V2StatefulHarnessResult(
            understanding=understanding,
            plan=plan,
            traces=(),
            outcomes=(outcome,),
            reply=reply,
            responder_model=model,
            active_task=active_task,
            recent_read_context=recent_read_context,
            availability_reference_context=availability_reference_context,
            reference_semantic_path_used=reference_semantic_path_used,
            reference_action=reference_action,
            selected_option_ref=selected_option_ref,
            reference_exact_time=reference_exact_time,
            full_interpreter_called=full_interpreter_called,
            reference_structured_output_error=reference_structured_output_error,
        )

    current_task: ActiveTaskState | None = narrowed_booking_task or active_task
    traces: list[V2HarnessStepTrace] = []
    outcomes: list[TurnOutcome] = []

    for planned_step in plan.steps:
        operation = _operation_for_step(understanding, planned_step)
        effective_step = adapt_matching_active_task_step(
            planned_step,
            operation=operation,
            active_task=current_task,
            context=semantic_context,
        )
        effective_step = persist_initial_task_intent(
            effective_step,
            operation=operation,
            context=semantic_context,
        )

        if effective_step.state_action == "update_active":
            initial_transition = apply_step_state(
                current_task,
                step=effective_step,
                operation=operation,
                reads=None,
                now=local_now,
                turn_id=resolved_turn_id,
                catalog=fixture.catalog,
            )
            current_task = initial_transition.active_task
            if current_task is None:
                trace, outcome, current_task = _execute_step(
                    step=effective_step,
                    operation=operation,
                    understanding=understanding,
                    semantic_context=semantic_context,
                    fixture=fixture,
                    current_task=current_task,
                    local_now=local_now,
                    turn_id=resolved_turn_id,
                    simulate_writes=simulate_writes,
                )
            else:
                progress = plan_active_task_progress(
                    current_task,
                    operation_index=effective_step.operation_index,
                    context=semantic_context,
                )
                trace, outcome, current_task = _execute_step(
                    step=progress,
                    operation=operation,
                    understanding=understanding,
                    semantic_context=semantic_context,
                    fixture=fixture,
                    current_task=current_task,
                    local_now=local_now,
                    turn_id=resolved_turn_id,
                    simulate_writes=simulate_writes,
                )
        else:
            trace, outcome, current_task = _execute_step(
                step=effective_step,
                operation=operation,
                understanding=understanding,
                semantic_context=semantic_context,
                fixture=fixture,
                current_task=current_task,
                local_now=local_now,
                turn_id=resolved_turn_id,
                simulate_writes=simulate_writes,
            )

        traces.append(trace)
        outcomes.append(outcome)

    reply, model = compose_v2_customer_reply(
        clinic_name=clinic_name,
        timezone_name=timezone_name,
        local_now=local_now,
        history=history,
        outcomes=outcomes,
    )
    next_availability_context = _fixture_availability_context(
        plan=plan,
        traces=traces,
        fixture=fixture,
        active_task=current_task,
    )
    if next_availability_context is None:
        next_availability_context = availability_reference_context
    next_read_context = next_availability_context or recent_read_context
    return V2StatefulHarnessResult(
        understanding=understanding,
        plan=plan,
        traces=tuple(traces),
        outcomes=tuple(outcomes),
        reply=reply,
        responder_model=model,
        active_task=current_task,
        recent_read_context=next_read_context,
        availability_reference_context=next_availability_context,
        reference_semantic_path_used=reference_semantic_path_used,
        reference_action=reference_action,
        selected_option_ref=selected_option_ref,
        reference_exact_time=reference_exact_time,
        full_interpreter_called=full_interpreter_called,
        reference_structured_output_error=reference_structured_output_error,
    )
