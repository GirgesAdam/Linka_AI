from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.agent_action import AgentAction
from app.models.conversation_flow_event import ConversationFlowEvent
from app.models.conversation_flow_state import ConversationFlowState


class FlowStateConflictError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def flow_ttl_hours(flow_type: str) -> int:
    """Return the authority lifetime for a persisted conversational flow."""
    if flow_type == "booking":
        return settings.agent_booking_flow_ttl_hours
    return settings.agent_flow_ttl_hours


def _add_event(
    db: Session,
    flow: ConversationFlowState,
    *,
    event_type: str,
    actor_type: str,
    run_id: UUID | None,
    metadata: dict | None = None,
) -> None:
    db.add(
        ConversationFlowEvent(
            workspace_id=flow.workspace_id,
            flow_state_id=flow.id,
            conversation_id=flow.conversation_id,
            run_id=run_id,
            event_type=event_type,
            actor_type=actor_type,
            state_version=flow.version,
            metadata_json=metadata or {},
        )
    )
    db.flush()


def get_active_flow(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    patient_id: UUID | None = None,
    run_id: UUID | None = None,
) -> ConversationFlowState | None:
    stmt = select(ConversationFlowState).where(
        ConversationFlowState.workspace_id == workspace_id,
        ConversationFlowState.conversation_id == conversation_id,
        ConversationFlowState.is_active.is_(True),
    )
    if patient_id is not None:
        stmt = stmt.where(ConversationFlowState.patient_id == patient_id)

    flow = db.scalar(stmt.order_by(ConversationFlowState.created_at.desc()).limit(1))
    if flow is None:
        return None

    now = _now()
    if flow.expires_at > now:
        return flow

    expected_version = flow.version
    result = db.execute(
        update(ConversationFlowState)
        .where(
            ConversationFlowState.id == flow.id,
            ConversationFlowState.workspace_id == workspace_id,
            ConversationFlowState.version == expected_version,
            ConversationFlowState.is_active.is_(True),
        )
        .values(
            is_active=False,
            status="expired",
            version=expected_version + 1,
            last_turn_at=now,
        )
    )
    if result.rowcount == 1:
        db.flush()
        db.refresh(flow)
        _add_event(
            db,
            flow,
            event_type="expired",
            actor_type="system",
            run_id=run_id,
            metadata={"reason": "flow_ttl_elapsed"},
        )
    return None


def start_flow(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    patient_id: UUID,
    flow_type: str,
    capabilities: list[str],
    entity_state: dict,
    missing_information: list[str],
    last_decision: dict,
    run_id: UUID,
) -> ConversationFlowState:
    current = get_active_flow(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        patient_id=patient_id,
        run_id=run_id,
    )
    if current is not None:
        if current.flow_type == flow_type:
            return transition_flow(
                db,
                current,
                actor_type="router",
                event_type="updated",
                run_id=run_id,
                capabilities=sorted(set(current.capabilities) | set(capabilities)),
                entity_state={**(current.entity_state or {}), **entity_state},
                missing_information=missing_information,
                last_decision=last_decision,
            )
        interrupt_flow(
            db,
            current,
            run_id=run_id,
            reason="superseded_by_new_flow",
        )

    now = _now()
    flow = ConversationFlowState(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        patient_id=patient_id,
        flow_type=flow_type,
        status="collecting_requirements",
        is_active=True,
        capabilities=sorted(set(capabilities)),
        entity_state=entity_state,
        missing_information=missing_information,
        pending_action={},
        option_snapshot={},
        last_decision=last_decision,
        version=1,
        expires_at=now + timedelta(hours=flow_ttl_hours(flow_type)),
        last_turn_at=now,
    )
    db.add(flow)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        existing = get_active_flow(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            patient_id=patient_id,
            run_id=run_id,
        )
        if existing is not None:
            return existing
        raise FlowStateConflictError(
            "Another turn created an active conversation flow concurrently."
        ) from exc

    _add_event(
        db,
        flow,
        event_type="started",
        actor_type="router",
        run_id=run_id,
        metadata={
            "flow_type": flow_type,
            "capabilities": flow.capabilities,
        },
    )
    return flow


