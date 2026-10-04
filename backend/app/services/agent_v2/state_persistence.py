from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.conversation_flow_event import ConversationFlowEvent
from app.models.conversation_flow_state import ConversationFlowState
from app.services.agent_v2.state import (
    ActiveTaskState,
    BookingTaskState,
    RescheduleTaskState,
)
from app.services.conversation_flows import (
    FlowStateConflictError,
    cancel_flow,
    complete_flow,
    flow_ttl_hours,
    get_active_flow,
    transition_flow,
)

_V2_NAMESPACE = "agent_core_v2"
_V2_SCHEMA_VERSION = 1

_FLOW_TYPE_BY_TASK = {
    "booking": "booking",
    "reschedule": "appointment_reschedule",
}
_CAPABILITY_BY_TASK = {
    "booking": "appointment_creation",
    "reschedule": "appointment_reschedule",
}
_FLOW_STATUS_BY_TASK_STATUS = {
    "collecting": "collecting_requirements",
    "awaiting_choice": "awaiting_option_selection",
    "ready": "ready_to_execute",
    "executing": "ready_to_execute",
}


class V2StatePersistenceError(RuntimeError):
    pass


class V2StateConflictError(V2StatePersistenceError):
    pass


class ForeignActiveFlowError(V2StateConflictError):
    pass


class InvalidPersistedV2StateError(V2StatePersistenceError):
    pass


@dataclass(frozen=True)
class PersistedActiveTask:
    active_task: ActiveTaskState
    flow_id: UUID
    flow_version: int


def _flow_type_for_task(active_task: ActiveTaskState) -> str:
    return _FLOW_TYPE_BY_TASK[active_task.task_type]


def _flow_status_for_task(active_task: ActiveTaskState) -> str:
    return _FLOW_STATUS_BY_TASK_STATUS[active_task.status]


def _capabilities_for_task(active_task: ActiveTaskState) -> list[str]:
    return [_CAPABILITY_BY_TASK[active_task.task_type]]


def _task_namespace(active_task: ActiveTaskState) -> dict[str, object]:
    return {
        "schema_version": _V2_SCHEMA_VERSION,
        "active_task": active_task.model_dump(mode="json"),
    }


def _entity_state_for_task(
    active_task: ActiveTaskState,
    *,
    existing: dict | None = None,
) -> dict[str, object]:
    result = dict(existing or {})
    result[_V2_NAMESPACE] = _task_namespace(active_task)
    return result


def _option_snapshot_for_task(active_task: ActiveTaskState) -> dict[str, object]:
    if active_task.option_snapshot is None:
        return {}
    return active_task.option_snapshot.model_dump(mode="json")


def _last_decision_for_task(active_task: ActiveTaskState) -> dict[str, object]:
    return {
        _V2_NAMESPACE: {
            "schema_version": _V2_SCHEMA_VERSION,
            "task_type": active_task.task_type,
            "task_version": active_task.version,
        }
    }


def _decode_flow_task(flow: ConversationFlowState) -> ActiveTaskState | None:
    entity_state = flow.entity_state or {}
    namespace = entity_state.get(_V2_NAMESPACE)
    if namespace is None:
        return None
    if not isinstance(namespace, dict):
        raise InvalidPersistedV2StateError("V2 flow namespace must be an object.")
    if namespace.get("schema_version") != _V2_SCHEMA_VERSION:
        raise InvalidPersistedV2StateError("Unsupported V2 persisted state schema version.")

    payload = namespace.get("active_task")
    if not isinstance(payload, dict):
        raise InvalidPersistedV2StateError("V2 flow is missing its active task payload.")

    task_type = payload.get("task_type")
    try:
        if task_type == "booking":
            active_task: ActiveTaskState = BookingTaskState.model_validate(payload)
        elif task_type == "reschedule":
            active_task = RescheduleTaskState.model_validate(payload)
        else:
            raise InvalidPersistedV2StateError("Unknown V2 persisted task type.")
    except ValidationError as exc:
        raise InvalidPersistedV2StateError("V2 persisted task payload is invalid.") from exc

    expected_flow_type = _flow_type_for_task(active_task)
    if flow.flow_type != expected_flow_type:
        raise InvalidPersistedV2StateError(
            "Persisted V2 task type does not match the conversation flow type."
        )
    return active_task


def _handle(flow: ConversationFlowState, active_task: ActiveTaskState) -> PersistedActiveTask:
    return PersistedActiveTask(
        active_task=active_task,
        flow_id=flow.id,
        flow_version=flow.version,
    )


def load_active_task(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    patient_id: UUID,
    run_id: UUID | None = None,
) -> PersistedActiveTask | None:
    """Load only Agent Core V2 state; active V1 flows remain foreign and untouched."""
    flow = get_active_flow(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        patient_id=patient_id,
        run_id=run_id,
    )
    if flow is None:
        return None

    active_task = _decode_flow_task(flow)
    if active_task is None:
        return None
    return _handle(flow, active_task)


