from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.patient import Patient
from app.models.workspace import Workspace
from app.schemas.agent import AgentChatRequest, AgentChatResponse
from app.services.agent_chat import (
    AgentChatError,
    _existing_agent_response_for_inbound,
    _get_or_create_conversation,
    _history_from_db,
    _uuid_from_metadata,
    _workspace_clock,
)
from app.services.agent_chat import (
    run_agent_chat as run_agent_chat_v1,
)
from app.services.agent_chat import (
    run_agent_for_existing_inbound as run_agent_for_existing_inbound_v1,
)
from app.services.agent_v2.orchestrator import V2OrchestratedTurn, orchestrate_v2_turn
from app.services.agent_v2.write_executor import execute_write_ready_step
from app.services.conversation_ownership import (
    OWNER_HUMAN,
    agent_can_reply,
    lock_conversation_ownership,
    record_customer_inbound,
)
from app.services.handoffs import create_handoff, get_active_handoff


def _get_patient_v2(db: Session, *, workspace_id: UUID, patient_id: UUID) -> Patient:
    patient = db.scalar(
        select(Patient).where(
            Patient.workspace_id == workspace_id,
            Patient.id == patient_id,
        )
    )
    if patient is None:
        raise AgentChatError("Patient not found in this workspace.")
    return patient


def _handoff_reason(turn: V2OrchestratedTurn) -> str:
    for outcome in turn.outcomes:
        if outcome.status != "handoff":
            continue
        detail = outcome.action_result.get("detail")
        if detail:
            return str(detail)[:4000]
    return "human_handoff_requested"


def _v2_handoff_ack_allowed(handoff: object | None, *, created_this_turn: bool) -> bool:
    if not created_this_turn or handoff is None:
        return False
    return (
        getattr(handoff, "source", None) == "ai"
        and getattr(handoff, "status", None) == "pending"
        and getattr(handoff, "assigned_user_id", None) is None
    )


