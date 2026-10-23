from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.agents.v2.availability_pagination import (
    availability_windows_from_outcome_facts,
    select_availability_window_page,
)
from app.agents.v2.availability_scope import (
    AVAILABILITY_PRESENTATION_SCOPE_KEY,
    availability_scope_key_from_reads,
    availability_scope_matches,
)
from app.core.config import settings
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import Appointment
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
from app.services.agent_v2.orchestrator import (
    V2OrchestratedTurn,
    V2TurnInterpretationStructuredOutputError,
    orchestrate_v2_turn,
)
from app.services.agent_v2.reference_resolution import build_availability_reference_options
from app.services.agent_v2.state import BookingTaskState
from app.services.agent_v2.write_executor import execute_write_ready_step
from app.services.conversation_ownership import (
    OWNER_HUMAN,
    agent_can_reply,
    lock_conversation_ownership,
    record_customer_inbound,
)
from app.services.handoffs import create_handoff, get_active_handoff

logger = logging.getLogger(__name__)


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


def _v2_handoff_continuation_allowed(
    conversation: Conversation,
    handoff: object | None,
) -> bool:
    return (
        handoff is not None
        and conversation.owner_type == OWNER_HUMAN
        and conversation.status == "pending"
        and getattr(handoff, "source", None) == "ai"
        and getattr(handoff, "status", None) == "pending"
        and getattr(handoff, "assigned_user_id", None) is None
    )