def transition_flow(
    db: Session,
    flow: ConversationFlowState,
    *,
    actor_type: str,
    event_type: str = "updated",
    run_id: UUID | None,
    **changes,
) -> ConversationFlowState:
    expected_version = flow.version
    now = _now()
    values = {
        **changes,
        "version": expected_version + 1,
        "last_turn_at": now,
        "expires_at": now + timedelta(hours=flow_ttl_hours(flow.flow_type)),
    }

    result = db.execute(
        update(ConversationFlowState)
        .where(
            ConversationFlowState.id == flow.id,
            ConversationFlowState.workspace_id == flow.workspace_id,
            ConversationFlowState.version == expected_version,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        db.rollback()
        raise FlowStateConflictError(
            "Conversation flow changed while this turn was being processed."
        )

    db.flush()
    db.expire(flow)
    db.refresh(flow)
    _add_event(
        db,
        flow,
        event_type=event_type,
        actor_type=actor_type,
        run_id=run_id,
        metadata={"changed_fields": sorted(changes)},
    )
    return flow


def interrupt_flow(
    db: Session,
    flow: ConversationFlowState,
    *,
    run_id: UUID | None,
    reason: str,
) -> ConversationFlowState:
    now = _now()
    return transition_flow(
        db,
        flow,
        actor_type="flow_interpreter",
        event_type="interrupted",
        run_id=run_id,
        status="interrupted",
        is_active=False,
        interrupted_at=now,
        pending_action={"reason": reason},
    )


def cancel_flow(
    db: Session,
    flow: ConversationFlowState,
    *,
    run_id: UUID | None,
    reason: str,
) -> ConversationFlowState:
    return transition_flow(
        db,
        flow,
        actor_type="flow_interpreter",
        event_type="cancelled",
        run_id=run_id,
        status="cancelled",
        is_active=False,
        pending_action={"reason": reason},
    )


def complete_flow(
    db: Session,
    flow: ConversationFlowState,
    *,
    run_id: UUID | None,
    result: dict,
) -> ConversationFlowState:
    now = _now()
    return transition_flow(
        db,
        flow,
        actor_type="tool",
        event_type="completed",
        run_id=run_id,
        status="completed",
        is_active=False,
        completed_at=now,
        pending_action=result,
    )


def record_write_authorized(
    db: Session,
    flow: ConversationFlowState,
    *,
    run_id: UUID,
    tool_name: str,
) -> None:
    _add_event(
        db,
        flow,
        event_type="write_authorized",
        actor_type="system",
        run_id=run_id,
        metadata={"tool_name": tool_name},
    )


def record_write_completed(
    db: Session,
    flow: ConversationFlowState,
    *,
    run_id: UUID,
    tool_name: str,
    result: dict,
) -> None:
    _add_event(
        db,
        flow,
        event_type="write_completed",
        actor_type="tool",
        run_id=run_id,
        metadata={
            "tool_name": tool_name,
            "result": result,
        },
    )


def _action_for_run(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    run_id: UUID,
    tool_name: str,
) -> AgentAction | None:
    return db.scalar(
        select(AgentAction)
        .where(
            AgentAction.workspace_id == workspace_id,
            AgentAction.conversation_id == conversation_id,
            AgentAction.run_id == run_id,
            AgentAction.status == "success",
            AgentAction.tool_name == tool_name,
        )
        .order_by(AgentAction.created_at.desc())
        .limit(1)
    )


def sync_flow_from_agent_run(
    db: Session,
    *,
    flow: ConversationFlowState | None,
    workspace_id: UUID,
    conversation_id: UUID,
    run_id: UUID,
) -> ConversationFlowState | None:
    if flow is None or not flow.is_active:
        return flow

    if flow.flow_type == "booking":
        discovery = _action_for_run(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            run_id=run_id,
            tool_name="get_booking_options",
        )
        write = _action_for_run(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            run_id=run_id,
            tool_name="book_appointment",
        )
    else:
        discovery = _action_for_run(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            run_id=run_id,
            tool_name="get_reschedule_options",
        )
        write = _action_for_run(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            run_id=run_id,
            tool_name="reschedule_appointment",
        )

    if write is not None and isinstance(write.output_json, dict):
        record_write_completed(
            db,
            flow,
            run_id=run_id,
            tool_name=write.tool_name,
            result=write.output_json,
        )
        return complete_flow(
            db,
            flow,
            run_id=run_id,
            result={"tool": write.tool_name, "output": write.output_json},
        )

    if discovery is None or not isinstance(discovery.output_json, dict):
        return flow

    output = discovery.output_json
    slots = output.get("slots")
    has_slots = isinstance(slots, list) and bool(slots)
    needs_choice = any(
        bool(output.get(key))
        for key in (
            "needs_service_choice",
            "needs_branch_choice",
            "needs_doctor_choice",
            "needs_appointment_choice",
        )
    )
    has_presented_options = has_slots or needs_choice
    status = "awaiting_option_selection" if has_presented_options else "collecting_requirements"

    entity_state = dict(flow.entity_state or {})
    for key in ("service", "branch", "current_appointment", "date", "requested_time_window"):
        if key in output:
            entity_state[key] = output[key]

    return transition_flow(
        db,
        flow,
        actor_type="agent",
        event_type="options_presented" if has_presented_options else "updated",
        run_id=run_id,
        status=status,
        option_snapshot=output if has_presented_options else {},
        entity_state=entity_state,
    )