def _recent_verified_read_context(
    db: Session,
    *,
    conversation: Conversation,
    inbound: Message,
) -> dict[str, Any] | None:
    """Return context only from the immediately preceding message in this conversation."""
    previous = db.scalar(
        select(Message)
        .where(
            Message.workspace_id == conversation.workspace_id,
            Message.conversation_id == conversation.id,
            Message.created_at < inbound.created_at,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    if previous is None or previous.sender_type != "ai" or previous.direction != "outbound":
        return None
    metadata = dict(previous.metadata_json or {})
    if metadata.get("runtime") != "v2":
        return None
    value = metadata.get("v2_read_context")
    return dict(value) if isinstance(value, dict) else None


def _recent_verified_action_context_from_outbounds(
    messages: list[Message],
) -> dict[str, Any] | None:
    """Recover booking context only across explicitly safe V2 read-only hops."""
    for index, previous in enumerate(messages):
        if previous.sender_type != "ai" or previous.direction != "outbound":
            return None
        metadata = dict(previous.metadata_json or {})
        if metadata.get("runtime") != "v2":
            return None
        value = metadata.get("v2_action_context")
        if isinstance(value, dict):
            if index == 0:
                return dict(value)
            return dict(value) if value.get("operation_type") == "book" else None
        if metadata.get("v2_action_context_passthrough") is not True:
            return None
    return None


def _recent_verified_action_context(
    db: Session,
    *,
    conversation: Conversation,
    inbound: Message,
) -> dict[str, Any] | None:
    """Return recent safe completed-action context across bounded read-only detours."""
    previous_outbounds = list(
        db.scalars(
            select(Message)
            .where(
                Message.workspace_id == conversation.workspace_id,
                Message.conversation_id == conversation.id,
                Message.direction == "outbound",
                Message.created_at < inbound.created_at,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(8)
        ).all()
    )
    return _recent_verified_action_context_from_outbounds(previous_outbounds)


def _recent_pending_choice_context(
    db: Session,
    *,
    conversation: Conversation,
    inbound: Message,
) -> dict[str, Any] | None:
    """Return only the immediately preceding V2 verified appointment-choice snapshot."""
    previous = db.scalar(
        select(Message)
        .where(
            Message.workspace_id == conversation.workspace_id,
            Message.conversation_id == conversation.id,
            Message.created_at < inbound.created_at,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    if previous is None or previous.sender_type != "ai" or previous.direction != "outbound":
        return None
    metadata = dict(previous.metadata_json or {})
    if metadata.get("runtime") != "v2":
        return None
    value = metadata.get("v2_pending_choice")
    return dict(value) if isinstance(value, dict) else None


def _availability_option_count_for_step(
    turn: V2OrchestratedTurn,
    *,
    operation_index: int,
) -> int | None:
    """Read the verified availability count from the matching runtime outcome."""
    for trace in reversed(turn.traces):
        if trace.operation_index != operation_index or trace.outcome is None:
            continue
        availability = trace.outcome.facts.get("availability")
        if not isinstance(availability, dict):
            return None
        value = availability.get("available_option_count")
        return value if isinstance(value, int) and value >= 0 else None
    return None


def _verified_read_context_from_turn(
    db: Session,
    *,
    workspace: Workspace,
    turn: V2OrchestratedTurn,
) -> dict[str, Any] | None:
    """Keep only the last read-only canonical scope plus a minimal verified result summary."""
    for step in reversed(turn.plan.steps):
        if step.disposition != "read" or step.write_intent is not None or not step.reads:
            continue
        context: dict[str, Any] = {"operation_type": step.operation_type}
        for key in (
            "service_id",
            "doctor_id",
            "doctor_ids",
            "device_key",
            "date",
            "time",
            "package_usage",
        ):
            value = step.facts.get(key)
            if value not in (None, "", [], {}):
                context[key] = value

        option_count = _availability_option_count_for_step(
            turn,
            operation_index=step.operation_index,
        )
        if option_count is not None:
            context["availability_option_count"] = option_count
            if turn.availability_presented_window_keys:
                context["availability_presented_window_keys"] = list(
                    turn.availability_presented_window_keys
                )

        # A doctor-list read establishes a verified set even though the customer did
        # not enumerate every doctor. Recreate that exact set from canonical catalog
        # relationships so a later "which of them" comparison never depends on prose.
        if step.operation_type == "doctor_info" and context.get("service_id") and not (
            context.get("doctor_id") or context.get("doctor_ids")
        ):
            service_id = str(context["service_id"])
            doctors = build_clinic_catalog(db, workspace).get("doctors")
            if isinstance(doctors, list):
                ids = [
                    str(row["id"])
                    for row in doctors
                    if isinstance(row, dict)
                    and row.get("id")
                    and isinstance(row.get("service_ids"), list)
                    and service_id in {str(item) for item in row["service_ids"]}
                ]
                if ids:
                    context["doctor_ids"] = ids
        return context
    return None


def _verified_action_context_from_turn(
    turn: V2OrchestratedTurn,
) -> dict[str, Any] | None:
    """Persist only minimal canonical facts from the immediately completed action."""
    direct = getattr(turn, "verified_action_context", None)
    if isinstance(direct, dict) and direct:
        return dict(direct)

    # Backward-compatible fallback for older/in-memory turn objects used by tests.
    for trace in reversed(turn.traces):
        outcome = trace.outcome
        if (
            outcome is None
            or outcome.status != "completed"
            or outcome.action_result.get("ok") is not True
            or outcome.action_result.get("action") != "buy_pulse_pack"
        ):
            continue
        for step in reversed(turn.plan.steps):
            if step.operation_index != trace.operation_index or step.write_intent is None:
                continue
            if step.write_intent.kind != "buy_pulse_pack":
                continue
            parameters = step.write_intent.parameters
            device_key = parameters.get("device_key")
            pulse_count = parameters.get("pulse_count")
            if device_key in (None, ""):
                return None
            context: dict[str, Any] = {
                "operation_type": "buy_pulse_pack",
                "device_key": str(device_key),
            }
            if isinstance(pulse_count, int) and not isinstance(pulse_count, bool) and pulse_count > 0:
                context["pulse_count"] = pulse_count
            return context
    return None


def _safe_action_context_passthrough(turn: V2OrchestratedTurn) -> bool:
    """Mark only operationally read-only informational turns as safe context hops."""
    if turn.active_task is not None or turn.pending_write is not None:
        return False
    if not turn.plan.steps or not turn.understanding.operations:
        return False
    if any(
        step.write_intent is not None
        or step.state_action != "none"
        or step.disposition not in {"read", "respond"}
        for step in turn.plan.steps
    ):
        return False
    return all(
        operation.execution_intent == "informational"
        for operation in turn.understanding.operations
    )


def _outbound_verified_action_context(
    turn: V2OrchestratedTurn,
    *,
    recent_action_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Carry one completed booking through read-only informational detours only."""
    direct = _verified_action_context_from_turn(turn)
    if direct is not None:
        return direct
    if (
        not isinstance(recent_action_context, dict)
        or recent_action_context.get("operation_type") != "book"
        or not _safe_action_context_passthrough(turn)
    ):
        return None
    return dict(recent_action_context)


def _run_v2_after_inbound(
    *,
    db: Session,
    workspace: Workspace,
    patient: Patient,
    conversation: Conversation,
    inbound: Message,
    run_id: UUID,
    outbound_delivery_status: str,
    source: str,
) -> AgentChatResponse:
    active_handoff = get_active_handoff(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
    )
    if not agent_can_reply(conversation) or active_handoff is not None:
        return AgentChatResponse(
            run_id=run_id,
            conversation_id=conversation.id,
            inbound_message_id=inbound.id,
            outbound_message_id=None,
            reply=None,
            handoff_required=True,
            agent_paused=True,
            model=None,
        )

    history = _history_from_db(db, conversation)
    timezone_name, local_now = _workspace_clock(workspace)
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    recent_read_context = _recent_verified_read_context(
        db,
        conversation=conversation,
        inbound=inbound,
    )
    recent_action_context = _recent_verified_action_context(
        db,
        conversation=conversation,
        inbound=inbound,
    )
    pending_choice_context = _recent_pending_choice_context(
        db,
        conversation=conversation,
        inbound=inbound,
    )

    def live_write(step):
        return execute_write_ready_step(
            db,
            workspace=workspace,
            patient=patient,
            step=step,
            idempotency_key=f"v2:{inbound.id}:{step.operation_index}",
            commit=False,
        )

    turn = orchestrate_v2_turn(
        db=db,
        workspace=workspace,
        patient=patient,
        conversation_id=conversation.id,
        run_id=run_id,
        history=history,
        local_now=local_now,
        timezone_name=timezone_name,
        clinic_name=workspace.name,
        adapter=adapter,
        turn_id=str(inbound.id),
        write_executor=live_write,
        recent_read_context=recent_read_context,
        recent_action_context=recent_action_context,
        pending_choice_context=pending_choice_context,
    )
    if turn.pending_write is not None:
        raise RuntimeError("Live V2 turn returned an unexecuted verified write.")
    if not turn.reply:
        raise RuntimeError("Live V2 turn produced no customer reply.")

    created_handoff_this_turn = any(outcome.status == "handoff" for outcome in turn.outcomes)
    if created_handoff_this_turn:
        create_handoff(
            db,
            workspace_id=workspace.id,
            conversation=conversation,
            patient=patient,
            reason=_handoff_reason(turn),
            category=turn.plan.handoff_category or "other",
            priority=turn.plan.handoff_priority or "normal",
            source="ai",
            commit=False,
        )

    locked_conversation = lock_conversation_ownership(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
    )
    if locked_conversation is None:
        raise AgentChatError("Conversation disappeared before the agent response was persisted.")
    conversation = locked_conversation
    active_handoff = get_active_handoff(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
    )
    handoff_ack_allowed = _v2_handoff_ack_allowed(
        active_handoff,
        created_this_turn=created_handoff_this_turn,
    )
    if (not agent_can_reply(conversation) or active_handoff is not None) and not handoff_ack_allowed:
        db.rollback()
        return AgentChatResponse(
            run_id=run_id,
            conversation_id=conversation.id,
            inbound_message_id=inbound.id,
            outbound_message_id=None,
            reply=None,
            handoff_required=True,
            agent_paused=True,
            model=None,
        )

    verified_read_context = _verified_read_context_from_turn(
        db,
        workspace=workspace,
        turn=turn,
    )
    verified_action_context = _outbound_verified_action_context(
        turn,
        recent_action_context=recent_action_context,
    )
    outbound_now = datetime.now(UTC)
    outbound = Message(
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        channel_connection_id=conversation.channel_connection_id,
        sender_type="ai",
        direction="outbound",
        in_reply_to_message_id=inbound.id,
        created_at=outbound_now,
        message_type="text",
        content=turn.reply,
        delivery_status=outbound_delivery_status,
        metadata_json={
            "agent_run_id": str(run_id),
            "model": turn.responder_model,
            "source": source,
            "runtime": "v2",
            "in_reply_to_message_id": str(inbound.id),
            "dispatch_required": outbound_delivery_status == "queued",
            "handoff_ack": handoff_ack_allowed,
            "v2_read_context": verified_read_context,
            "v2_action_context": verified_action_context,
            "v2_action_context_passthrough": _safe_action_context_passthrough(turn),
            "v2_pending_choice": (
                turn.pending_choice.model_dump(mode="json")
                if turn.pending_choice is not None
                else None
            ),
        },
    )
    conversation.last_message_at = outbound_now
    db.add(outbound)
    db.commit()

    return AgentChatResponse(
        run_id=run_id,
        conversation_id=conversation.id,
        inbound_message_id=inbound.id,
        outbound_message_id=outbound.id,
        reply=turn.reply,
        handoff_required=conversation.owner_type == OWNER_HUMAN,
        agent_paused=False,
        model=turn.responder_model,
    )


def run_agent_chat(
    *,
    db: Session,
    workspace: Workspace,
    payload: AgentChatRequest,
) -> AgentChatResponse:
    if not settings.agent_v2_live_enabled:
        return run_agent_chat_v1(db=db, workspace=workspace, payload=payload)

    now = datetime.now(UTC)
    run_id = uuid4()
    patient = _get_patient_v2(db, workspace_id=workspace.id, patient_id=payload.patient_id)
    conversation = _get_or_create_conversation(
        db,
        workspace=workspace,
        patient=patient,
        payload=payload,
        now=now,
    )
    activity_now = datetime.now(UTC)
    inbound = Message(
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        channel_connection_id=conversation.channel_connection_id,
        sender_type="patient",
        direction="inbound",
        created_at=activity_now,
        message_type="text",
        content=payload.message,
        delivery_status="received",
        metadata_json={"agent_run_id": str(run_id), "source": "agent_api"},
    )
    record_customer_inbound(conversation, now=activity_now)
    patient.last_contact_at = activity_now
    db.add(inbound)
    db.commit()
    db.refresh(inbound)
    db.refresh(conversation)
    db.refresh(patient)

    return _run_v2_after_inbound(
        db=db,
        workspace=workspace,
        patient=patient,
        conversation=conversation,
        inbound=inbound,
        run_id=run_id,
        outbound_delivery_status="sent",
        source="agent_api",
    )


def run_agent_for_existing_inbound(
    *,
    db: Session,
    workspace: Workspace,
    patient: Patient,
    conversation: Conversation,
    inbound: Message,
    source: str = "channel_adapter",
) -> AgentChatResponse:
    if not settings.agent_v2_live_enabled:
        return run_agent_for_existing_inbound_v1(
            db=db,
            workspace=workspace,
            patient=patient,
            conversation=conversation,
            inbound=inbound,
            source=source,
        )

    if inbound.workspace_id != workspace.id:
        raise AgentChatError("Inbound message belongs to another workspace.")
    if inbound.conversation_id != conversation.id:
        raise AgentChatError("Inbound message belongs to another conversation.")
    if conversation.patient_id != patient.id:
        raise AgentChatError("Conversation belongs to another patient.")
    if inbound.sender_type != "patient" or inbound.direction != "inbound":
        raise AgentChatError(
            "Only inbound patient messages can be processed by the customer agent."
        )

    metadata = dict(inbound.metadata_json or {})
    existing_run_id = _uuid_from_metadata(metadata.get("agent_run_id"))
    run_id = existing_run_id or uuid4()
    metadata["agent_run_id"] = str(run_id)
    try:
        prior_attempts = int(metadata.get("agent_processing_attempts") or 0)
    except (TypeError, ValueError):
        prior_attempts = 0
    metadata["agent_processing_attempts"] = prior_attempts + 1
    inbound.metadata_json = metadata
    patient.last_contact_at = inbound.created_at
    db.commit()
    db.refresh(inbound)

    if existing_run_id is not None:
        existing_response = _existing_agent_response_for_inbound(
            db,
            conversation=conversation,
            inbound=inbound,
            run_id=run_id,
        )
        if existing_response is not None:
            return existing_response

    return _run_v2_after_inbound(
        db=db,
        workspace=workspace,
        patient=patient,
        conversation=conversation,
        inbound=inbound,
        run_id=run_id,
        outbound_delivery_status="queued",
        source=source,
    )