def _active_flow_for_expected_handle(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    patient_id: UUID,
    expected: PersistedActiveTask,
) -> ConversationFlowState:
    flow = db.scalar(
        select(ConversationFlowState).where(
            ConversationFlowState.id == expected.flow_id,
            ConversationFlowState.workspace_id == workspace_id,
            ConversationFlowState.conversation_id == conversation_id,
            ConversationFlowState.patient_id == patient_id,
            ConversationFlowState.is_active.is_(True),
        )
    )
    if flow is None or flow.version != expected.flow_version:
        raise V2StateConflictError("V2 active task changed while this turn was being processed.")

    persisted_task = _decode_flow_task(flow)
    if persisted_task is None:
        raise ForeignActiveFlowError("The active conversation flow is not owned by Agent Core V2.")
    if persisted_task.task_type != expected.active_task.task_type:
        raise V2StateConflictError("V2 active task type changed while this turn was being processed.")
    return flow


def _add_started_event(
    db: Session,
    flow: ConversationFlowState,
    *,
    run_id: UUID,
) -> None:
    db.add(
        ConversationFlowEvent(
            workspace_id=flow.workspace_id,
            flow_state_id=flow.id,
            conversation_id=flow.conversation_id,
            run_id=run_id,
            event_type="started",
            actor_type="system",
            state_version=flow.version,
            metadata_json={
                "owner": _V2_NAMESPACE,
                "schema_version": _V2_SCHEMA_VERSION,
                "flow_type": flow.flow_type,
            },
        )
    )
    db.flush()


def _create_active_task(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    patient_id: UUID,
    active_task: ActiveTaskState,
    run_id: UUID,
) -> PersistedActiveTask:
    current = get_active_flow(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        patient_id=patient_id,
        run_id=run_id,
    )
    if current is not None:
        if _decode_flow_task(current) is None:
            raise ForeignActiveFlowError(
                "A non-V2 conversation flow is already active; V2 will not replace it."
            )
        raise V2StateConflictError(
            "A V2 active task already exists; save it with its expected persistence handle."
        )

    now = datetime.now(UTC)
    flow = ConversationFlowState(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        patient_id=patient_id,
        flow_type=_flow_type_for_task(active_task),
        status=_flow_status_for_task(active_task),
        is_active=True,
        capabilities=_capabilities_for_task(active_task),
        entity_state=_entity_state_for_task(active_task),
        missing_information=[],
        pending_action={},
        option_snapshot=_option_snapshot_for_task(active_task),
        last_decision=_last_decision_for_task(active_task),
        version=1,
        expires_at=now + timedelta(hours=flow_ttl_hours(_flow_type_for_task(active_task))),
        last_turn_at=now,
    )

    try:
        with db.begin_nested():
            db.add(flow)
            db.flush()
    except IntegrityError as exc:
        concurrent = get_active_flow(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            patient_id=patient_id,
            run_id=run_id,
        )
        if concurrent is not None and _decode_flow_task(concurrent) is None:
            raise ForeignActiveFlowError(
                "A non-V2 flow won the active-flow creation race; V2 left it untouched."
            ) from exc
        raise V2StateConflictError(
            "Another turn created or changed the active V2 task concurrently."
        ) from exc

    _add_started_event(db, flow, run_id=run_id)
    return _handle(flow, active_task)


def save_active_task(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    patient_id: UUID,
    active_task: ActiveTaskState,
    run_id: UUID,
    expected: PersistedActiveTask | None = None,
) -> PersistedActiveTask:
    """Persist one V2 task transition using DB flow version as the concurrency token."""
    if expected is None:
        return _create_active_task(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            patient_id=patient_id,
            active_task=active_task,
            run_id=run_id,
        )

    flow = _active_flow_for_expected_handle(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        patient_id=patient_id,
        expected=expected,
    )
    if active_task.task_type != expected.active_task.task_type:
        raise V2StateConflictError("A persisted V2 task cannot change task type in place.")

    event_type = (
        "options_presented"
        if active_task.status == "awaiting_choice" and active_task.option_snapshot is not None
        else "updated"
    )
    try:
        updated = transition_flow(
            db,
            flow,
            actor_type="system",
            event_type=event_type,
            run_id=run_id,
            status=_flow_status_for_task(active_task),
            capabilities=_capabilities_for_task(active_task),
            entity_state=_entity_state_for_task(active_task, existing=flow.entity_state),
            missing_information=[],
            option_snapshot=_option_snapshot_for_task(active_task),
            last_decision=_last_decision_for_task(active_task),
        )
    except FlowStateConflictError as exc:
        raise V2StateConflictError(str(exc)) from exc
    return _handle(updated, active_task)


def cancel_active_task(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    patient_id: UUID,
    expected: PersistedActiveTask,
    run_id: UUID | None,
    reason: str = "customer_cancelled_active_task",
) -> None:
    flow = _active_flow_for_expected_handle(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        patient_id=patient_id,
        expected=expected,
    )
    try:
        cancel_flow(db, flow, run_id=run_id, reason=reason)
    except FlowStateConflictError as exc:
        raise V2StateConflictError(str(exc)) from exc


def complete_active_task(
    db: Session,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    patient_id: UUID,
    expected: PersistedActiveTask,
    run_id: UUID | None,
    result: dict[str, object],
) -> None:
    flow = _active_flow_for_expected_handle(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        patient_id=patient_id,
        expected=expected,
    )
    try:
        complete_flow(db, flow, run_id=run_id, result=result)
    except FlowStateConflictError as exc:
        raise V2StateConflictError(str(exc)) from exc
