from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import BaseMessage

from app.agents.v2.responder import compose_v2_customer_reply
from app.agents.v2.semantic_context import SemanticContext, build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_task_context
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnOperation
from app.agents.v2.turn_interpreter import interpret_customer_turn_v2
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    persist_initial_task_intent,
    plan_active_task_progress,
)
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
from app.services.agent_v2.state import ActiveTaskState
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


@dataclass(frozen=True)
class V2StatefulHarnessResult:
    understanding: TiaTurnUnderstanding
    plan: TurnPlan
    traces: tuple[V2HarnessStepTrace, ...]
    outcomes: tuple[TurnOutcome, ...]
    reply: str
    responder_model: str
    active_task: ActiveTaskState | None


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
    understanding = interpret_customer_turn_v2(
        history=history,
        semantic_context=semantic_context,
        timezone_name=timezone_name,
        local_now=local_now,
    )
    plan = plan_turn(
        understanding,
        PlannerContext(
            semantic_context=semantic_context,
            active_task=active_task,
            now=local_now,
        ),
    )
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
        )

    current_task = active_task
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
    return V2StatefulHarnessResult(
        understanding=understanding,
        plan=plan,
        traces=tuple(traces),
        outcomes=tuple(outcomes),
        reply=reply,
        responder_model=model,
        active_task=current_task,
    )