def _previous_handoff_ack_reply(
    db: Session,
    *,
    conversation: Conversation,
    inbound: Message,
    handoff_id: object,
) -> str | None:
    messages = db.scalars(
        select(Message)
        .where(
            Message.workspace_id == conversation.workspace_id,
            Message.conversation_id == conversation.id,
            Message.sender_type == "ai",
            Message.direction == "outbound",
            Message.created_at < inbound.created_at,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(20)
    )
    expected_handoff_id = str(handoff_id)
    for message in messages:
        metadata = dict(message.metadata_json or {})
        if metadata.get("handoff_ack") is not True:
            continue
        if str(metadata.get("handoff_id") or "") != expected_handoff_id:
            continue
        content = str(message.content or "").strip()
        if content:
            return content
    return None


def _deterministic_handoff_continuation_reply(customer_text: str) -> str:
    arabic = any("\u0600" <= char <= "\u06ff" for char in customer_text)
    if arabic:
        return "الطلب لسه مع فريق العيادة للمراجعة، وأي إجراء هيتم بعد مراجعتهم."
    return "Your request is still with the clinic team for review. Any action will happen after their review."


def _persist_handoff_continuation(
    *,
    db: Session,
    workspace: Workspace,
    conversation: Conversation,
    inbound: Message,
    run_id: UUID,
    outbound_delivery_status: str,
    source: str,
) -> AgentChatResponse | None:
    locked_conversation = lock_conversation_ownership(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
    )
    if locked_conversation is None:
        raise AgentChatError("Conversation disappeared before handoff continuation.")
    conversation = locked_conversation
    active_handoff = get_active_handoff(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        for_update=True,
    )
    if not _v2_handoff_continuation_allowed(conversation, active_handoff):
        db.rollback()
        return None

    handoff_id = getattr(active_handoff, "id", None)
    previous_ack = (
        _previous_handoff_ack_reply(
            db,
            conversation=conversation,
            inbound=inbound,
            handoff_id=handoff_id,
        )
        if handoff_id is not None
        else None
    )
    reply = previous_ack or _deterministic_handoff_continuation_reply(
        str(inbound.content or "")
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
        content=reply,
        delivery_status=outbound_delivery_status,
        metadata_json={
            "agent_run_id": str(run_id),
            "model": "deterministic:handoff-continuation",
            "source": source,
            "runtime": "v2",
            "in_reply_to_message_id": str(inbound.id),
            "dispatch_required": outbound_delivery_status == "queued",
            "handoff_continuation": True,
            "handoff_id": str(handoff_id),
            "handoff_ack_reused": previous_ack is not None,
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
        reply=reply,
        handoff_required=True,
        agent_paused=True,
        model="deterministic:handoff-continuation",
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


def _recent_availability_reference_context(
    db: Session,
    *,
    conversation: Conversation,
    inbound: Message,
) -> dict[str, Any] | None:
    """Read the carried server-owned displayed availability state from the prior AI turn."""
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
    value = metadata.get("v2_availability_reference_context")
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


def _verified_automation_context(
    db: Session,
    *,
    conversation: Conversation,
    patient: Patient,
    metadata: dict[str, Any],
    source_message_id: object,
) -> dict[str, Any] | None:
    appointment_id = _uuid_from_metadata(metadata.get("appointment_id"))
    if appointment_id is None:
        return None
    appointment = db.scalar(
        select(Appointment).where(
            Appointment.workspace_id == conversation.workspace_id,
            Appointment.id == appointment_id,
            Appointment.patient_id == patient.id,
        )
    )
    if appointment is None:
        return None
    return {
        "source": "automation_engine",
        "automation_message_id": str(source_message_id),
        "automation_job_id": str(metadata.get("automation_job_id") or ""),
        "automation_rule_key": str(metadata.get("automation_rule_key") or ""),
        "appointment_id": str(appointment.id),
        "service_id": str(appointment.service_id),
        "device_key": (
            str(appointment.laser_device_key)
            if appointment.laser_device_key not in (None, "")
            else None
        ),
        "appointment_status": appointment.status,
        "start_at": appointment.start_at.isoformat(),
    }


def _recent_automation_context(
    db: Session,
    *,
    conversation: Conversation,
    patient: Patient,
    inbound: Message,
) -> dict[str, Any] | None:
    """Return the immediately preceding verified automation focus.

    Template prose is never parsed for identity. A contained structured-output failure
    may carry the prior server-owned automation metadata for one clarification hop, but
    the appointment target is re-verified against the patient before reuse.
    """
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
    if previous is None or previous.direction != "outbound":
        return None

    metadata = dict(previous.metadata_json or {})
    if previous.sender_type == "system" and metadata.get("source") == "automation_engine":
        return _verified_automation_context(
            db,
            conversation=conversation,
            patient=patient,
            metadata=metadata,
            source_message_id=previous.id,
        )

    carried = metadata.get("v2_automation_context")
    if (
        previous.sender_type == "ai"
        and metadata.get("runtime") == "v2"
        and isinstance(metadata.get("v2_structured_interpretation_failure"), dict)
        and isinstance(carried, dict)
    ):
        return _verified_automation_context(
            db,
            conversation=conversation,
            patient=patient,
            metadata=carried,
            source_message_id=carried.get("automation_message_id") or previous.id,
        )
    return None


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


def _displayed_booking_availability_context_from_outcome(
    turn: V2OrchestratedTurn,
) -> dict[str, Any] | None:
    """Recover displayed booking availability from runtime truth + task snapshot."""
    task = turn.active_task
    if not isinstance(task, BookingTaskState):
        return None
    snapshot = task.option_snapshot
    if snapshot is None or snapshot.task_version != task.version or not snapshot.options:
        return None

    presented = next(
        (
            outcome
            for outcome in reversed(turn.outcomes)
            if outcome.response_goal == "present_availability"
        ),
        None,
    )
    if presented is None:
        return None
    service_name, windows = availability_windows_from_outcome_facts(presented.facts)
    if not windows:
        return None

    selected_windows, selected_keys, has_more = select_availability_window_page(
        windows,
        service_name=service_name,
    )
    verified_slots: list[dict[str, object]] = []
    for choice in snapshot.options:
        payload = dict(choice.payload)
        if payload.get("device_key") not in (None, ""):
            payload.setdefault("laser_device_key", payload["device_key"])
        verified_slots.append(payload)
    reference_options = build_availability_reference_options(
        displayed_windows=selected_windows,
        verified_slots=verified_slots,
    )
    if not reference_options:
        return None

    constraints = task.constraints
    context: dict[str, Any] = {
        "operation_type": "book",
        "availability_reference_options": reference_options,
        "availability_presented_window_keys": sorted(selected_keys),
        "availability_presentation_has_more": has_more,
    }
    for key in ("service_id", "doctor_id", "branch_id", "device_key"):
        value = getattr(constraints, key, None)
        if value not in (None, ""):
            context[key] = str(value)
    if constraints.date is not None:
        context["date"] = constraints.date.model_dump(mode="json")
    if constraints.time is not None:
        context["time"] = constraints.time.model_dump(mode="json")
    if constraints.package_usage not in (None, ""):
        context["package_usage"] = constraints.package_usage
    availability = presented.facts.get("availability")
    if isinstance(availability, dict):
        count = availability.get("available_option_count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            context["availability_option_count"] = count
        checked_dates = availability.get("checked_dates")
        if isinstance(checked_dates, list) and checked_dates:
            context["availability_checked_dates"] = [
                str(value) for value in checked_dates if isinstance(value, str) and value
            ]
        search_truncated = availability.get("search_truncated")
        if isinstance(search_truncated, bool):
            context["availability_search_truncated"] = search_truncated
    return context



def _grouped_availability_read_context_from_turn(
    turn: V2OrchestratedTurn,
) -> dict[str, Any] | None:
    active_task = getattr(turn, "active_task", None)
    if not isinstance(active_task, BookingTaskState):
        return None
    requested = list(dict.fromkeys(active_task.requested_service_ids))
    if len(requested) < 2:
        return None

    options: list[dict[str, object]] = []
    presented_keys: set[str] = set()
    seen_services: set[str] = set()
    common_scope: dict[str, object] = {}
    common_scope_initialized = False
    option_index = 0

    for step in turn.plan.steps:
        if step.disposition != "read" or step.write_intent is not None:
            continue
        availability_read = next(
            (request for request in step.reads if request.kind == "availability"),
            None,
        )
        if availability_read is None:
            continue
        service_id = str(
            availability_read.parameters.get("service_id")
            or step.facts.get("service_id")
            or ""
        )
        if service_id not in requested:
            continue
        matching_trace = next(
            (
                trace
                for trace in reversed(turn.traces)
                if trace.operation_index == step.operation_index
                and trace.outcome is not None
            ),
            None,
        )
        if matching_trace is None or matching_trace.outcome is None:
            continue
        service_name, windows = availability_windows_from_outcome_facts(
            matching_trace.outcome.facts
        )
        if not windows:
            continue
        selected_windows, selected_keys, _has_more = select_availability_window_page(
            windows,
            service_name=service_name,
            shown_keys=set(),
        )
        verified_slots = [
            dict(item) for item in getattr(matching_trace, "availability_slots", ())
        ]
        component_options = build_availability_reference_options(
            displayed_windows=selected_windows,
            verified_slots=verified_slots,
        )
        if not component_options:
            continue
        seen_services.add(service_id)
        presented_keys.update(selected_keys)
        for option in component_options:
            option_index += 1
            item = dict(option)
            item["option_ref"] = f"opt_{option_index}"
            item["index"] = option_index
            item["service_id"] = service_id
            options.append(item)

        component_scope = {
            key: value
            for key in ("branch_id", "date", "package_usage")
            if (value := availability_read.parameters.get(key)) not in (None, "", [], {})
        }
        if not common_scope_initialized:
            common_scope = component_scope
            common_scope_initialized = True
        else:
            common_scope = {
                key: value
                for key, value in common_scope.items()
                if component_scope.get(key) == value
            }

    if seen_services != set(requested) or not options:
        return None
    context: dict[str, Any] = {
        "operation_type": "availability",
        "requested_service_ids": requested,
        "multi_service_single_appointment": True,
        "availability_reference_options": options,
        "availability_option_count": len(options),
        **common_scope,
    }
    if presented_keys:
        context["availability_presented_window_keys"] = sorted(presented_keys)
    return context


def _verified_read_context_from_turn(
    db: Session,
    *,
    workspace: Workspace,
    turn: V2OrchestratedTurn,
    previous_read_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Keep only the last read-only canonical scope plus a minimal verified result summary."""
    availability_presented = any(
        outcome.response_goal == "present_availability" for outcome in getattr(turn, "outcomes", ())
    )
    if availability_presented:
        grouped_context = _grouped_availability_read_context_from_turn(turn)
        if grouped_context is not None:
            return grouped_context
    else:
        for step in reversed(turn.plan.steps):
            if step.facts.get("verified_availability_reference") is True and isinstance(
                previous_read_context, dict
            ):
                context = dict(previous_read_context)
                selected_index = step.facts.get("availability_reference_index")
                if isinstance(selected_index, int) and not isinstance(selected_index, bool):
                    context["availability_reference_anchor_index"] = selected_index
                return context

    for step in reversed(turn.plan.steps):
        if step.disposition != "read" or step.write_intent is not None or not step.reads:
            continue
        context: dict[str, Any] = {"operation_type": step.operation_type}
        scope_source = step.facts
        if step.operation_type == "appointment_list":
            appointment_read = next(
                (request for request in step.reads if request.kind == "appointments"),
                None,
            )
            if appointment_read is not None:
                scope_source = appointment_read.parameters
            trace = next(
                (
                    item
                    for item in reversed(turn.traces)
                    if item.operation_index == step.operation_index
                    and item.operation_type == "appointment_list"
                    and isinstance(getattr(item, "verified_parameters", None), dict)
                ),
                None,
            )
            if trace is not None:
                verified_parameters = getattr(trace, "verified_parameters", None)
                appointment_id = (
                    verified_parameters.get("appointment_id")
                    if isinstance(verified_parameters, dict)
                    else None
                )
                grouped_ids = (
                    verified_parameters.get("appointment_ids")
                    if isinstance(verified_parameters, dict)
                    else None
                )
                if appointment_id not in (None, "") and not grouped_ids:
                    context["appointment_id"] = str(appointment_id)
        for key in (
            "service_id",
            "requested_service_ids",
            "doctor_id",
            "doctor_ids",
            "device_key",
            "date",
            "time",
            "package_usage",
        ):
            value = scope_source.get(key)
            if value not in (None, "", [], {}):
                context[key] = value

        if any(request.kind == "package_offers" for request in step.reads):
            trace = next(
                (
                    item
                    for item in reversed(turn.traces)
                    if item.operation_index == step.operation_index
                    and isinstance(getattr(item, "verified_parameters", None), dict)
                ),
                None,
            )
            verified_offer = (
                dict(trace.verified_parameters)
                if trace is not None and isinstance(trace.verified_parameters, dict)
                else {}
            )
            package_offer_id = verified_offer.get("package_offer_id")
            package_sessions = verified_offer.get("package_sessions")
            service_id = verified_offer.get("service_id")
            if (
                package_offer_id not in (None, "")
                and isinstance(package_sessions, int)
                and not isinstance(package_sessions, bool)
                and package_sessions > 0
                and service_id not in (None, "")
            ):
                context["package_offer_id"] = str(package_offer_id)
                context["package_sessions"] = package_sessions
                context["service_id"] = str(service_id)
                if verified_offer.get("device_key") not in (None, ""):
                    context["device_key"] = str(verified_offer["device_key"])

        option_count = _availability_option_count_for_step(
            turn,
            operation_index=step.operation_index,
        )
        if option_count is not None:
            context["availability_option_count"] = option_count

        if step.operation_type in {"availability", "book", "reschedule"}:
            matching_trace = next(
                (
                    trace
                    for trace in reversed(turn.traces)
                    if trace.operation_index == step.operation_index
                    and trace.outcome is not None
                ),
                None,
            )
            outcome = matching_trace.outcome if matching_trace is not None else None
            if outcome is not None:
                availability_facts = outcome.facts.get("availability")
                if isinstance(availability_facts, dict):
                    checked_dates = availability_facts.get("checked_dates")
                    if isinstance(checked_dates, list) and checked_dates:
                        context["availability_checked_dates"] = [
                            str(value)
                            for value in checked_dates
                            if isinstance(value, str) and value
                        ]
                    search_truncated = availability_facts.get("search_truncated")
                    if isinstance(search_truncated, bool):
                        context["availability_search_truncated"] = search_truncated
                service_name, windows = availability_windows_from_outcome_facts(
                    outcome.facts
                )
                if windows:
                    operation = (
                        turn.understanding.operations[step.operation_index]
                        if step.operation_index < len(turn.understanding.operations)
                        else None
                    )
                    current_scope_key = availability_scope_key_from_reads(step.reads)
                    continuation = bool(
                        operation is not None
                        and getattr(operation, "continues_previous", False)
                        and availability_scope_matches(
                            previous_read_context,
                            current_scope_key=current_scope_key,
                        )
                    )
                    if current_scope_key:
                        context[AVAILABILITY_PRESENTATION_SCOPE_KEY] = current_scope_key
                    previous_keys = set()
                    if continuation and isinstance(previous_read_context, dict):
                        raw_keys = previous_read_context.get(
                            "availability_presented_window_keys"
                        )
                        if isinstance(raw_keys, list):
                            previous_keys = {
                                str(value)
                                for value in raw_keys
                                if isinstance(value, str) and value
                            }
                    selected_windows, selected_keys, has_more = select_availability_window_page(
                        windows,
                        service_name=service_name,
                        shown_keys=previous_keys,
                    )
                    verified_slots = (
                        [dict(item) for item in getattr(matching_trace, "availability_slots", ())]
                        if matching_trace is not None
                        else []
                    )
                    reference_options = build_availability_reference_options(
                        displayed_windows=selected_windows,
                        verified_slots=verified_slots,
                    )
                    if reference_options:
                        context["availability_reference_options"] = reference_options
                    cumulative = sorted(previous_keys | set(selected_keys))
                    if cumulative:
                        context["availability_presented_window_keys"] = cumulative
                    context["availability_presentation_has_more"] = has_more

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

        previous_appointment_id = (
            previous_read_context.get("appointment_id")
            if isinstance(previous_read_context, dict)
            and previous_read_context.get("operation_type") == "appointment_list"
            else None
        )
        current_has_appointment_read = any(
            current_step.operation_type == "appointment_list"
            for current_step in turn.plan.steps
        )
        if (
            step.operation_type != "appointment_list"
            and not current_has_appointment_read
            and previous_appointment_id not in (None, "")
            and _safe_action_context_passthrough(turn)
        ):
            context["appointment_id"] = str(previous_appointment_id)
        return context
    return _displayed_booking_availability_context_from_outcome(turn)

def _availability_reference_context_from_turn(
    turn: V2OrchestratedTurn,
    *,
    previous_context: dict[str, Any] | None,
    verified_read_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Persist only the latest displayed options plus one selected opaque ref."""
    if any(
        outcome.status == "completed"
        and outcome.response_goal in {"booking_completed", "reschedule_completed"}
        for outcome in turn.outcomes
    ):
        return None

    availability_presented = any(
        outcome.response_goal == "present_availability" for outcome in getattr(turn, "outcomes", ())
    )
    if availability_presented and isinstance(verified_read_context, dict):
        raw_options = verified_read_context.get("availability_reference_options")
        if isinstance(raw_options, list) and raw_options:
            context: dict[str, Any] = {
                "availability_reference_options": [
                    dict(item) for item in raw_options if isinstance(item, dict)
                ]
            }
            for key in (
                "service_id",
                "requested_service_ids",
                "doctor_id",
                "doctor_ids",
                "device_key",
                "date",
                "time",
                "package_usage",
            ):
                value = verified_read_context.get(key)
                if value not in (None, "", [], {}):
                    context[key] = value
            has_more = verified_read_context.get("availability_presentation_has_more")
            if isinstance(has_more, bool):
                context["availability_presentation_has_more"] = has_more
            checked_dates = verified_read_context.get("availability_checked_dates")
            if isinstance(checked_dates, list) and checked_dates:
                context["availability_checked_dates"] = [
                    str(value) for value in checked_dates if isinstance(value, str) and value
                ]
            search_truncated = verified_read_context.get("availability_search_truncated")
            if isinstance(search_truncated, bool):
                context["availability_search_truncated"] = search_truncated
            return context

    for step in reversed(turn.plan.steps):
        if step.facts.get("verified_availability_reference") is not True:
            continue
        if not isinstance(previous_context, dict):
            return None
        context = dict(previous_context)
        selected_ref = step.facts.get("availability_reference_option_ref")
        pending_time = step.facts.get("availability_reference_pending_time")
        scope_field = step.facts.get("availability_reference_scope_field")
        scope_value = step.facts.get("availability_reference_scope_value")
        if (
            scope_field in {"doctor_id", "device_key"}
            and isinstance(scope_value, str)
            and scope_value
        ):
            context[str(scope_field)] = scope_value
        if isinstance(selected_ref, str) and selected_ref:
            context["last_selected_option_ref"] = selected_ref
            context.pop("pending_exact_time", None)
        elif isinstance(pending_time, str) and pending_time:
            context["pending_exact_time"] = pending_time
        elif step.facts.get("availability_reference_reason") == "semantic_clarification":
            context.pop("pending_exact_time", None)
        return context

    availability_read = any(
        request.kind == "availability"
        for step in turn.plan.steps
        for request in step.reads
    )
    if availability_read or availability_presented:
        if not isinstance(verified_read_context, dict):
            return None
        raw_options = verified_read_context.get("availability_reference_options")
        if not isinstance(raw_options, list) or not raw_options:
            return None
        context: dict[str, Any] = {
            "availability_reference_options": [
                dict(item) for item in raw_options if isinstance(item, dict)
            ]
        }
        for key in (
            "service_id",
            "requested_service_ids",
            "doctor_id",
            "doctor_ids",
            "device_key",
            "date",
            "time",
            "package_usage",
        ):
            value = verified_read_context.get(key)
            if value not in (None, "", [], {}):
                context[key] = value
        has_more = verified_read_context.get("availability_presentation_has_more")
        if isinstance(has_more, bool):
            context["availability_presentation_has_more"] = has_more
        checked_dates = verified_read_context.get("availability_checked_dates")
        if isinstance(checked_dates, list) and checked_dates:
            context["availability_checked_dates"] = [
                str(value) for value in checked_dates if isinstance(value, str) and value
            ]
        search_truncated = verified_read_context.get("availability_search_truncated")
        if isinstance(search_truncated, bool):
            context["availability_search_truncated"] = search_truncated
        return context

    if (
        getattr(turn, "reference_semantic_path_used", False)
        and getattr(turn, "reference_action", None) == "new_search"
    ):
        return None

    if isinstance(previous_context, dict):
        active_task = getattr(turn, "active_task", None)
        active_scope = getattr(active_task, "constraints", None) or getattr(
            active_task,
            "replacement",
            None,
        )
        task_service_id = getattr(active_scope, "service_id", None)
        previous_service_id = previous_context.get("service_id")
        if (
            task_service_id not in (None, "")
            and previous_service_id not in (None, "")
            and str(task_service_id) != str(previous_service_id)
        ):
            return None

    return dict(previous_context) if isinstance(previous_context, dict) else None


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


_STRUCTURED_INTERPRETATION_MODEL = "deterministic:structured-output-clarification"


def _structured_interpretation_clarification_reply(patient: Patient) -> str:
    language = str(getattr(patient, "preferred_language", "ar") or "ar").lower()
    if language.startswith("ar"):
        return (
            "\u0645\u0639\u0644\u0634\u060c \u0645\u0645\u0643\u0646 \u062a\u0648\u0636\u062d\u064a\u0644\u064a "
            "\u0637\u0644\u0628\u0643 \u0628\u0634\u0643\u0644 \u0623\u062f\u0642 \u0634\u0648\u064a\u0629 \u0639\u0634\u0627\u0646 "
            "\u0623\u062a\u0623\u0643\u062f \u0625\u0646\u064a \u0645\u0646\u0641\u0630\u0634 \u062d\u0627\u062c\u0629 \u063a\u0644\u0637\u061f"
        )
    return (
        "Sorry, could you clarify your request a little so I can make sure I do not "
        "do the wrong thing?"
    )


def _persist_structured_interpretation_failure(
    *,
    db: Session,
    workspace: Workspace,
    patient: Patient,
    conversation: Conversation,
    inbound: Message,
    run_id: UUID,
    outbound_delivery_status: str,
    source: str,
    recent_read_context: dict[str, Any] | None,
    recent_action_context: dict[str, Any] | None,
    pending_choice_context: dict[str, Any] | None,
    availability_reference_context: dict[str, Any] | None,
    automation_context: dict[str, Any] | None,
) -> AgentChatResponse:
    """Persist a non-executable clarification while preserving verified prior context."""
    locked_conversation = lock_conversation_ownership(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
    )
    if locked_conversation is None:
        raise AgentChatError(
            "Conversation disappeared before structured-output clarification was persisted."
        )
    conversation = locked_conversation
    active_handoff = get_active_handoff(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
    )
    if not agent_can_reply(conversation) or active_handoff is not None:
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

    reply = _structured_interpretation_clarification_reply(patient)
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
        content=reply,
        delivery_status=outbound_delivery_status,
        metadata_json={
            "agent_run_id": str(run_id),
            "model": _STRUCTURED_INTERPRETATION_MODEL,
            "source": source,
            "runtime": "v2",
            "in_reply_to_message_id": str(inbound.id),
            "dispatch_required": outbound_delivery_status == "queued",
            "v2_structured_interpretation_failure": {
                "stage": "turn_interpretation",
                "category": "StructuredOutputError",
                "primary_model": settings.openai_model,
                "fallback_model": settings.openai_fallback_model or None,
                "attempts_per_model": 2,
            },
            # A failed semantic turn is state-neutral. Carry only the already
            # verified/server-owned context that existed before the failed turn.
            "v2_read_context": recent_read_context,
            "v2_availability_reference_context": availability_reference_context,
            "v2_action_context": recent_action_context,
            "v2_action_context_passthrough": recent_action_context is not None,
            "v2_pending_choice": pending_choice_context,
            "v2_automation_context": automation_context,
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
        reply=reply,
        handoff_required=False,
        agent_paused=False,
        model=_STRUCTURED_INTERPRETATION_MODEL,
    )


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
    if _v2_handoff_continuation_allowed(conversation, active_handoff):
        continuation = _persist_handoff_continuation(
            db=db,
            workspace=workspace,
            conversation=conversation,
            inbound=inbound,
            run_id=run_id,
            outbound_delivery_status=outbound_delivery_status,
            source=source,
        )
        if continuation is not None:
            return continuation
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
    availability_reference_context = _recent_availability_reference_context(
        db,
        conversation=conversation,
        inbound=inbound,
    )
    recent_action_context = _recent_verified_action_context(
        db,
        conversation=conversation,
        inbound=inbound,
    )
    automation_context = _recent_automation_context(
        db,
        conversation=conversation,
        patient=patient,
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

    try:
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
            automation_context=automation_context,
            pending_choice_context=pending_choice_context,
            availability_reference_context=availability_reference_context,
        )
    except V2TurnInterpretationStructuredOutputError:
        logger.error(
            "V2 structured interpretation contained stage=turn_interpretation "
            "error_category=StructuredOutputError run_id=%s conversation_id=%s "
            "inbound_message_id=%s primary_model=%s fallback_model=%s "
            "attempts_per_model=2",
            run_id,
            conversation.id,
            inbound.id,
            settings.openai_model,
            settings.openai_fallback_model or None,
        )
        return _persist_structured_interpretation_failure(
            db=db,
            workspace=workspace,
            patient=patient,
            conversation=conversation,
            inbound=inbound,
            run_id=run_id,
            outbound_delivery_status=outbound_delivery_status,
            source=source,
            recent_read_context=recent_read_context,
            recent_action_context=recent_action_context,
            pending_choice_context=pending_choice_context,
            availability_reference_context=availability_reference_context,
            automation_context=automation_context,
        )
    if turn.pending_write is not None:
        raise RuntimeError("Live V2 turn returned an unexecuted verified write.")
    no_reply = turn.reply is None and turn.responder_model == "deterministic:no-reply"
    if not turn.reply and not no_reply:
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

    if no_reply:
        db.commit()
        return AgentChatResponse(
            run_id=run_id,
            conversation_id=conversation.id,
            inbound_message_id=inbound.id,
            outbound_message_id=None,
            reply=None,
            handoff_required=False,
            agent_paused=False,
            model=turn.responder_model,
        )

    verified_read_context = _verified_read_context_from_turn(
        db,
        workspace=workspace,
        turn=turn,
        previous_read_context=recent_read_context,
    )
    verified_action_context = _outbound_verified_action_context(
        turn,
        recent_action_context=recent_action_context,
    )
    outgoing_availability_reference_context = _availability_reference_context_from_turn(
        turn,
        previous_context=availability_reference_context,
        verified_read_context=verified_read_context,
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
            "handoff_id": (
                str(active_handoff.id)
                if handoff_ack_allowed and getattr(active_handoff, "id", None) is not None
                else None
            ),
            "v2_read_context": verified_read_context,
            "v2_availability_reference_context": outgoing_availability_reference_context,
            "v2_reference_semantic": {
                "path_used": turn.reference_semantic_path_used,
                "full_interpreter_called": turn.full_interpreter_called,
                "action": turn.reference_action,
                "selected_option_ref": turn.selected_option_ref,
                "structured_output_error": turn.reference_structured_output_error,
            },
            "v2_action_context": verified_action_context,
            "v2_action_context_passthrough": _safe_action_context_passthrough(turn),
            "v2_pending_choice": (
                turn.pending_choice.model_dump(mode="json")
                if turn.pending_choice is not None
                else None
            ),
            "v2_booking_proposal": (
                {
                    "proposal_id": turn.active_task.proposal.proposal_id,
                    "expires_at": turn.active_task.proposal.expires_at.isoformat(),
                }
                if isinstance(turn.active_task, BookingTaskState)
                and turn.active_task.proposal is not None
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
