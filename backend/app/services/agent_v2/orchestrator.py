from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from langchain_core.messages import BaseMessage, HumanMessage
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.agents.explicit_time import extract_single_explicit_hhmm
from app.agents.structured_output import StructuredOutputError
from app.agents.v2.availability_reference_interpreter import (
    ReferenceInterpretation,
    interpret_availability_reference_turn,
)
from app.agents.v2.availability_scope import (
    availability_scope_key_from_reads,
    availability_scope_matches,
)
from app.agents.v2.responder import compose_v2_customer_reply
from app.agents.v2.semantic_context import SemanticContext, build_semantic_context
from app.agents.v2.semantic_state_view import (
    with_safe_action_context,
    with_safe_automation_context,
    with_safe_availability_reference_context,
    with_safe_read_context,
    with_safe_task_context,
)
from app.agents.v2.turn_contract import (
    DateConstraint,
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import (
    interpret_customer_turn_v2,
    merge_presented_availability_context,
    merge_verified_read_context,
)
from app.integrations.clinic.base import ClinicAdapter
from app.models.patient import Patient
from app.models.workspace import Workspace
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    persist_initial_task_intent,
    plan_active_task_progress,
    prepare_active_task_clock_progress,
    resolve_pending_bare_clock_period,
)
from app.services.agent_v2.compound_turn_policy import (
    apply_compound_runtime_cursor,
    completed_compound_booking_end,
    compound_anchor_key,
    compound_write_group,
    normalize_compound_turn_plan,
    resolve_compound_followup_after_reads,
)
from app.services.agent_v2.compound_visit_preflight import preflight_compound_visit_plan
from app.services.agent_v2.grouped_booking_continuity import (
    enforce_grouped_booking_write_guard,
    explicitly_narrow_grouped_booking_task,
    grouped_booking_task_from_plan,
    restore_grouped_booking_understanding,
)
from app.services.agent_v2.grouped_write_transaction import (
    begin_group_savepoint,
    release_group_savepoint,
    rollback_group_savepoint,
)
from app.services.agent_v2.multi_service_booking import (
    MultiServiceBookingError,
    common_doctor_ids,
    common_laser_device_keys,
    laser_service_ids,
    resolve_additional_services,
    select_main_service_id,
    service_operational_category,
)
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import build_handoff_outcome, build_step_outcome
from app.services.agent_v2.planner import (
    PlannerContext,
    PlanStep,
    ReadRequest,
    TurnPlan,
    WriteIntent,
    plan_turn,
    resolve_same_turn_verified_service_dependency,
)
from app.services.agent_v2.read_executor import (
    ReadExecutionBundle,
    ReadExecutionContext,
    execute_step_reads,
)
from app.services.agent_v2.reference_resolution import (
    canonical_availability_slot_time,
    resolve_verified_availability_reference,
    resolve_verified_availability_scope,
)
from app.services.agent_v2.state import (
    ActiveTaskState,
    BookingProposal,
    BookingProposalVisit,
    BookingTaskState,
    CustomerConstraints,
    OptionChoice,
    OptionSnapshot,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import (
    apply_step_state,
    finalize_step_after_state_transition,
)
from app.services.agent_v2.state_persistence import (
    PersistedActiveTask,
    cancel_active_task,
    complete_active_task,
    load_active_task,
    save_active_task,
)
from app.services.agent_v2.state_rules import (
    apply_booking_branch_change,
    apply_booking_device_change,
    apply_booking_doctor_change,
    apply_verified_booking_slot_selection,
    attach_booking_proposal,
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
from app.services.agent_v2.write_policy import advance_step_with_write_policies

V2WriteExecutor = Callable[[PlanStep], dict[str, object]]


_SIDE_QUESTION_READ_TYPES = frozenset(
    {
        "service_info",
        "pricing",
        "doctor_info",
        "clinic_info",
        "payment_info",
        "customer_profile",
        "customer_history",
        "package_info",
        "package_compare",
        "pulse_info",
        "refund_quote",
    }
)


def _is_pure_informational_side_question(turn: TiaTurnUnderstanding) -> bool:
    return bool(turn.operations) and all(
        getattr(operation, "execution_intent", None) == "informational"
        and getattr(operation, "type", None) in _SIDE_QUESTION_READ_TYPES
        for operation in turn.operations
    )


class V2TurnInterpretationStructuredOutputError(RuntimeError):
    """Exhausted structured semantic interpretation for one customer turn."""


@dataclass(frozen=True)
class PendingV2Write:
    """Verified write boundary returned when no real executor is supplied."""

    step: PlanStep
    reads: ReadExecutionBundle


@dataclass(frozen=True)
class V2RuntimeStepTrace:
    operation_index: int
    operation_type: str
    disposition_before: str
    disposition_after: str
    read_kinds: tuple[str, ...]
    outcome: TurnOutcome | None = None
    verified_parameters: dict[str, object] | None = None
    pending_write: bool = False
    availability_slots: tuple[dict[str, object], ...] = ()
    skipped: bool = False
    skip_reason: str | None = None


@dataclass(frozen=True)
class V2OrchestratedTurn:
    understanding: TiaTurnUnderstanding
    plan: TurnPlan
    traces: tuple[V2RuntimeStepTrace, ...]
    outcomes: tuple[TurnOutcome, ...]
    reply: str | None
    responder_model: str | None
    active_task: ActiveTaskState | None
    persisted_task: PersistedActiveTask | None
    pending_write: PendingV2Write | None
    verified_action_context: dict[str, object] | None = None
    pending_choice: OptionSnapshot | None = None
    reference_semantic_path_used: bool = False
    full_interpreter_called: bool = True
    reference_action: str | None = None
    selected_option_ref: str | None = None
    reference_structured_output_error: bool = False


def _verified_availability_slots(reads: ReadExecutionBundle) -> tuple[dict[str, object], ...]:
    for result in reads.results:
        if result.kind != "availability" or not result.ok:
            continue
        raw = result.payload.get("slots")
        if isinstance(raw, list):
            return tuple(dict(item) for item in raw if isinstance(item, dict))
    return ()


def _service_names(catalog: dict[str, Any], service_ids: list[str]) -> list[str]:
    rows = catalog.get("services")
    by_id = {
        str(row.get("id")): str(row.get("name") or "")
        for row in rows if isinstance(row, dict) and row.get("id")
    } if isinstance(rows, list) else {}
    return [by_id.get(service_id, service_id) for service_id in service_ids]


def _proposal_slot(reads: ReadExecutionBundle, *, start_at: str, doctor_id: str) -> dict[str, object] | None:
    for slot in _verified_availability_slots(reads):
        if str(slot.get("start_at") or "") != start_at:
            continue
        if str(slot.get("doctor_id") or "") != doctor_id:
            continue
        return slot
    return None


def _proposal_matches_current_exact_time(
    current_task: ActiveTaskState | None,
    *,
    start_at: datetime,
    timezone_name: str,
) -> bool:
    if not isinstance(current_task, BookingTaskState):
        return True
    constraint = current_task.constraints.time
    if constraint is None or constraint.mode != "exact" or not constraint.start_time:
        return True
    try:
        local_time = start_at.astimezone(ZoneInfo(timezone_name)).strftime("%H:%M")
    except (ValueError, TypeError, KeyError):
        return False
    return local_time == str(constraint.start_time).strip()[:5]


def _prepare_booking_proposal(
    step: PlanStep,
    *,
    reads: ReadExecutionBundle,
    current_task: ActiveTaskState | None,
    db: Session,
    workspace: Workspace,
    patient: Patient,
    catalog: dict[str, Any],
    now: datetime,
    turn_id: str,
    additional_service_resolver: Callable[[BookingProposal], list[object]] | None = None,
) -> tuple[PlanStep, BookingProposal | None]:
    intent = step.write_intent
    requested_raw = step.facts.get("requested_service_ids")
    if (
        step.disposition != "write_ready"
        or intent is None
        or intent.kind != "booking"
        or step.facts.get("booking_proposal_required") is not True
        or not isinstance(requested_raw, list)
    ):
        return step, None
    requested = list(dict.fromkeys(str(item) for item in requested_raw if item not in (None, "")))
    params = dict(intent.parameters)
    main_service_id = str(params.get("service_id") or "")
    start_raw = str(params.get("start_at") or "")
    branch_id = str(params.get("branch_id") or "")
    doctor_id = str(params.get("doctor_id") or "")
    if not requested or main_service_id not in requested or not all((start_raw, branch_id, doctor_id)):
        return step.model_copy(
            update={
                "disposition": "blocked",
                "write_intent": None,
                "response_goal": "clarification",
                "facts": {**step.facts, "booking_proposal_invalid": True},
            }
        ), None
    requested_laser_ids = laser_service_ids(catalog, requested)
    common_device_keys = common_laser_device_keys(catalog, requested)
    visit_device_key = str(params.get("device_key") or "").strip() or None
    expected_main_service_id = select_main_service_id(
        catalog, requested, device_key=visit_device_key
    )
    if main_service_id != expected_main_service_id:
        return step.model_copy(
            update={
                "disposition": "blocked",
                "write_intent": None,
                "response_goal": "clarification",
                "facts": {
                    **step.facts,
                    "booking_proposal_main_service_mismatch": True,
                    "expected_main_service_id": expected_main_service_id,
                },
            }
        ), None
    if requested_laser_ids and (
        not common_device_keys or visit_device_key not in common_device_keys
    ):
        return step.model_copy(
            update={
                "disposition": "clarify",
                "write_intent": None,
                "clarification_field": "device",
                "response_goal": "clarification",
                "facts": {
                    **step.facts,
                    "multi_service_device_incompatible": True,
                    "compatible_visit_device_keys": list(common_device_keys),
                },
            }
        ), None
    if not requested_laser_ids and visit_device_key is not None:
        return step.model_copy(
            update={
                "disposition": "blocked",
                "write_intent": None,
                "response_goal": "clarification",
                "facts": {**step.facts, "booking_proposal_unexpected_device": True},
            }
        ), None
    compatible_doctors = common_doctor_ids(catalog, requested, branch_id=branch_id)
    if doctor_id not in compatible_doctors:
        return step.model_copy(
            update={
                "disposition": "clarify",
                "write_intent": None,
                "clarification_field": "doctor",
                "response_goal": "clarification",
                "facts": {
                    **step.facts,
                    "multi_service_doctor_incompatible": True,
                    "compatible_visit_doctor_ids": compatible_doctors,
                },
            }
        ), None
    slot = _proposal_slot(reads, start_at=start_raw, doctor_id=doctor_id)
    if slot is None or slot.get("end_at") in (None, ""):
        return step.model_copy(
            update={
                "disposition": "blocked",
                "write_intent": None,
                "response_goal": "requested_time_unavailable",
                "facts": {**step.facts, "booking_proposal_slot_missing": True},
            }
        ), None
    try:
        start_at = datetime.fromisoformat(start_raw)
        end_at = datetime.fromisoformat(str(slot["end_at"]))
    except ValueError:
        return step.model_copy(
            update={
                "disposition": "blocked",
                "write_intent": None,
                "response_goal": "clarification",
                "facts": {**step.facts, "booking_proposal_invalid_time": True},
            }
        ), None
    if not _proposal_matches_current_exact_time(
        current_task,
        start_at=start_at,
        timezone_name=getattr(workspace, "timezone", "Africa/Cairo"),
    ):
        return step.model_copy(
            update={
                "disposition": "blocked",
                "write_intent": None,
                "response_goal": "clarification",
                "facts": {
                    **step.facts,
                    "booking_proposal_time_constraint_mismatch": True,
                },
            }
        ), None

    previous_version = (
        current_task.proposal.proposal_version
        if isinstance(current_task, BookingTaskState) and current_task.proposal is not None
        else 0
    )
    proposal = BookingProposal(
        proposal_id=str(
            uuid5(
                NAMESPACE_URL,
                "tia-booking-proposal:"
                f"{workspace.id}:{patient.id}:{turn_id}:{','.join(requested)}:"
                f"{branch_id}:{doctor_id}:{start_at.isoformat()}",
            )
        ),
        patient_id=str(patient.id),
        service_ids=requested,
        main_service_id=main_service_id,
        additional_service_ids=[item for item in requested if item != main_service_id],
        branch_id=branch_id,
        doctor_id=doctor_id,
        start_at=start_at,
        end_at=end_at,
        device_key=visit_device_key,
        additional_service_device_keys={
            service_id: visit_device_key
            for service_id in requested_laser_ids
            if service_id != main_service_id and visit_device_key is not None
        },
        package_usage=str(params.get("package_usage") or "unspecified"),
        package_id=(str(params["package_id"]) if params.get("package_id") else None),
        task_version=max(getattr(current_task, "version", 1), 1),
        proposal_version=previous_version + 1,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    try:
        resolved = (
            additional_service_resolver(proposal)
            if additional_service_resolver is not None
            else resolve_additional_services(db, workspace=workspace, proposal=proposal)
        )
    except MultiServiceBookingError as exc:
        field = exc.dimension if exc.dimension in {"doctor", "device", "branch", "service"} else "service"
        return step.model_copy(
            update={
                "disposition": "clarify",
                "write_intent": None,
                "clarification_field": field,
                "response_goal": "clarification",
                "facts": {
                    **step.facts,
                    "additional_service_incompatible": True,
                    "compatibility_dimension": exc.dimension,
                },
            }
        ), None
    proposal = proposal.model_copy(
        update={
            "additional_service_device_keys": {
                str(item.service_id): item.device_key
                for item in resolved
                if item.device_key is not None
            }
        }
    )
    summary = {
        "services": _service_names(catalog, requested),
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "start_local": slot.get("start_local"),
        "doctor_name": slot.get("doctor_name"),
        "branch_name": slot.get("branch_name"),
        "device_name": slot.get("laser_device_name"),
    }
    return step.model_copy(
        update={
            "disposition": "respond",
            "write_intent": None,
            "response_goal": "booking_proposal",
            "facts": {**step.facts, "booking_proposal_summary": summary},
        }
    ), proposal


def _attach_verified_booking_proposal_to_task(
    task: BookingTaskState,
    *,
    proposal: BookingProposal,
    selected_option_ref: str,
    catalog: dict[str, Any],
    timezone_name: str,
) -> BookingTaskState:
    local_start = proposal.start_at.astimezone(ZoneInfo(timezone_name))
    proposal_devices = dict(task.service_device_keys)
    proposal_laser_ids = laser_service_ids(catalog, list(proposal.service_ids))
    if proposal.device_key is not None:
        proposal_devices.update(
            {service_id: proposal.device_key for service_id in proposal_laser_ids}
        )
    selected = apply_verified_booking_slot_selection(
        task,
        selected_slot_ref=selected_option_ref,
        main_service_id=proposal.main_service_id,
        branch_id=proposal.branch_id,
        doctor_id=proposal.doctor_id,
        device_key=proposal.device_key,
        date=DateConstraint(mode="exact", start_date=local_start.date().isoformat()),
        time=TimeConstraint(
            mode="exact",
            start_time=local_start.strftime("%H:%M"),
            start_time_ambiguity="none",
        ),
        service_device_keys=proposal_devices,
    )
    return attach_booking_proposal(selected, proposal=proposal)


def _prepare_cross_category_compound_proposal(
    plan: TurnPlan,
    *,
    current_task: BookingTaskState | None,
    workspace: Workspace,
    patient: Patient,
    catalog: dict[str, object],
    now: datetime,
    turn_id: str,
) -> tuple[TurnPlan, BookingTaskState | None]:
    bookings = [
        step
        for step in plan.steps
        if step.write_intent is not None
        and step.write_intent.kind == "booking"
        and step.facts.get("compound_visit_independent_category_resources") is True
        and compound_write_group(step) is not None
    ]
    if len(bookings) < 2:
        return plan, None
    group_keys = {compound_write_group(step) for step in bookings}
    if len(group_keys) != 1:
        return plan, None

    visits: list[BookingProposalVisit] = []
    all_services: list[str] = []
    service_devices: dict[str, str] = {}
    for step in bookings:
        assert step.write_intent is not None
        params = step.write_intent.parameters
        main_service_id = str(params.get("service_id") or "")
        branch_id = str(params.get("branch_id") or "")
        doctor_id = str(params.get("doctor_id") or "")
        start_raw = step.facts.get("compound_verified_start_at")
        end_raw = step.facts.get("compound_verified_end_at")
        if not all((main_service_id, branch_id, doctor_id, start_raw, end_raw)):
            return plan, None
        requested_raw = step.facts.get("requested_service_ids")
        requested = (
            list(dict.fromkeys(str(item) for item in requested_raw if item not in (None, "")))
            if isinstance(requested_raw, list) and requested_raw
            else [main_service_id]
        )
        categories = {service_operational_category(catalog, item) for item in requested}
        if len(requested) > 1 and (len(categories) != 1 or None in categories):
            return plan, None
        device_key = str(params.get("device_key") or "").strip() or None
        expected_main = select_main_service_id(
            catalog, requested, device_key=device_key
        )
        if main_service_id != expected_main:
            return plan, None
        compatible_doctors = common_doctor_ids(catalog, requested, branch_id=branch_id)
        if doctor_id not in compatible_doctors:
            return plan, None
        laser_ids = laser_service_ids(catalog, requested)
        if laser_ids:
            common_devices = common_laser_device_keys(catalog, requested)
            if device_key not in common_devices:
                return plan, None
        elif device_key is not None:
            return plan, None
        start_at = datetime.fromisoformat(str(start_raw))
        end_at = datetime.fromisoformat(str(end_raw))
        visit = BookingProposalVisit(
            service_ids=requested,
            main_service_id=main_service_id,
            additional_service_ids=[item for item in requested if item != main_service_id],
            branch_id=branch_id,
            doctor_id=doctor_id,
            start_at=start_at,
            end_at=end_at,
            device_key=device_key,
            additional_service_device_keys={
                item: device_key
                for item in laser_ids
                if item != main_service_id and device_key is not None
            },
            package_usage=str(params.get("package_usage") or "unspecified"),
            package_id=str(params["package_id"]) if params.get("package_id") else None,
        )
        visits.append(visit)
        all_services.extend(requested)
        if device_key is not None:
            service_devices.update({item: device_key for item in laser_ids})

    if len(all_services) != len(set(all_services)):
        return plan, None
    for previous, current in zip(visits, visits[1:], strict=False):
        if current.start_at < previous.end_at:
            return plan, None

    first = visits[0]
    proposal = BookingProposal(
        proposal_id=str(
            uuid5(
                NAMESPACE_URL,
                "tia-compound-category-proposal:"
                f"{workspace.id}:{patient.id}:{turn_id}:"
                + ";".join(
                    f"{','.join(visit.service_ids)}:{visit.start_at.isoformat()}"
                    for visit in visits
                ),
            )
        ),
        patient_id=str(patient.id),
        service_ids=all_services,
        main_service_id=first.main_service_id,
        additional_service_ids=first.additional_service_ids,
        branch_id=first.branch_id,
        doctor_id=first.doctor_id,
        start_at=first.start_at,
        end_at=first.end_at,
        device_key=first.device_key,
        additional_service_device_keys=first.additional_service_device_keys,
        package_usage=first.package_usage,
        package_id=first.package_id,
        visits=visits,
        task_version=max(getattr(current_task, "version", 1), 1),
        proposal_version=(
            current_task.proposal.proposal_version + 1
            if current_task is not None and current_task.proposal is not None
            else 1
        ),
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    first_params = bookings[0].write_intent.parameters if bookings[0].write_intent else {}
    constraints = CustomerConstraints.model_validate(
        {
            "service_id": first.main_service_id,
            "doctor_id": first.doctor_id,
            "branch_id": first.branch_id,
            "device_key": first.device_key,
            "date": first_params.get("date"),
            "time": first_params.get("time"),
            "package_usage": first.package_usage,
        }
    )
    if current_task is None:
        base_task = BookingTaskState(
            write_authorization=WriteAuthorization(operation="booking", authorized=False),
            constraints=constraints,
            requested_service_ids=all_services,
            service_device_keys=service_devices,
        )
    else:
        base_task = current_task.model_copy(
            update={
                "status": "collecting",
                "constraints": constraints,
                "requested_service_ids": all_services,
                "service_device_keys": service_devices,
                "grouped": None,
                "proposal": None,
                "option_snapshot": None,
            }
        )
    proposal_task = attach_booking_proposal(base_task, proposal=proposal)
    summary = {
        "services": _service_names(catalog, all_services),
        "visits": [
            {
                "services": _service_names(catalog, visit.service_ids),
                "start_at": visit.start_at.isoformat(),
                "end_at": visit.end_at.isoformat(),
                "doctor_id": visit.doctor_id,
                "branch_id": visit.branch_id,
                "device_key": visit.device_key,
            }
            for visit in visits
        ],
    }
    response_step = bookings[0].model_copy(
        update={
            "disposition": "respond",
            "reads": [],
            "write_intent": None,
            "state_action": "none",
            "response_goal": "booking_proposal",
            "facts": {
                **bookings[0].facts,
                "booking_proposal_summary": summary,
                "compound_category_proposal": True,
            },
        }
    )
    return plan.model_copy(update={"steps": [response_step]}), proposal_task


def _task_dict(active_task: ActiveTaskState | None) -> dict[str, Any] | None:
    return active_task.model_dump(mode="json") if active_task is not None else None



def _reference_option_time(
    decision: object,
    availability_context: dict[str, Any] | None,
) -> str | None:
    exact = str(getattr(decision, "exact_time", None) or "").strip()[:5]
    if exact:
        return exact
    if (
        getattr(decision, "action", None) != "select_presented_option"
        or getattr(decision, "selection_scope", "none") != "none"
    ):
        return None
    option_ref = str(getattr(decision, "option_ref", None) or "")
    raw_options = (availability_context or {}).get("availability_reference_options")
    if not option_ref or not isinstance(raw_options, list):
        return None
    matches = [
        item
        for item in raw_options
        if isinstance(item, dict) and str(item.get("option_ref") or "") == option_ref
    ]
    if len(matches) != 1:
        return None
    option = matches[0]
    slot = option.get("slot")
    if isinstance(slot, dict):
        clock = str(slot.get("start_time_24h") or "").strip()[:5]
        if clock:
            return clock
    return str(option.get("start_time_24h") or "").strip()[:5] or None


def _grouped_reference_date(
    availability_context: dict[str, Any] | None,
    *,
    exact_time: str,
) -> DateConstraint | None:
    raw_date = (availability_context or {}).get("date")
    if isinstance(raw_date, dict):
        try:
            date_constraint = DateConstraint.model_validate(raw_date)
        except ValueError:
            date_constraint = None
        if date_constraint is not None and date_constraint.mode == "exact":
            return date_constraint
    raw_options = (availability_context or {}).get("availability_reference_options")
    if not isinstance(raw_options, list):
        return None
    dates: set[str] = set()
    for item in raw_options:
        if not isinstance(item, dict):
            continue
        slot = item.get("slot")
        clock = str(item.get("start_time_24h") or "").strip()[:5]
        start_local = str(item.get("start_local") or "")
        if isinstance(slot, dict):
            clock = str(slot.get("start_time_24h") or clock).strip()[:5]
            start_local = str(slot.get("start_local") or start_local)
        if clock != exact_time or len(start_local) < 10:
            continue
        dates.add(start_local[:10])
    if len(dates) != 1:
        return None
    return DateConstraint(mode="exact", start_date=next(iter(dates)))


def _verified_booking_selection_plan(
    task: ActiveTaskState | None,
    *,
    decision: object | None,
    availability_context: dict[str, Any] | None,
    catalog: dict[str, Any],
    selection_time: str | None,
    now: datetime,
) -> tuple[TurnPlan, dict[str, Any]] | None:
    """Plan one fresh verification for a concrete server-owned booking slot."""
    if not isinstance(task, BookingTaskState) or task.status != "awaiting_slot":
        return None
    if (
        decision is None
        or getattr(decision, "action", None) != "select_presented_option"
        or getattr(decision, "selection_scope", "none") != "none"
    ):
        return None
    option_ref = str(getattr(decision, "option_ref", None) or "")
    if not option_ref:
        return None
    validated_context = _active_task_reference_option_context(
        task,
        option_ref=option_ref,
        availability_context=availability_context,
        explicit_user_time=selection_time,
        now=now,
    )
    if validated_context is None:
        return None
    resolution = resolve_verified_availability_reference(
        Selection(kind="ref", ref=option_ref),
        validated_context,
        explicit_user_time=selection_time,
    )
    if resolution.get("status") != "resolved":
        return None
    option = resolution.get("option")
    slot = option.get("slot") if isinstance(option, dict) else None
    if not isinstance(slot, dict):
        return None
    slot_time = canonical_availability_slot_time(slot)
    start_local = str(slot.get("start_local") or "")
    if slot_time is None or len(start_local) < 10:
        return None

    requested = list(task.requested_service_ids)
    if not requested and task.constraints.service_id is not None:
        requested = [task.constraints.service_id]
    requested = list(dict.fromkeys(requested))
    slot_service_id = str(slot.get("service_id") or "")
    if not requested or slot_service_id not in set(requested):
        return None
    if len(requested) > 1:
        context_services = validated_context.get("requested_service_ids")
        if not isinstance(context_services, list) or set(map(str, context_services)) != set(requested):
            return None

    branch_id = str(slot.get("branch_id") or task.constraints.branch_id or "")
    doctor_id = str(slot.get("doctor_id") or "")
    device_key = str(slot.get("laser_device_key") or slot.get("device_key") or "").strip() or None
    if not branch_id or not doctor_id:
        return None
    if doctor_id not in common_doctor_ids(catalog, requested, branch_id=branch_id):
        return (
            TurnPlan(
                steps=[
                    PlanStep(
                        operation_index=0,
                        operation_type="book",
                        disposition="clarify",
                        state_action="none",
                        response_goal="clarification",
                        clarification_field="doctor",
                        facts={
                            "verified_booking_slot_selection": True,
                            "verified_availability_reference": True,
                            "selected_option_ref": option_ref,
                            "multi_service_doctor_incompatible": True,
                        },
                    )
                ]
            ),
            validated_context,
        )
    laser_ids = laser_service_ids(catalog, requested)
    if laser_ids:
        common_devices = common_laser_device_keys(catalog, requested)
        if device_key is None or device_key not in common_devices:
            return (
                TurnPlan(
                    steps=[
                        PlanStep(
                            operation_index=0,
                            operation_type="book",
                            disposition="clarify",
                            state_action="none",
                            response_goal="clarification",
                            clarification_field="device",
                            facts={
                                "verified_booking_slot_selection": True,
                                "verified_availability_reference": True,
                                "selected_option_ref": option_ref,
                                "multi_service_device_incompatible": True,
                                "compatible_visit_device_keys": list(common_devices),
                            },
                        )
                    ]
                ),
                validated_context,
            )
    elif device_key is not None:
        return None

    main_service_id = select_main_service_id(catalog, requested, device_key=device_key)
    date_constraint = DateConstraint(mode="exact", start_date=start_local[:10])
    time_constraint = TimeConstraint(
        mode="exact",
        start_time=slot_time,
        start_time_ambiguity="none",
    )
    params: dict[str, object] = {
        "service_id": main_service_id,
        "branch_id": branch_id,
        "doctor_id": doctor_id,
        "date": date_constraint.model_dump(mode="json"),
        "time": time_constraint.model_dump(mode="json"),
        "package_usage": task.constraints.package_usage,
    }
    if device_key is not None:
        params["device_key"] = device_key
    if task.derived.selected_package_id is not None:
        params["package_id"] = task.derived.selected_package_id

    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        reads=[ReadRequest(kind="availability", parameters=dict(params))],
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters=dict(params),
            requires_verification=True,
        ),
        state_action="select_active",
        response_goal="present_availability",
        facts={
            **params,
            "requested_service_ids": requested,
            "booking_proposal_required": True,
            "exact_time_requested": True,
            "service_requires_laser_device": bool(laser_ids),
            "multi_service_single_appointment": len(requested) > 1,
            "verified_booking_slot_selection": True,
            "verified_availability_reference": True,
            "selected_option_ref": option_ref,
            "verified_selection_date": date_constraint.model_dump(mode="json"),
            "verified_selection_time": time_constraint.model_dump(mode="json"),
        },
    )
    return TurnPlan(steps=[step]), validated_context


def _grouped_visit_reference_understanding(
    task: ActiveTaskState | None,
    *,
    decision: object | None,
    availability_context: dict[str, Any] | None,
    catalog: dict[str, Any],
) -> tuple[TiaTurnUnderstanding, BookingTaskState] | None:
    if not isinstance(task, BookingTaskState) or len(task.requested_service_ids) < 2:
        return None
    if decision is None or getattr(decision, "action", None) not in {
        "clarify",
        "select_presented_option",
    }:
        return None
    context_services = (availability_context or {}).get("requested_service_ids")
    if not isinstance(context_services, list) or set(map(str, context_services)) != set(
        task.requested_service_ids
    ):
        return None
    exact_time = _reference_option_time(decision, availability_context)
    if exact_time is None:
        return None
    date_constraint = _grouped_reference_date(
        availability_context,
        exact_time=exact_time,
    )
    if date_constraint is None:
        return None

    raw_options = (availability_context or {}).get("availability_reference_options")
    if not isinstance(raw_options, list):
        return None
    requested = list(task.requested_service_ids)
    requested_set = set(requested)
    selected_ref = (
        str(getattr(decision, "option_ref", None) or "")
        if getattr(decision, "action", None) == "select_presented_option"
        else ""
    )
    verified_slots: list[dict[str, object]] = []
    for option in raw_options:
        if not isinstance(option, dict):
            continue
        option_ref = str(option.get("option_ref") or "")
        if not option_ref or (selected_ref and option_ref != selected_ref):
            continue
        resolution = resolve_verified_availability_reference(
            Selection(kind="ref", ref=option_ref),
            availability_context,
            explicit_user_time=exact_time,
        )
        if resolution.get("status") != "resolved":
            continue
        resolved_option = resolution.get("option")
        slot = resolved_option.get("slot") if isinstance(resolved_option, dict) else None
        if not isinstance(slot, dict):
            continue
        slot_time = canonical_availability_slot_time(slot)
        start_local = str(slot.get("start_local") or slot.get("start_at") or "")
        if slot_time != exact_time or len(start_local) < 10:
            continue
        if date_constraint.start_date and start_local[:10] != date_constraint.start_date:
            continue
        service_id = str(slot.get("service_id") or "")
        if service_id not in requested_set:
            return None
        verified_slots.append(slot)
    if not verified_slots:
        return None

    branch_ids = {
        str(slot.get("branch_id"))
        for slot in verified_slots
        if slot.get("branch_id") not in (None, "")
    }
    doctor_ids = {
        str(slot.get("doctor_id"))
        for slot in verified_slots
        if slot.get("doctor_id") not in (None, "")
    }
    device_keys = {
        str(slot.get("laser_device_key") or slot.get("device_key"))
        for slot in verified_slots
        if (slot.get("laser_device_key") or slot.get("device_key")) not in (None, "")
    }
    if len(branch_ids) > 1 or len(doctor_ids) != 1:
        return None
    branch_id = next(iter(branch_ids)) if branch_ids else task.constraints.branch_id
    doctor_id = next(iter(doctor_ids))
    if doctor_id not in common_doctor_ids(catalog, requested, branch_id=branch_id):
        return None

    laser_ids = laser_service_ids(catalog, requested)
    device_key: str | None = None
    if laser_ids:
        if len(device_keys) != 1:
            return None
        device_key = next(iter(device_keys))
        if device_key not in common_laser_device_keys(catalog, requested):
            return None
    elif device_keys:
        return None

    resource_task = task
    if branch_id is not None:
        resource_task = apply_booking_branch_change(resource_task, branch_id=branch_id)
    resource_task = apply_booking_doctor_change(resource_task, doctor_id=doctor_id)
    if device_key is not None:
        resource_task = apply_booking_device_change(resource_task, device_key=device_key)
        resource_task = resource_task.model_copy(
            update={
                "service_device_keys": {
                    **resource_task.service_device_keys,
                    **{service_id: device_key for service_id in laser_ids},
                }
            }
        )

    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    date=date_constraint,
                    time=TimeConstraint(
                        mode="exact",
                        start_time=exact_time,
                        start_time_ambiguity="none",
                    ),
                ),
                execution_intent="informational",
                active_task_relationship="continue",
                grouped_booking_action="preserve_group",
                continues_previous=True,
            )
        ]
    )
    return understanding, resource_task


def _terminal_no_reply_allowed(
    understanding: TiaTurnUnderstanding,
    *,
    active_task: ActiveTaskState | None,
    pending_choice: OptionSnapshot | None,
    automation_context: dict[str, Any] | None = None,
) -> bool:
    """Fail closed: semantic no-reply is valid only after conversational work is finished."""
    # Compatibility/test doubles and future semantic variants must fail closed:
    # silence is opt-in only when the typed semantic marker is explicitly present.
    if getattr(understanding, "response_disposition", None) != "no_reply":
        return False
    if active_task is not None or pending_choice is not None or understanding.safety_signals:
        return False
    if automation_context is not None:
        return False
    if not understanding.operations:
        return True
    if any(operation.type != "social" for operation in understanding.operations):
        return False
    return all(
        operation.automation_context_relationship == "none"
        for operation in understanding.operations
    )


def _availability_slot_identity(payload: dict[str, object]) -> tuple[str, ...]:
    device = payload.get("device_key") or payload.get("laser_device_key")
    start = payload.get("start_at") or payload.get("start_local")
    return tuple(
        str(value or "")
        for value in (
            payload.get("branch_id"),
            payload.get("service_id"),
            payload.get("doctor_id"),
            device,
            start,
        )
    )


def _active_task_reference_option_context(
    task: ActiveTaskState,
    *,
    option_ref: str,
    availability_context: dict[str, Any] | None,
    explicit_user_time: str | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Validate a displayed ref against the current task snapshot.

    A concrete displayed option must match exactly one current snapshot slot. A
    compressed server-owned window may be narrowed only by an explicit HH:MM that
    resolves to exactly one current snapshot slot inside that same displayed window.
    """
    snapshot = task.option_snapshot
    if snapshot is None or not snapshot.is_current(task_version=task.version, now=now):
        return None
    context = dict(availability_context or {})
    raw_options = context.get("availability_reference_options")
    if not isinstance(raw_options, list):
        return None
    indexed_matches = [
        (index, item)
        for index, item in enumerate(raw_options)
        if isinstance(item, dict) and item.get("option_ref") == option_ref
    ]
    if len(indexed_matches) != 1:
        return None
    option_index, option = indexed_matches[0]
    if option.get("concrete") is True:
        slot = option.get("slot")
        if not isinstance(slot, dict):
            return None
        identity = _availability_slot_identity(slot)
        snapshot_matches = [
            choice
            for choice in snapshot.options
            if _availability_slot_identity(choice.payload) == identity
        ]
        return context if len(snapshot_matches) == 1 else None

    explicit = str(explicit_user_time or "").strip()[:5]
    if not explicit:
        return None
    try:
        window_start = datetime.fromisoformat(str(option.get("start_local") or ""))
        window_end = datetime.fromisoformat(str(option.get("end_local") or ""))
    except ValueError:
        return None

    candidates: list[OptionChoice] = []
    for choice in snapshot.options:
        payload = choice.payload
        try:
            slot_start = datetime.fromisoformat(str(payload.get("start_local") or ""))
        except ValueError:
            continue
        if not window_start <= slot_start <= window_end:
            continue
        if slot_start.strftime("%H:%M") != explicit:
            continue
        if option.get("doctor_name") not in (None, "") and str(
            payload.get("doctor_name") or ""
        ) != str(option.get("doctor_name")):
            continue
        if option.get("laser_device_name") not in (None, "") and str(
            payload.get("laser_device_name") or ""
        ) != str(option.get("laser_device_name")):
            continue
        candidates.append(choice)
    if len(candidates) != 1:
        return None

    selected_payload = dict(candidates[0].payload)
    if selected_payload.get("device_key") not in (None, ""):
        selected_payload.setdefault("laser_device_key", selected_payload["device_key"])
    materialized = dict(option)
    materialized.update(
        {
            "concrete": True,
            "slot": selected_payload,
            "start_local": selected_payload.get("start_local"),
            "end_local": selected_payload.get("start_local"),
            "start_time_24h": selected_payload.get("start_time_24h") or explicit,
            "end_time_24h": selected_payload.get("start_time_24h") or explicit,
        }
    )
    updated_options = list(raw_options)
    updated_options[option_index] = materialized
    context["availability_reference_options"] = updated_options
    return context


def _active_task_reference_window_facts(
    task: ActiveTaskState,
    *,
    option_ref: str,
    availability_context: dict[str, Any] | None,
    now: datetime,
) -> dict[str, object] | None:
    """Narrow a current compressed availability window without inventing a slot."""
    snapshot = task.option_snapshot
    if snapshot is None or not snapshot.is_current(task_version=task.version, now=now):
        return None
    raw_options = (availability_context or {}).get("availability_reference_options")
    if not isinstance(raw_options, list):
        return None
    matches = [
        item
        for item in raw_options
        if isinstance(item, dict) and item.get("option_ref") == option_ref
    ]
    if len(matches) != 1 or matches[0].get("concrete") is True:
        return None
    option = matches[0]
    try:
        window_start = datetime.fromisoformat(str(option.get("start_local") or ""))
        window_end = datetime.fromisoformat(str(option.get("end_local") or ""))
    except ValueError:
        return None

    candidates: list[OptionChoice] = []
    for choice in snapshot.options:
        payload = choice.payload
        try:
            slot_start = datetime.fromisoformat(str(payload.get("start_local") or ""))
        except ValueError:
            continue
        if not window_start <= slot_start <= window_end:
            continue
        if option.get("doctor_name") not in (None, "") and str(
            payload.get("doctor_name") or ""
        ) != str(option.get("doctor_name")):
            continue
        if option.get("laser_device_name") not in (None, "") and str(
            payload.get("laser_device_name") or ""
        ) != str(option.get("laser_device_name")):
            continue
        candidates.append(choice)
    if not candidates:
        return None

    facts: dict[str, object] = {}
    doctor_ids = {
        str(choice.payload.get("doctor_id"))
        for choice in candidates
        if choice.payload.get("doctor_id") not in (None, "")
    }
    if option.get("doctor_name") not in (None, ""):
        if len(doctor_ids) != 1:
            return None
        facts["doctor_id"] = next(iter(doctor_ids))
    device_keys = {
        str(choice.payload.get("device_key") or choice.payload.get("laser_device_key"))
        for choice in candidates
        if (choice.payload.get("device_key") or choice.payload.get("laser_device_key"))
        not in (None, "")
    }
    if option.get("laser_device_name") not in (None, ""):
        if len(device_keys) != 1:
            return None
        facts["device_key"] = next(iter(device_keys))
    return facts or None

def _verified_pending_reference_time(
    task: ActiveTaskState,
    *,
    availability_context: dict[str, Any] | None,
    exact_time: str | None,
    now: datetime,
) -> str | None:
    """Keep an explicit semantic clock only while multiple current displayed slots share it."""
    snapshot = task.option_snapshot
    if snapshot is None or not snapshot.is_current(task_version=task.version, now=now):
        return None
    explicit = str(exact_time or "").strip()[:5]
    try:
        datetime.strptime(explicit, "%H:%M")
    except ValueError:
        return None
    raw_options = (availability_context or {}).get("availability_reference_options")
    if not isinstance(raw_options, list):
        return None

    identities: set[tuple[str, ...]] = set()
    for choice in snapshot.options:
        payload = choice.payload
        try:
            slot_start = datetime.fromisoformat(str(payload.get("start_local") or ""))
        except ValueError:
            continue
        if slot_start.strftime("%H:%M") != explicit:
            continue
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            try:
                window_start = datetime.fromisoformat(str(option.get("start_local") or ""))
                window_end = datetime.fromisoformat(str(option.get("end_local") or ""))
            except ValueError:
                continue
            if not window_start <= slot_start <= window_end:
                continue
            if option.get("doctor_name") not in (None, "") and str(
                payload.get("doctor_name") or ""
            ) != str(option.get("doctor_name")):
                continue
            if option.get("laser_device_name") not in (None, "") and str(
                payload.get("laser_device_name") or ""
            ) != str(option.get("laser_device_name")):
                continue
            identities.add(_availability_slot_identity(payload))
            break
    return explicit if len(identities) >= 2 else None


def _active_task_reference_option_is_current(
    task: ActiveTaskState,
    *,
    option_ref: str,
    availability_context: dict[str, Any] | None,
    now: datetime,
) -> bool:
    return _active_task_reference_option_context(
        task,
        option_ref=option_ref,
        availability_context=availability_context,
        explicit_user_time=None,
        now=now,
    ) is not None


def _pending_choice_from_context(
    value: dict[str, Any] | None,
    *,
    now: datetime,
) -> OptionSnapshot | None:
    if not isinstance(value, dict):
        return None
    try:
        snapshot = OptionSnapshot.model_validate(value)
    except ValueError:
        return None
    if now >= snapshot.expires_at:
        return None
    if snapshot.purpose not in {"appointment", "appointment_target"}:
        return None
    return snapshot


def _appointment_choice_snapshot(
    *,
    step: PlanStep,
    outcome: TurnOutcome,
    now: datetime,
    turn_id: str,
) -> OptionSnapshot | None:
    if (
        step.response_goal != "ask_appointment_choice"
        or step.operation_type not in {"cancel_appointment", "confirm_appointment", "reschedule"}
    ):
        return None
    choices: list[OptionChoice] = []
    for choice in outcome.choices:
        appointment_id = choice.facts.get("appointment_id")
        if appointment_id in (None, ""):
            continue
        choices.append(
            OptionChoice(
                ref=choice.ref,
                label=choice.label,
                payload={"appointment_id": str(appointment_id)},
            )
        )
    if not choices:
        return None
    return OptionSnapshot(
        snapshot_id=f"{turn_id}:{step.operation_index}:appointment-choice",
        purpose="appointment",
        lifecycle_action=step.operation_type,
        task_version=1,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        options=choices,
    )


def _selected_appointment_snapshot(
    *,
    step: PlanStep,
    now: datetime,
    turn_id: str,
) -> OptionSnapshot | None:
    if step.operation_type != "select_active":
        return None
    action = step.facts.get("lifecycle_action")
    selected = step.facts.get("selected_option")
    if action not in {"cancel_appointment", "confirm_appointment", "reschedule"}:
        return None
    if not isinstance(selected, dict):
        return None
    payload = selected.get("payload")
    if not isinstance(payload, dict) or payload.get("appointment_id") in (None, ""):
        return None
    choice = OptionChoice(
        ref=str(selected.get("ref") or "appointment-target"),
        label=str(selected.get("label")) if selected.get("label") not in (None, "") else None,
        payload={"appointment_id": str(payload["appointment_id"])},
    )
    return OptionSnapshot(
        snapshot_id=f"{turn_id}:{step.operation_index}:appointment-target",
        purpose="appointment_target",
        lifecycle_action=action,
        task_version=1,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        options=[choice],
    )


def _operation_for_step(
    understanding: TiaTurnUnderstanding,
    step: PlanStep,
) -> TurnOperation:
    try:
        return understanding.operations[step.operation_index]
    except IndexError as exc:
        raise RuntimeError("V2 plan referenced an operation outside the structured turn.") from exc


def _advance_after_reads(step: PlanStep, reads: ReadExecutionBundle) -> PlanStep:
    if not step.reads:
        return step
    if step.write_intent is None and step.state_action != "start_reschedule":
        return step
    return advance_step_with_write_policies(step, reads)


def _auto_resolve_single_booking_device(
    step: PlanStep,
    reads: ReadExecutionBundle,
) -> PlanStep:
    if (
        step.facts.get("booking_device_price_step") is not True
        or step.facts.get("device_key") not in (None, "")
    ):
        return step
    candidates: list[dict[str, object]] = []
    for result in reads.results:
        if not result.ok:
            continue
        if result.kind == "service_catalog_set":
            variants = result.payload.get("variants")
            compatible = step.facts.get("compatible_visit_device_keys")
            if (
                result.payload.get("device_required") is True
                and isinstance(variants, list)
                and len(variants) == 1
                and isinstance(compatible, list)
                and len(compatible) == 1
                and isinstance(variants[0], dict)
            ):
                # The set read intentionally exposes a complete-visit price variant,
                # not per-service device rows. Its canonical compatible-device fact
                # supplies the key for the sole fully priced variant.
                candidates.append(
                    {
                        "device_key": compatible[0],
                        "device_name": variants[0].get("device_name"),
                        "price_minor": variants[0].get("total_price_minor"),
                    }
                )
            continue
        if result.kind != "service_catalog":
            continue
        service = result.payload.get("service")
        if not isinstance(service, dict):
            continue
        raw_devices = service.get("laser_devices")
        if not isinstance(raw_devices, list):
            continue
        candidates.extend(
            device
            for device in raw_devices
            if isinstance(device, dict)
            and device.get("configured") is not False
            and device.get("device_key") not in (None, "")
            and (
                device.get("price_minor") is not None
                or device.get("price") not in (None, "")
            )
        )
    compatible = step.facts.get("compatible_visit_device_keys")
    allowed = (
        {str(item) for item in compatible if item not in (None, "")}
        if isinstance(compatible, list)
        else None
    )
    unique: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        key = str(candidate.get("device_key") or "").strip()
        if not key or (allowed is not None and key not in allowed):
            continue
        unique.setdefault(key, candidate)
    candidates = list(unique.values())
    if len(candidates) != 1:
        return step
    return step.model_copy(
        update={
            "facts": {
                **step.facts,
                "device_key": str(candidates[0]["device_key"]),
                # Device resolution must not rewrite booking progression. The planner
                # already owns whether the next field is date vs booking based on the
                # customer's canonical constraints and same-turn availability read.
                "device_auto_resolved": True,
            }
        }
    )


def _compatibility_failure_step(
    step: PlanStep,
    reads: ReadExecutionBundle,
) -> PlanStep | None:
    """Turn a canonically proven compatibility read failure into safe continuation."""
    for result in reads.results:
        if result.kind != "availability" or result.ok:
            continue
        failure = result.payload.get("compatibility_failure")
        if not isinstance(failure, dict):
            continue
        dimension = failure.get("dimension")
        if dimension not in {"doctor", "device"}:
            continue

        facts = {
            **step.facts,
            "compatibility_failure": dict(failure),
            "doctor_id" if dimension == "doctor" else "device_key": None,
        }
        state_action = step.state_action
        if state_action == "none" and step.operation_type == "book":
            state_action = "start_booking"
        return step.model_copy(
            update={
                "disposition": "clarify",
                "write_intent": None,
                "state_action": state_action,
                "response_goal": "clarification",
                "clarification_field": str(dimension),
                "facts": facts,
            }
        )
    return None


def _verified_no_availability(reads: ReadExecutionBundle | None) -> bool:
    """True only for a successful availability read that canonically found zero options."""
    if reads is None:
        return False
    availability_results = [result for result in reads.results if result.kind == "availability"]
    if not availability_results:
        return False
    for result in availability_results:
        if not result.ok:
            return False
        count = result.payload.get("matching_slot_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
        if count > 0:
            return False
    return True


def _availability_presentation_continuation(
    understanding: TiaTurnUnderstanding,
    plan: TurnPlan,
    recent_read_context: dict[str, Any] | None,
) -> bool:
    for step in reversed(plan.steps):
        if step.operation_index >= len(understanding.operations):
            continue
        operation = understanding.operations[step.operation_index]
        if (
            not bool(getattr(operation, "continues_previous", False))
            or operation.type not in {"availability", "book", "reschedule"}
        ):
            continue
        current_scope_key = availability_scope_key_from_reads(step.reads)
        return availability_scope_matches(
            recent_read_context,
            current_scope_key=current_scope_key,
        )
    return False


def _availability_shown_window_keys(
    recent_read_context: dict[str, Any] | None,
) -> set[str]:
    if not isinstance(recent_read_context, dict):
        return set()
    raw = recent_read_context.get("availability_presented_window_keys")
    if not isinstance(raw, list):
        return set()
    return {str(value) for value in raw if isinstance(value, str) and value}


def _verified_recent_no_availability(
    recent_read_context: dict[str, Any] | None,
) -> bool:
    """Accept only the immediately previous server-owned verified zero-availability summary."""
    if not isinstance(recent_read_context, dict):
        return False
    if recent_read_context.get("operation_type") not in {
        "availability",
        "book",
        "reschedule",
    }:
        return False
    option_count = recent_read_context.get("availability_option_count")
    return (
        isinstance(option_count, int)
        and not isinstance(option_count, bool)
        and option_count == 0
    )


def _recent_zero_availability_exact_date(
    recent_read_context: dict[str, Any] | None,
) -> datetime | None:
    if not _verified_recent_no_availability(recent_read_context):
        return None
    if not isinstance(recent_read_context, dict):
        return None
    if recent_read_context.get("time") not in (None, "", {}, []):
        return None
    raw_date = recent_read_context.get("date")
    if isinstance(raw_date, dict):
        if raw_date.get("mode") != "exact":
            return None
        raw_value = raw_date.get("start_date")
    else:
        raw_value = raw_date
    if not isinstance(raw_value, str) or not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def _normalize_cross_turn_nearest_after_verified_miss(
    understanding: TiaTurnUnderstanding,
    *,
    recent_read_context: dict[str, Any] | None,
) -> TiaTurnUnderstanding:
    """Advance typed nearest semantics beyond an immediately verified date-level miss."""
    if not understanding.operations:
        return understanding
    operation = understanding.operations[0]
    if not bool(getattr(operation, "continues_previous", False)):
        return understanding
    if operation.type not in {"continue_active", "availability", "book", "reschedule"}:
        return understanding
    time_constraint = operation.entities.time
    condition = getattr(operation, "continuation_condition", "always")
    if time_constraint is not None and time_constraint.mode != "nearest":
        return understanding
    if time_constraint is None and condition != "if_previous_no_availability":
        return understanding
    missed = _recent_zero_availability_exact_date(recent_read_context)
    if missed is None:
        return understanding
    current_date = operation.entities.date
    missed_iso = missed.date().isoformat()
    if current_date is not None and (
        current_date.mode != "exact" or current_date.start_date != missed_iso
    ):
        return understanding
    next_date = (missed.date() + timedelta(days=1)).isoformat()
    entities = operation.entities.model_copy(
        update={
            "date": DateConstraint(
                mode="from_date",
                start_date=next_date,
                end_date=None,
            )
        }
    )
    normalized = operation.model_copy(update={"entities": entities})
    return understanding.model_copy(
        update={"operations": [normalized, *understanding.operations[1:]]}
    )


def _continuation_condition_satisfied(
    operation: TurnOperation,
    *,
    previous_reads: ReadExecutionBundle | None,
    recent_read_context: dict[str, Any] | None = None,
) -> bool:
    condition = getattr(operation, "continuation_condition", "always")
    if condition == "always":
        return True
    if condition == "if_previous_no_availability":
        if previous_reads is not None:
            return _verified_no_availability(previous_reads)
        if not bool(getattr(operation, "continues_previous", False)):
            return False
        return _verified_recent_no_availability(recent_read_context)
    raise RuntimeError(f"Unsupported continuation condition: {condition}")


def _completed_action_context(
    step: PlanStep,
    action_result: dict[str, object],
) -> dict[str, object] | None:
    """Capture only canonical facts needed for the immediately following turn."""
    intent = step.write_intent
    if (
        step.disposition != "write_ready"
        or intent is None
        or action_result.get("ok") is not True
    ):
        return None

    parameters = dict(intent.parameters)
    if intent.kind == "buy_pulse_pack":
        device_key = parameters.get("device_key")
        if device_key in (None, ""):
            return None
        context: dict[str, object] = {
            "operation_type": "buy_pulse_pack",
            "device_key": str(device_key),
        }
        pulse_count = parameters.get("pulse_count")
        if isinstance(pulse_count, int) and not isinstance(pulse_count, bool) and pulse_count > 0:
            context["pulse_count"] = pulse_count
        return context

    if intent.kind == "booking_proposal":
        raw_proposal = parameters.get("proposal")
        if not isinstance(raw_proposal, dict):
            return None
        appointment_id = action_result.get("appointment_id")
        status = action_result.get("status")
        required = ("main_service_id", "doctor_id", "start_at")
        if (
            appointment_id in (None, "")
            or status not in {"pending", "confirmed"}
            or any(raw_proposal.get(key) in (None, "") for key in required)
        ):
            return None
        context: dict[str, object] = {
            "operation_type": "book",
            "appointment_id": str(appointment_id),
            "service_id": str(raw_proposal["main_service_id"]),
            "doctor_id": str(raw_proposal["doctor_id"]),
            "start_at": str(raw_proposal["start_at"]),
            "status": str(status),
            "package_usage": str(raw_proposal.get("package_usage") or "unspecified"),
        }
        additional = raw_proposal.get("additional_service_ids")
        if isinstance(additional, list):
            context["additional_service_ids"] = [str(item) for item in additional]
        if raw_proposal.get("device_key") not in (None, ""):
            context["device_key"] = str(raw_proposal["device_key"])
        return context

    if intent.kind == "booking":
        required = ("service_id", "doctor_id", "start_at")
        if any(parameters.get(key) in (None, "") for key in required):
            return None
        appointment_id = action_result.get("appointment_id")
        status = action_result.get("status")
        if appointment_id in (None, "") or status not in {"pending", "confirmed"}:
            return None
        context = {
            "operation_type": "book",
            "appointment_id": str(appointment_id),
            "service_id": str(parameters["service_id"]),
            "doctor_id": str(parameters["doctor_id"]),
            "start_at": str(parameters["start_at"]),
            "status": str(status),
            "package_usage": str(parameters.get("package_usage") or "unspecified"),
        }
        if parameters.get("device_key") not in (None, ""):
            context["device_key"] = str(parameters["device_key"])
        for key in ("date", "time"):
            if isinstance(parameters.get(key), dict):
                context[key] = dict(parameters[key])
        return context

    if intent.kind == "reschedule":
        appointment_id = action_result.get("appointment_id")
        previous_appointment_id = action_result.get("previous_appointment_id")
        status = action_result.get("status")
        required = ("service_id", "doctor_id", "start_at")
        if (
            appointment_id in (None, "")
            or previous_appointment_id in (None, "")
            or status not in {"pending", "confirmed"}
            or any(parameters.get(key) in (None, "") for key in required)
        ):
            return None
        context = {
            "operation_type": "reschedule",
            "appointment_id": str(appointment_id),
            "previous_appointment_id": str(previous_appointment_id),
            "service_id": str(parameters["service_id"]),
            "doctor_id": str(parameters["doctor_id"]),
            "start_at": str(parameters["start_at"]),
            "status": str(status),
        }
        if parameters.get("device_key") not in (None, ""):
            context["device_key"] = str(parameters["device_key"])
        for key in ("date", "time"):
            if isinstance(parameters.get(key), dict):
                context[key] = dict(parameters[key])
        return context

    if intent.kind == "cancel_appointment":
        appointment_id = action_result.get("appointment_id")
        if appointment_id in (None, "") or action_result.get("status") != "cancelled":
            return None
        return {
            "operation_type": "cancel_appointment",
            "appointment_id": str(appointment_id),
            "status": "cancelled",
        }
    return None


def _exact_requested_start_at(step: PlanStep, *, timezone_name: str) -> datetime | None:
    date = step.facts.get("date")
    time = step.facts.get("time")
    if not isinstance(date, dict) or not isinstance(time, dict):
        return None
    if date.get("mode") != "exact" or time.get("mode") != "exact":
        return None
    start_date = date.get("start_date")
    start_time = time.get("start_time")
    if not isinstance(start_date, str) or not isinstance(start_time, str):
        return None
    try:
        local = datetime.fromisoformat(f"{start_date}T{start_time}")
        return local.replace(tzinfo=ZoneInfo(timezone_name))
    except (ValueError, KeyError):
        return None


def _same_recent_booking(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object],
    *,
    timezone_name: str,
) -> bool:
    if (
        step.operation_type != "book"
        or not operation.continues_previous
        or recent_action.get("operation_type") != "book"
        or recent_action.get("status") not in {"pending", "confirmed"}
    ):
        return False
    expected_start = _exact_requested_start_at(step, timezone_name=timezone_name)
    recent_start_raw = recent_action.get("start_at")
    if expected_start is None or not isinstance(recent_start_raw, str):
        return False
    try:
        recent_start = datetime.fromisoformat(recent_start_raw)
    except ValueError:
        return False
    if recent_start.tzinfo is None or recent_start.utcoffset() is None:
        return False
    if expected_start.astimezone(recent_start.tzinfo) != recent_start:
        return False

    def same(key: str) -> bool:
        current = step.facts.get(key)
        recent = recent_action.get(key)
        return str(current) == str(recent) if current not in (None, "") or recent not in (None, "") else True

    return (
        same("service_id")
        and same("doctor_id")
        and same("device_key")
        and str(step.facts.get("package_usage") or "unspecified")
        == str(recent_action.get("package_usage") or "unspecified")
    )


def _same_recent_reschedule(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object],
    *,
    timezone_name: str,
) -> bool:
    if (
        step.operation_type != "reschedule"
        or recent_action.get("operation_type") != "reschedule"
        or recent_action.get("status") not in {"pending", "confirmed"}
    ):
        return False
    if operation.selection is not None:
        return False
    appointment = operation.entities.appointment
    if appointment is not None and (
        appointment.ref is not None
        or appointment.text not in (None, "")
        or appointment.candidate_refs
    ):
        return False

    expected_start = _exact_requested_start_at(step, timezone_name=timezone_name)
    recent_start_raw = recent_action.get("start_at")
    if expected_start is None or not isinstance(recent_start_raw, str):
        return False
    try:
        recent_start = datetime.fromisoformat(recent_start_raw)
    except ValueError:
        return False
    if recent_start.tzinfo is None or recent_start.utcoffset() is None:
        return False
    if expected_start.astimezone(recent_start.tzinfo) != recent_start:
        return False

    def compatible(key: str) -> bool:
        current = step.facts.get(key)
        recent = recent_action.get(key)
        return current in (None, "") or str(current) == str(recent)

    if not all(compatible(key) for key in ("service_id", "doctor_id", "device_key")):
        return False

    source = step.facts.get("source_appointment")
    if isinstance(source, dict):
        source_id = source.get("appointment_id")
        if source_id not in (None, "") and str(source_id) != str(
            recent_action.get("appointment_id")
        ):
            return False
        for key in ("service_id", "doctor_id", "device_key"):
            value = source.get(key)
            if value not in (None, "") and str(value) != str(recent_action.get(key)):
                return False
    return True


def _recent_reschedule_validation_request(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object] | None,
    *,
    timezone_name: str,
) -> ReadRequest | None:
    if not isinstance(recent_action, dict):
        return None
    if not _same_recent_reschedule(
        step,
        operation,
        recent_action,
        timezone_name=timezone_name,
    ):
        return None
    appointment_id = recent_action.get("appointment_id")
    if appointment_id in (None, ""):
        return None
    return ReadRequest(
        kind="appointments",
        parameters={"appointment_id": str(appointment_id)},
    )


def _canonical_recent_reschedule_is_current(
    recent_action: dict[str, object] | None,
    reads: ReadExecutionBundle,
) -> bool:
    if not isinstance(recent_action, dict):
        return False
    appointment_id = recent_action.get("appointment_id")
    if appointment_id in (None, "") or reads.verification.appointment_match_count != 1:
        return False
    for result in reads.results:
        if result.kind != "appointments" or not result.ok:
            continue
        rows = result.payload.get("appointments")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("appointment_id")) != str(appointment_id):
                continue
            if row.get("status") not in {"pending", "confirmed"}:
                return False
            for row_key, context_key in (
                ("service_id", "service_id"),
                ("doctor_id", "doctor_id"),
                ("laser_device_key", "device_key"),
            ):
                expected = recent_action.get(context_key)
                current = row.get(row_key)
                if expected in (None, ""):
                    if current not in (None, ""):
                        return False
                elif str(current) != str(expected):
                    return False
            current_start = row.get("start_at")
            recent_start = recent_action.get("start_at")
            if not isinstance(current_start, str) or not isinstance(recent_start, str):
                return False
            try:
                current_dt = datetime.fromisoformat(current_start)
                recent_dt = datetime.fromisoformat(recent_start)
            except ValueError:
                return False
            if (
                current_dt.tzinfo is None
                or current_dt.utcoffset() is None
                or recent_dt.tzinfo is None
                or recent_dt.utcoffset() is None
                or current_dt != recent_dt
            ):
                return False
            return True
    return False


def _reschedule_acknowledgment_step(step: PlanStep) -> PlanStep:
    return step.model_copy(
        update={
            "disposition": "respond",
            "reads": [],
            "write_intent": None,
            "state_action": "none",
            "response_goal": "social_ack",
            "clarification_field": None,
            "facts": _acknowledgment_facts("reschedule"),
        }
    )


def _bare_recent_cancellation(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object],
) -> bool:
    if (
        step.operation_type != "cancel_appointment"
        or recent_action.get("operation_type") != "cancel_appointment"
        or recent_action.get("status") != "cancelled"
    ):
        return False
    explicit_target = step.facts.get("appointment_id")
    if explicit_target not in (None, ""):
        return str(explicit_target) == str(recent_action.get("appointment_id"))
    if operation.selection is not None:
        return False
    entities = operation.entities
    return all(
        value is None
        for value in (
            entities.appointment,
            entities.service,
            entities.doctor,
            entities.device,
            entities.date,
            entities.time,
        )
    )


def _acknowledgment_facts(action: str, *, same_booking: bool = False) -> dict[str, object]:
    return {
        "acknowledgment": {
            "action": action,
            "already_completed": True,
            **({"same_booking": True} if same_booking else {}),
        }
    }


def _bare_recent_booking_confirmation_ack(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object],
) -> bool:
    """Treat a bare post-booking confirmation as an acknowledgment, not a status mutation.

    The model may semantically classify a short continuation such as a repeated
    confirmation as ``confirm_appointment`` after the booking write has already
    succeeded.  Recent action context identifies that just-completed booking, but
    it is not lifecycle authority.  Only collapse the turn when the customer did
    not explicitly target an appointment or appointment field; the caller then
    revalidates the recent appointment canonically before suppressing the write.
    """

    if (
        step.operation_type != "confirm_appointment"
        or recent_action.get("operation_type") != "book"
        or recent_action.get("status") not in {"pending", "confirmed"}
        or not operation.continues_previous
        or operation.selection is not None
        or operation.active_task_explicit_fields
        or operation.fresh_task_explicit_fields
        or operation.appointment_action_explicit_fields
        or (
            operation.entities.appointment is not None
            and operation.entities.appointment.text not in (None, "")
        )
    ):
        return False

    source = operation.source_appointment
    source_appointment = source.appointment if source is not None else None
    if source_appointment is not None and source_appointment.text not in (None, ""):
        return False
    return True


def _recent_booking_confirmation_ack_validation_request(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object] | None,
) -> ReadRequest | None:
    if not isinstance(recent_action, dict) or not _bare_recent_booking_confirmation_ack(
        step,
        operation,
        recent_action,
    ):
        return None
    appointment_id = recent_action.get("appointment_id")
    if appointment_id in (None, ""):
        return None
    return ReadRequest(
        kind="appointments",
        parameters={"appointment_id": str(appointment_id)},
    )


def _recent_booking_validation_request(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object] | None,
    *,
    timezone_name: str,
) -> ReadRequest | None:
    """Return the targeted canonical read required before a duplicate-booking ack."""
    if not isinstance(recent_action, dict):
        return None
    if not _same_recent_booking(
        step,
        operation,
        recent_action,
        timezone_name=timezone_name,
    ):
        return None
    appointment_id = recent_action.get("appointment_id")
    if appointment_id in (None, ""):
        return None
    return ReadRequest(
        kind="appointments",
        parameters={"appointment_id": str(appointment_id)},
    )


def _canonical_recent_booking_is_current(
    recent_action: dict[str, object] | None,
    reads: ReadExecutionBundle,
) -> bool:
    """Require the recent booking to still match canonical active appointment truth."""
    if not isinstance(recent_action, dict):
        return False

    appointment_id = recent_action.get("appointment_id")
    if appointment_id in (None, "") or reads.verification.appointment_match_count != 1:
        return False

    def same_value(current: object, expected: object) -> bool:
        if expected in (None, ""):
            return current in (None, "")
        return str(current) == str(expected)

    for result in reads.results:
        if result.kind != "appointments" or not result.ok:
            continue
        rows = result.payload.get("appointments")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("appointment_id")) != str(appointment_id):
                continue
            if row.get("status") not in {"pending", "confirmed"}:
                return False
            if not same_value(row.get("service_id"), recent_action.get("service_id")):
                return False
            if not same_value(row.get("doctor_id"), recent_action.get("doctor_id")):
                return False
            if not same_value(row.get("laser_device_key"), recent_action.get("device_key")):
                return False
            current_start = row.get("start_at")
            recent_start = recent_action.get("start_at")
            if not isinstance(current_start, str) or not isinstance(recent_start, str):
                return False
            try:
                current_dt = datetime.fromisoformat(current_start)
                recent_dt = datetime.fromisoformat(recent_start)
            except ValueError:
                return False
            if (
                current_dt.tzinfo is None
                or current_dt.utcoffset() is None
                or recent_dt.tzinfo is None
                or recent_dt.utcoffset() is None
                or current_dt != recent_dt
            ):
                return False
            return True
    return False


def _active_reschedule_target_validation_request(
    step: PlanStep,
    active_task: ActiveTaskState | None,
) -> ReadRequest | None:
    if not isinstance(active_task, RescheduleTaskState):
        return None
    if step.operation_type != "reschedule":
        return None
    target_id = active_task.target.appointment_id
    for request in step.reads:
        if (
            request.kind == "appointments"
            and str(request.parameters.get("appointment_id") or "") == str(target_id)
        ):
            return request
    return None


def _canonical_reschedule_target_is_non_actionable(
    request: ReadRequest | None,
    reads: ReadExecutionBundle,
) -> bool:
    if request is None or request.kind != "appointments":
        return False
    target_id = request.parameters.get("appointment_id")
    if target_id in (None, "") or reads.verification.appointment_match_count != 0:
        return False
    appointment_results = [
        result for result in reads.results if result.kind == "appointments"
    ]
    if len(appointment_results) != 1 or not appointment_results[0].ok:
        return False
    rows = appointment_results[0].payload.get("appointments")
    return isinstance(rows, list) and not rows


def _invalidated_reschedule_context(
    active_task: ActiveTaskState | None,
) -> dict[str, object] | None:
    if not isinstance(active_task, RescheduleTaskState):
        return None
    return {
        "operation_type": "reschedule_invalidated",
        "target_appointment_id": active_task.target.appointment_id,
        "replacement": active_task.replacement.model_dump(mode="json"),
    }


def _same_invalidated_reschedule_continuation(
    step: PlanStep,
    operation: TurnOperation,
    recent_action: dict[str, object],
) -> bool:
    if (
        recent_action.get("operation_type") != "reschedule_invalidated"
        or not operation.continues_previous
        or step.operation_type not in {"book", "reschedule"}
    ):
        return False
    raw_replacement = recent_action.get("replacement")
    if not isinstance(raw_replacement, dict):
        return False

    def compatible(current: object, previous: object) -> bool:
        if current in (None, "", {}, []):
            return True
        if isinstance(current, dict):
            if not isinstance(previous, dict):
                return False
            return all(
                compatible(value, previous.get(key))
                for key, value in current.items()
            )
        return previous not in (None, "", {}, []) and current == previous

    return all(
        compatible(step.facts.get(key), raw_replacement.get(key))
        for key in ("service_id", "doctor_id", "device_key", "date", "time")
    )


def _invalidated_reschedule_continuation_step(step: PlanStep) -> PlanStep:
    return step.model_copy(
        update={
            "disposition": "respond",
            "reads": [],
            "write_intent": None,
            "state_action": "none",
            "response_goal": "clarification",
            "clarification_field": None,
            "facts": {
                "active_task_invalidated": True,
                "canonical_target_non_actionable": True,
                "stale_continuation_blocked": True,
            },
        }
    )


def _invalidated_reschedule_step(step: PlanStep) -> PlanStep:
    return step.model_copy(
        update={
            "disposition": "respond",
            "reads": [],
            "write_intent": None,
            "state_action": "cancel_active",
            "response_goal": "clarification",
            "clarification_field": None,
            "facts": {
                **step.facts,
                "active_task_invalidated": True,
                "canonical_target_non_actionable": True,
            },
        }
    )


def _booking_acknowledgment_step(step: PlanStep) -> PlanStep:
    return step.model_copy(
        update={
            "disposition": "respond",
            "reads": [],
            "write_intent": None,
            "state_action": "none",
            "response_goal": "social_ack",
            "clarification_field": None,
            "facts": _acknowledgment_facts("booking", same_booking=True),
        }
    )


def _normalize_recent_action_acknowledgments(
    plan: TurnPlan,
    understanding: TiaTurnUnderstanding,
    *,
    recent_action: dict[str, object] | None,
    timezone_name: str,
) -> TurnPlan:
    if not isinstance(recent_action, dict):
        return plan
    normalized: list[PlanStep] = []
    changed = False
    for step in plan.steps:
        operation = _operation_for_step(understanding, step)
        ack_facts: dict[str, object] | None = None
        # Booking acknowledgments are resolved later, after a targeted canonical
        # appointment read. Recent action context is continuity evidence, not
        # current lifecycle truth.
        if _bare_recent_cancellation(step, operation, recent_action):
            ack_facts = _acknowledgment_facts("cancel_appointment")
        if _same_invalidated_reschedule_continuation(step, operation, recent_action):
            normalized.append(_invalidated_reschedule_continuation_step(step))
            changed = True
            continue
        if ack_facts is None:
            normalized.append(step)
            continue
        normalized.append(
            step.model_copy(
                update={
                    "disposition": "respond",
                    "reads": [],
                    "write_intent": None,
                    "state_action": "none",
                    "response_goal": "social_ack",
                    "clarification_field": None,
                    "facts": ack_facts,
                }
            )
        )
        changed = True
    return plan.model_copy(update={"steps": normalized}) if changed else plan


def _terminal_handoff_plan(plan: TurnPlan) -> bool:
    """Safety/standalone handoffs remain terminal; mixed safe reads may run first."""
    return plan.handoff_category is not None and all(
        step.disposition == "handoff" for step in plan.steps
    )


def _preserve_active_task_on_handoff(plan: TurnPlan) -> bool:
    return any(
        step.disposition == "handoff"
        and step.facts.get("preserve_active_task") is True
        for step in plan.steps
    )


def _persist_final_task(
    *,
    db: Session,
    workspace_id: UUID,
    conversation_id: UUID,
    patient_id: UUID,
    run_id: UUID,
    initial: PersistedActiveTask | None,
    final_task: ActiveTaskState | None,
    cancelled_existing_task: bool,
    cancelled_existing_task_reason: str | None,
    replaced_existing_task: bool,
    replaced_existing_task_reason: str | None,
    completed_existing_task_result: dict[str, object] | None,
) -> PersistedActiveTask | None:
    initial_task = initial.active_task if initial is not None else None

    if replaced_existing_task and initial is not None:
        cancel_active_task(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            patient_id=patient_id,
            expected=initial,
            run_id=run_id,
            reason=replaced_existing_task_reason or "fresh_customer_task_replaced_active_task",
        )
        if final_task is None:
            return None
        return save_active_task(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            patient_id=patient_id,
            active_task=final_task,
            run_id=run_id,
            expected=None,
        )

    if final_task == initial_task and completed_existing_task_result is None:
        return initial

    if final_task is None:
        if initial is not None:
            if completed_existing_task_result is not None:
                complete_active_task(
                    db,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    patient_id=patient_id,
                    expected=initial,
                    run_id=run_id,
                    result=completed_existing_task_result,
                )
            elif cancelled_existing_task:
                cancel_active_task(
                    db,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    patient_id=patient_id,
                    expected=initial,
                    run_id=run_id,
                    reason=(
                        cancelled_existing_task_reason
                        or "customer_cancelled_active_task"
                    ),
                )
            else:
                raise RuntimeError(
                    "V2 runtime may clear persisted state only through an explicit cancellation "
                    "or a completed terminal write."
                )
        return None

    return save_active_task(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        patient_id=patient_id,
        active_task=final_task,
        run_id=run_id,
        expected=initial,
    )


def orchestrate_v2_turn(
    *,
    db: Session,
    workspace: Workspace,
    patient: Patient,
    conversation_id: UUID,
    run_id: UUID,
    history: list[BaseMessage],
    local_now: datetime,
    timezone_name: str,
    clinic_name: str,
    catalog: dict[str, Any] | None = None,
    adapter: ClinicAdapter | None = None,
    turn_id: str | None = None,
    write_executor: V2WriteExecutor | None = None,
    recent_read_context: dict[str, Any] | None = None,
    recent_action_context: dict[str, Any] | None = None,
    automation_context: dict[str, Any] | None = None,
    pending_choice_context: dict[str, Any] | None = None,
    availability_reference_context: dict[str, Any] | None = None,
) -> V2OrchestratedTurn:
    """Run one stateful V2 turn with an optional verified-write executor.

    With no executor this preserves the isolated/shadow boundary and returns the first
    ``PendingV2Write`` without mutating clinic business data. Production may supply a
    real executor; successful writes are converted to verified outcomes and compound
    operations continue in order. The caller owns the outer database transaction.
    """
    persisted = load_active_task(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation_id,
        patient_id=patient.id,
        run_id=run_id,
    )
    initial_task = persisted.active_task if persisted is not None else None
    pending_choice = _pending_choice_from_context(
        pending_choice_context,
        now=local_now,
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
    explicit_user_time = extract_single_explicit_hhmm(latest_customer_text)

    canonical_catalog = catalog or build_clinic_catalog(db, workspace)
    semantic_context: SemanticContext = build_semantic_context(canonical_catalog)
    task_context_kwargs: dict[str, Any] = {
        "active_task": _task_dict(initial_task),
    }
    if pending_choice is not None:
        task_context_kwargs["pending_choice"] = pending_choice.model_dump(mode="json")
    semantic_context = with_safe_task_context(
        semantic_context,
        **task_context_kwargs,
    )
    semantic_context = with_safe_read_context(
        semantic_context,
        read_context=recent_read_context,
    )
    semantic_context = with_safe_availability_reference_context(
        semantic_context,
        availability_context=availability_reference_context,
    )
    semantic_context = with_safe_action_context(
        semantic_context,
        action_context=recent_action_context,
    )
    semantic_context = with_safe_automation_context(
        semantic_context,
        automation_context=automation_context,
    )
    reference_interpretation: ReferenceInterpretation | None = None
    reference_plan_override: TurnPlan | None = None
    full_interpreter_called = True
    raw_reference_options = (availability_reference_context or {}).get(
        "availability_reference_options"
    )
    if (
        latest_customer_text
        and isinstance(raw_reference_options, list)
        and raw_reference_options
    ):
        reference_interpretation = interpret_availability_reference_turn(
            latest_customer_text=latest_customer_text,
            availability_context=availability_reference_context or {},
        )

    decision = reference_interpretation.decision if reference_interpretation is not None else None
    if decision is not None and decision.action in {"new_search", "refresh_availability"}:
        model_input = dict(semantic_context.model_input)
        model_input["availability_followup_intent"] = {
            "action": decision.action,
            "time_constraint": (
                decision.time_constraint.model_dump(mode="json")
                if decision.time_constraint is not None
                else None
            ),
        }
        semantic_context = SemanticContext(
            model_input=model_input,
            reference_map=semantic_context.reference_map,
            server_metadata=semantic_context.server_metadata,
        )
    exact_reference_time = (
        str(decision.exact_time).strip()[:5]
        if decision is not None and decision.exact_time is not None
        else None
    )
    carried_pending_time = (
        str((availability_reference_context or {}).get("pending_exact_time") or "").strip()[:5]
        or None
    )
    verified_pending_time = (
        _verified_pending_reference_time(
            initial_task,
            availability_context=availability_reference_context,
            exact_time=exact_reference_time,
            now=local_now,
        )
        if initial_task is not None
        and decision is not None
        and decision.action == "clarify"
        and exact_reference_time is not None
        else None
    )
    selection_time = explicit_user_time or exact_reference_time or carried_pending_time
    verified_booking_selection = _verified_booking_selection_plan(
        initial_task,
        decision=decision,
        availability_context=availability_reference_context,
        catalog=canonical_catalog,
        selection_time=selection_time,
        now=local_now,
    )
    grouped_reference = (
        _grouped_visit_reference_understanding(
            initial_task,
            decision=decision,
            availability_context=availability_reference_context,
            catalog=canonical_catalog,
        )
        if decision is not None and decision.action == "clarify"
        else None
    )
    if verified_booking_selection is not None:
        reference_plan_override, availability_reference_context = verified_booking_selection
        understanding = TiaTurnUnderstanding(
            operations=[
                TurnOperation(
                    type="book",
                    entities=TurnEntities(),
                    selection=Selection(kind="ref", ref=str(decision.option_ref)),
                    execution_intent="informational",
                    active_task_relationship="continue",
                    continues_previous=True,
                )
            ]
        )
        full_interpreter_called = False
    elif grouped_reference is not None:
        understanding, initial_task = grouped_reference
        full_interpreter_called = False
    elif (
        decision is not None
        and decision.action == "new_search"
        and exact_reference_time is not None
        and explicit_user_time is not None
        and exact_reference_time == explicit_user_time
    ):
        understanding = TiaTurnUnderstanding(
            operations=[
                TurnOperation(
                    type="availability",
                    entities=TurnEntities(
                        time=TimeConstraint(
                            mode="exact",
                            start_time=explicit_user_time,
                            start_time_ambiguity="none",
                        )
                    ),
                    execution_intent="informational",
                    continues_previous=True,
                )
            ]
        )
        # The narrow resolver understands only the changed clock. Rebind the
        # unchanged service/doctor/device/date from the current server-owned
        # displayed availability before planning the fresh canonical read.
        understanding = merge_presented_availability_context(
            understanding, semantic_context
        )
        understanding = merge_verified_read_context(understanding, semantic_context)
        full_interpreter_called = False
    elif decision is not None and decision.action == "select_presented_option" and decision.option_ref:
        active_selection_type = (
            "book"
            if isinstance(initial_task, BookingTaskState)
            else "reschedule"
            if isinstance(initial_task, RescheduleTaskState)
            else "availability"
        )
        validated_reference_context = (
            _active_task_reference_option_context(
                initial_task,
                option_ref=decision.option_ref,
                availability_context=availability_reference_context,
                explicit_user_time=selection_time,
                now=local_now,
            )
            if initial_task is not None
            else availability_reference_context
        )
        window_narrowing_facts = (
            _active_task_reference_window_facts(
                initial_task,
                option_ref=decision.option_ref,
                availability_context=availability_reference_context,
                now=local_now,
            )
            if initial_task is not None
            and validated_reference_context is None
            and selection_time is None
            else None
        )
        ambiguous_current_reference = False
        if (
            initial_task is not None
            and validated_reference_context is None
            and selection_time is not None
            and initial_task.option_snapshot is not None
            and initial_task.option_snapshot.is_current(
                task_version=initial_task.version,
                now=local_now,
            )
        ):
            unresolved = resolve_verified_availability_reference(
                Selection(kind="ref", ref=decision.option_ref),
                availability_reference_context,
                explicit_user_time=selection_time,
            )
            raw_dimensions = unresolved.get("ambiguous_dimensions")
            dimensions = (
                [
                    str(item)
                    for item in raw_dimensions
                    if str(item) in {"doctor", "device"}
                ]
                if isinstance(raw_dimensions, list)
                else []
            )
            ambiguous_current_reference = (
                unresolved.get("status") == "window_ambiguous"
                and unresolved.get("exact_time") == selection_time
                and bool(dimensions)
            )
        stale_active_selection = (
            initial_task is not None
            and validated_reference_context is None
            and window_narrowing_facts is None
            and not ambiguous_current_reference
        )
        if stale_active_selection:
            # A stale option reference is not enough to select a slot, but it should
            # not suppress a fresh interpretation of a natural-language time correction.
            # The normal planner still requires current canonical availability before a proposal.
            try:
                understanding = interpret_customer_turn_v2(
                    history=history,
                    semantic_context=semantic_context,
                    timezone_name=timezone_name,
                    local_now=local_now,
                )
            except StructuredOutputError as exc:
                raise V2TurnInterpretationStructuredOutputError(
                    "V2 turn semantic interpretation exhausted structured attempts."
                ) from exc
            full_interpreter_called = True
        elif window_narrowing_facts is not None:
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
            reference_plan_override = TurnPlan(
                steps=[
                    PlanStep(
                        operation_index=0,
                        operation_type="continue_active",
                        disposition="state_update",
                        state_action="update_active",
                        facts={
                            **window_narrowing_facts,
                            "verified_availability_window_narrowing": True,
                            "verified_availability_reference": True,
                            "availability_reference_option_ref": decision.option_ref,
                        },
                    )
                ]
            )
        else:
            if initial_task is not None and validated_reference_context is not None:
                availability_reference_context = validated_reference_context
            understanding = TiaTurnUnderstanding(
                operations=[
                    TurnOperation(
                        type=active_selection_type,
                        entities=TurnEntities(),
                        selection=Selection(kind="ref", ref=decision.option_ref),
                        execution_intent=(
                            "execute"
                            if active_selection_type != "availability"
                            else "informational"
                        ),
                        active_task_relationship=(
                            "continue"
                            if active_selection_type != "availability"
                            else "unspecified"
                        ),
                        continues_previous=True,
                    )
                ]
            )
            if (
                initial_task is None
                and decision.selection_scope != "none"
                and availability_reference_context is not None
            ):
                verified_scope = resolve_verified_availability_scope(
                    option_ref=decision.option_ref,
                    selection_scope=decision.selection_scope,
                    read_context=availability_reference_context,
                )
                resolution_status = resolve_verified_availability_reference(
                    Selection(kind="ref", ref=decision.option_ref),
                    availability_reference_context,
                    explicit_user_time=selection_time,
                ).get("status")
                if (
                    verified_scope.get("status") == "resolved"
                    and resolution_status == "window_ambiguous"
                ):
                    reference_plan_override = TurnPlan(
                        steps=[
                            PlanStep(
                                operation_index=0,
                                operation_type="availability",
                                disposition="clarify",
                                response_goal="clarification",
                                clarification_field="availability_reference",
                                facts={
                                    "verified_availability_reference": True,
                                    "availability_reference_reason": "window_ambiguous",
                                    "availability_reference_index": verified_scope.get("index"),
                                    "availability_reference_option_ref": decision.option_ref,
                                    "availability_reference_scope_field": verified_scope.get("field"),
                                    "availability_reference_scope_value": verified_scope.get("value"),
                                },
                            )
                        ]
                    )
        if not stale_active_selection:
            full_interpreter_called = False
    elif decision is not None and decision.action == "clarify":
        understanding = TiaTurnUnderstanding(
            operations=[
                TurnOperation(
                    type="availability",
                    entities=TurnEntities(),
                    execution_intent="informational",
                    continues_previous=True,
                )
            ]
        )
        reference_plan_override = TurnPlan(
            steps=[
                PlanStep(
                    operation_index=0,
                    operation_type="availability",
                    disposition="clarify",
                    response_goal="clarification",
                    clarification_field="availability_reference",
                    facts={
                        "verified_availability_reference": True,
                        "availability_reference_reason": "semantic_clarification",
                        **(
                            {"availability_reference_pending_time": verified_pending_time}
                            if verified_pending_time is not None
                            else {}
                        ),
                    },
                )
            ]
        )
        full_interpreter_called = False
    else:
        try:
            understanding = interpret_customer_turn_v2(
                history=history,
                semantic_context=semantic_context,
                timezone_name=timezone_name,
                local_now=local_now,
            )
        except StructuredOutputError as exc:
            raise V2TurnInterpretationStructuredOutputError(
                "V2 turn semantic interpretation exhausted structured attempts."
            ) from exc
    typed_period_hints: set[str] = set()
    for operation in understanding.operations:
        time_entity = getattr(operation.entities, "time", None)
        selection = getattr(operation, "selection", None)
        for hint in (
            getattr(time_entity, "spoken_period_hint", "none"),
            getattr(selection, "spoken_period_hint", "none"),
        ):
            if hint in {"am", "pm"}:
                typed_period_hints.add(hint)
    if len(typed_period_hints) == 1:
        typed_period_resolution = resolve_pending_bare_clock_period(
            initial_task,
            next(iter(typed_period_hints)),
            context=semantic_context,
        )
        if typed_period_resolution is not None:
            initial_task, _resolved_period = typed_period_resolution
            semantic_context = with_safe_task_context(
                semantic_context,
                active_task=_task_dict(initial_task),
                pending_choice=(
                    pending_choice.model_dump(mode="json") if pending_choice is not None else None
                ),
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

    understanding = merge_same_visit_booking_operations(understanding)
    understanding = merge_same_turn_grouped_availability_operations(
        understanding,
        semantic_context=semantic_context,
        catalog=canonical_catalog,
    )
    initial_booking_task = (
        initial_task if isinstance(initial_task, BookingTaskState) else None
    )
    understanding, booking_service_sets = collapse_booking_service_sets(
        understanding,
        semantic_context=semantic_context,
        catalog=canonical_catalog,
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
        catalog=canonical_catalog,
        booking_service_sets=booking_service_sets,
    )
    understanding, restored_visit_groups = restore_grouped_booking_understanding(
        understanding,
        active_task=initial_booking_task,
        context=semantic_context,
        booking_service_sets=booking_service_sets,
        catalog=canonical_catalog,
    )
    if restored_visit_groups:
        understanding, restored_service_sets, restored_visit_groups = (
            recollapse_restored_booking_visit_groups(
                understanding,
                semantic_context=semantic_context,
                catalog=canonical_catalog,
                restored_visit_groups=restored_visit_groups,
            )
        )
        booking_service_sets = {**booking_service_sets, **restored_service_sets}
    semantic_visit_groups = {
        **semantic_visit_groups,
        **restored_visit_groups,
    }
    understanding = _normalize_cross_turn_nearest_after_verified_miss(
        understanding,
        recent_read_context=recent_read_context,
    )
    planner_active_task: ActiveTaskState | None = narrowed_booking_task or initial_task
    plan = reference_plan_override or plan_turn(
        understanding,
        PlannerContext(
            semantic_context=semantic_context,
            active_task=planner_active_task,
            now=local_now,
            pending_choice=pending_choice,
            explicit_user_time=explicit_user_time,
            availability_reference_exact_time=selection_time,
            recent_read_context=recent_read_context,
            availability_reference_context=availability_reference_context,
        ),
    )
    plan = tag_booking_service_sets(
        plan, service_sets=booking_service_sets, catalog=canonical_catalog
    )
    plan = normalize_compound_turn_plan(
        plan,
        catalog=canonical_catalog,
        operation_visit_groups=semantic_visit_groups,
    )
    plan = guard_multiple_booking_writes(plan)
    plan = _normalize_recent_action_acknowledgments(
        plan,
        understanding,
        recent_action=recent_action_context,
        timezone_name=timezone_name,
    )

    if _terminal_handoff_plan(plan):
        outcome = build_handoff_outcome(plan)
        preserve_active_task = _preserve_active_task_on_handoff(plan)
        if persisted is not None and not preserve_active_task:
            cancel_active_task(
                db,
                workspace_id=workspace.id,
                conversation_id=conversation_id,
                patient_id=patient.id,
                expected=persisted,
                run_id=run_id,
                reason="human_handoff_requested",
            )
        reply, model = compose_v2_customer_reply(
            clinic_name=clinic_name,
            timezone_name=timezone_name,
            local_now=local_now,
            history=history,
            outcomes=[outcome],
        )
        return V2OrchestratedTurn(
            understanding=understanding,
            plan=plan,
            traces=(),
            outcomes=(outcome,),
            reply=reply,
            responder_model=model,
            active_task=initial_task if preserve_active_task else None,
            persisted_task=persisted if preserve_active_task else None,
            pending_write=None,
            reference_semantic_path_used=reference_interpretation is not None,
            full_interpreter_called=full_interpreter_called,
            reference_action=(
                reference_interpretation.decision.action
                if reference_interpretation is not None
                else None
            ),
            selected_option_ref=(
                reference_interpretation.decision.option_ref
                if reference_interpretation is not None
                else None
            ),
            reference_structured_output_error=(
                reference_interpretation.structured_output_error
                if reference_interpretation is not None
                else False
            ),
        )

    read_context = ReadExecutionContext(
        db=db,
        workspace=workspace,
        patient=patient,
        now=local_now,
        catalog=canonical_catalog,
        adapter=adapter,
    )
    resolved_turn_id = turn_id or str(run_id)
    stable_visit_group_id = str(
        uuid5(NAMESPACE_URL, f"tia-v2-visit:{workspace.id}:{resolved_turn_id}")
    )
    plan = preflight_compound_visit_plan(
        plan,
        context=read_context,
        timezone_name=timezone_name,
        visit_group_id=stable_visit_group_id,
    )
    grouped_booking_task = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=initial_booking_task,
        now=local_now,
        turn_id=resolved_turn_id,
    )
    plan, compound_category_task = _prepare_cross_category_compound_proposal(
        plan,
        current_task=initial_booking_task,
        workspace=workspace,
        patient=patient,
        catalog=canonical_catalog,
        now=local_now,
        turn_id=resolved_turn_id,
    )
    if compound_category_task is not None:
        grouped_booking_task = compound_category_task
    plan = enforce_grouped_booking_write_guard(
        plan,
        active_task=initial_booking_task,
        candidate_task=grouped_booking_task,
        explicit_narrowing=narrowed_booking_task is not None,
    )
    current_task: ActiveTaskState | None = (
        grouped_booking_task or narrowed_booking_task or initial_task
    )
    traces: list[V2RuntimeStepTrace] = []
    outcomes: list[TurnOutcome] = []
    pending_write: PendingV2Write | None = None
    outgoing_pending_choice: OptionSnapshot | None = (
        pending_choice if _is_pure_informational_side_question(understanding) else None
    )
    cancelled_existing_task = False
    cancelled_existing_task_reason: str | None = None
    replaced_existing_task = False
    replaced_existing_task_reason: str | None = None
    completed_existing_task_result: dict[str, object] | None = None
    completed_action_context: dict[str, object] | None = None
    compound_cursors: dict[str, datetime] = {}
    operation_reads: dict[int, ReadExecutionBundle] = {}
    same_turn_verified_appointment: dict[str, object] | None = None
    grouped_positions: dict[str, list[int]] = {}
    for position, grouped_step in enumerate(plan.steps):
        group = compound_write_group(grouped_step)
        if group is not None:
            grouped_positions.setdefault(group, []).append(position)
    active_group_key: str | None = None
    active_group_tx = None
    active_group_outcome_start = 0
    active_group_trace_start = 0

    for step_position, planned_step in enumerate(plan.steps):
        anchor_key = compound_anchor_key(planned_step)
        planned_step = apply_compound_runtime_cursor(
            planned_step,
            previous_end_at=compound_cursors.get(anchor_key) if anchor_key is not None else None,
            timezone_name=timezone_name,
        )
        operation = _operation_for_step(understanding, planned_step)
        if not _continuation_condition_satisfied(
            operation,
            previous_reads=operation_reads.get(planned_step.operation_index - 1),
            recent_read_context=recent_read_context,
        ):
            traces.append(
                V2RuntimeStepTrace(
                    operation_index=planned_step.operation_index,
                    operation_type=planned_step.operation_type,
                    disposition_before=planned_step.disposition,
                    disposition_after="skipped",
                    read_kinds=(),
                    skipped=True,
                    skip_reason=(
                        "continuation_condition_false:"
                        f"{getattr(operation, 'continuation_condition', 'always')}"
                    ),
                )
            )
            continue

        planned_step = resolve_same_turn_verified_service_dependency(
            planned_step,
            operation=operation,
            verified_appointment_parameters=same_turn_verified_appointment,
        )
        effective_step = adapt_matching_active_task_step(
            planned_step,
            operation=operation,
            active_task=current_task,
            context=semantic_context,
            now=local_now,
        )
        effective_step = persist_initial_task_intent(
            effective_step,
            operation=operation,
            context=semantic_context,
        )
        if isinstance(planned_step.facts.get("requested_service_ids"), list):
            effective_step = effective_step.model_copy(
                update={
                    "facts": {
                        **effective_step.facts,
                        "requested_service_ids": list(planned_step.facts["requested_service_ids"]),
                        "multi_service_single_appointment": True,
                        "booking_proposal_required": True,
                    }
                }
            )
        step_group_key = compound_write_group(effective_step) or compound_write_group(planned_step)

        if effective_step.state_action == "update_active" and step_group_key is None:
            # Grouped booking steps are already reconstructed/preflighted as one
            # atomic visit. Single-service journey progression must not replace
            # those grouped steps with a one-component commercial gate.
            initial_transition = apply_step_state(
                current_task,
                step=effective_step,
                operation=operation,
                reads=None,
                now=local_now,
                turn_id=resolved_turn_id,
                catalog=canonical_catalog,
            )
            current_task = initial_transition.active_task
            if current_task is not None and effective_step.disposition != "clarify":
                current_task, clock_gate = prepare_active_task_clock_progress(
                    current_task,
                    operation_index=effective_step.operation_index,
                    context=semantic_context,
                )
                effective_step = clock_gate or plan_active_task_progress(
                    current_task,
                    operation_index=effective_step.operation_index,
                    context=semantic_context,
                )

        target_validation = _active_reschedule_target_validation_request(
            effective_step,
            current_task,
        )
        target_validation_reads = ReadExecutionBundle()
        if target_validation is not None:
            target_validation_step = effective_step.model_copy(
                update={"reads": [target_validation]}
            )
            target_validation_reads = execute_step_reads(
                target_validation_step,
                read_context,
            )

        recent_booking_validation = _recent_booking_validation_request(
            effective_step,
            operation,
            recent_action_context,
            timezone_name=timezone_name,
        )
        if recent_booking_validation is None:
            recent_booking_validation = _recent_booking_confirmation_ack_validation_request(
                effective_step,
                operation,
                recent_action_context,
            )
        recent_reschedule_validation = _recent_reschedule_validation_request(
            effective_step,
            operation,
            recent_action_context,
            timezone_name=timezone_name,
        )
        validation_request = recent_booking_validation or recent_reschedule_validation
        validation_reads = ReadExecutionBundle()
        if validation_request is not None:
            validation_step = effective_step.model_copy(
                update={"reads": [validation_request]}
            )
            validation_reads = execute_step_reads(validation_step, read_context)

        if _canonical_reschedule_target_is_non_actionable(
            target_validation,
            target_validation_reads,
        ):
            reads = target_validation_reads
            invalidated_context = _invalidated_reschedule_context(current_task)
            if invalidated_context is not None:
                completed_action_context = invalidated_context
            advanced = _invalidated_reschedule_step(effective_step)
        elif (
            recent_booking_validation is not None
            and _canonical_recent_booking_is_current(
                recent_action_context,
                validation_reads,
            )
        ):
            reads = validation_reads
            advanced = _booking_acknowledgment_step(effective_step)
        elif (
            recent_reschedule_validation is not None
            and _canonical_recent_reschedule_is_current(
                recent_action_context,
                validation_reads,
            )
        ):
            reads = validation_reads
            advanced = _reschedule_acknowledgment_step(effective_step)
        else:
            normal_reads = (
                execute_step_reads(effective_step, read_context)
                if effective_step.reads
                else ReadExecutionBundle()
            )
            compatibility_advanced = _compatibility_failure_step(
                effective_step,
                normal_reads,
            )
            if compatibility_advanced is None:
                compound_advanced, normal_reads, compound_handled = (
                    resolve_compound_followup_after_reads(
                        effective_step,
                        normal_reads,
                    )
                )
                advanced = (
                    compound_advanced
                    if compound_handled
                    else _advance_after_reads(effective_step, normal_reads)
                )
            else:
                advanced = compatibility_advanced
            reads = (
                ReadExecutionBundle(
                    results=[*validation_reads.results, *normal_reads.results],
                    verification=normal_reads.verification,
                )
                if validation_reads.results
                else normal_reads
            )

        advanced = _auto_resolve_single_booking_device(advanced, reads)
        advanced, pending_proposal_for_step = _prepare_booking_proposal(
            advanced,
            reads=reads,
            current_task=current_task,
            db=db,
            workspace=workspace,
            patient=patient,
            catalog=canonical_catalog,
            now=local_now,
            turn_id=resolved_turn_id,
        )
        operation_reads[effective_step.operation_index] = reads
        if effective_step.operation_type == "appointment_list":
            verified_appointment = dict(reads.verification.verified_parameters)
            same_turn_verified_appointment = (
                verified_appointment
                if verified_appointment.get("service_id") not in (None, "")
                else None
            )
        transition = apply_step_state(
            current_task,
            step=advanced,
            operation=operation,
            reads=reads if reads.results else None,
            now=local_now,
            turn_id=resolved_turn_id,
            catalog=canonical_catalog,
        )
        if (
            pending_proposal_for_step is not None
            and isinstance(transition.active_task, BookingTaskState)
        ):
            proposal_base = transition.active_task
            if advanced.facts.get("verified_booking_slot_selection") is True:
                proposal_task = _attach_verified_booking_proposal_to_task(
                    proposal_base,
                    proposal=pending_proposal_for_step,
                    selected_option_ref=str(advanced.facts.get("selected_option_ref") or ""),
                    catalog=canonical_catalog,
                    timezone_name=getattr(workspace, "timezone", "Africa/Cairo"),
                )
            else:
                proposal_task = attach_booking_proposal(
                    proposal_base,
                    proposal=pending_proposal_for_step,
                )
            transition = transition.__class__(
                active_task=proposal_task,
                changed=True,
                reason="booking_proposal_created",
            )
        advanced = finalize_step_after_state_transition(advanced, transition)
        if (
            advanced.state_action == "cancel_active"
            and transition.changed
            and persisted is not None
            and transition.active_task is None
        ):
            cancelled_existing_task = True
            if advanced.facts.get("canonical_target_non_actionable") is True:
                cancelled_existing_task_reason = (
                    "canonical_reschedule_target_non_actionable"
                )
        if (
            advanced.state_action == "replace_active"
            and transition.changed
            and persisted is not None
        ):
            replaced_existing_task = True
            replaced_existing_task_reason = "fresh_customer_task_replaced_active_task"
            outgoing_pending_choice = None
        current_task = transition.active_task

        continue_same_turn_booking = (
            advanced.facts.get("device_auto_resolved") is True
            or (
                advanced.facts.get("booking_commercial_basis_step") is True
                and advanced.facts.get("booking_next_field") == "booking"
                and advanced.facts.get("exact_time_requested") is True
            )
        )
        if (
            continue_same_turn_booking
            and isinstance(current_task, BookingTaskState)
            and step_group_key is None
        ):
            current_task, clock_gate = prepare_active_task_clock_progress(
                current_task,
                operation_index=advanced.operation_index,
                context=semantic_context,
            )
            continuation_step = clock_gate or plan_active_task_progress(
                current_task,
                operation_index=advanced.operation_index,
                context=semantic_context,
            )
            continuation_reads = (
                execute_step_reads(continuation_step, read_context)
                if continuation_step.reads
                else ReadExecutionBundle()
            )
            continuation_compatibility = _compatibility_failure_step(
                continuation_step,
                continuation_reads,
            )
            continuation_advanced = (
                continuation_compatibility
                if continuation_compatibility is not None
                else _advance_after_reads(continuation_step, continuation_reads)
            )
            if continuation_reads.results:
                reads = ReadExecutionBundle(
                    results=[*reads.results, *continuation_reads.results],
                    verification=continuation_reads.verification,
                )
            advanced = continuation_advanced
            operation_reads[effective_step.operation_index] = reads
            advanced, pending_proposal_for_step = _prepare_booking_proposal(
                advanced,
                reads=reads,
                current_task=current_task,
                db=db,
                workspace=workspace,
                patient=patient,
                catalog=canonical_catalog,
                now=local_now,
                turn_id=resolved_turn_id,
            )
            continuation_transition = apply_step_state(
                current_task,
                step=advanced,
                operation=operation,
                reads=reads if reads.results else None,
                now=local_now,
                turn_id=resolved_turn_id,
                catalog=canonical_catalog,
            )
            if (
                pending_proposal_for_step is not None
                and isinstance(continuation_transition.active_task, BookingTaskState)
            ):
                proposal_task = attach_booking_proposal(
                    continuation_transition.active_task,
                    proposal=pending_proposal_for_step,
                )
                continuation_transition = continuation_transition.__class__(
                    active_task=proposal_task,
                    changed=True,
                    reason="booking_proposal_created",
                )
            advanced = finalize_step_after_state_transition(
                advanced,
                continuation_transition,
            )
            current_task = continuation_transition.active_task

        if advanced.disposition == "write_ready":
            if write_executor is None:
                pending_write = PendingV2Write(step=advanced, reads=reads)
                traces.append(
                    V2RuntimeStepTrace(
                        operation_index=advanced.operation_index,
                        operation_type=advanced.operation_type,
                        disposition_before=planned_step.disposition,
                        disposition_after=advanced.disposition,
                        read_kinds=tuple(result.kind for result in reads.results),
                        verified_parameters=dict(reads.verification.verified_parameters),
                        availability_slots=_verified_availability_slots(reads),
                        pending_write=True,
                    )
                )
                break

            if step_group_key is not None and active_group_tx is None:
                active_group_key = step_group_key
                active_group_outcome_start = len(outcomes)
                active_group_trace_start = len(traces)
                active_group_tx = begin_group_savepoint(db)

            action_result = write_executor(advanced)
            outcome = build_step_outcome(
                advanced,
                turn=understanding,
                semantic_context=semantic_context,
                reads=reads if reads.results else None,
                action_result=action_result,
                active_task_summary=_task_dict(current_task),
            )
            traces.append(
                V2RuntimeStepTrace(
                    operation_index=advanced.operation_index,
                    operation_type=advanced.operation_type,
                    disposition_before=planned_step.disposition,
                    disposition_after=advanced.disposition,
                    read_kinds=tuple(result.kind for result in reads.results),
                    outcome=outcome,
                    verified_parameters=dict(reads.verification.verified_parameters),
                        availability_slots=_verified_availability_slots(reads),
                )
            )
            outcomes.append(outcome)

            if outcome.status == "completed":
                action_context = _completed_action_context(advanced, action_result)
                if action_context is not None:
                    completed_action_context = action_context
                if (
                    step_group_key is not None
                    and active_group_key == step_group_key
                    and grouped_positions.get(step_group_key)
                    and step_position == grouped_positions[step_group_key][-1]
                    and active_group_tx is not None
                ):
                    release_group_savepoint(active_group_tx)
                    active_group_tx = None
                    active_group_key = None
                write_kind = advanced.write_intent.kind if advanced.write_intent is not None else None
                if write_kind == "booking" and anchor_key is not None:
                    verified_end = completed_compound_booking_end(reads)
                    if verified_end is not None:
                        compound_cursors[anchor_key] = verified_end
                terminal_task_write = step_group_key is None or (
                    bool(grouped_positions.get(step_group_key))
                    and step_position == grouped_positions[step_group_key][-1]
                )
                if (
                    terminal_task_write
                    and current_task is not None
                    and (
                        current_task.task_type == write_kind
                        or (current_task.task_type == "booking" and write_kind == "booking_proposal")
                    )
                ):
                    if (
                        persisted is not None
                        and persisted.active_task.task_type == current_task.task_type
                    ):
                        completed_existing_task_result = dict(action_result)
                    current_task = None
                continue

            if outcome.status == "handoff":
                if current_task is not None:
                    cancelled_existing_task = persisted is not None
                    current_task = None
            if step_group_key is not None and active_group_key == step_group_key and active_group_tx is not None:
                rollback_group_savepoint(active_group_tx)
                active_group_tx = None
                active_group_key = None
                outcomes = outcomes[:active_group_outcome_start]
                traces = traces[:active_group_trace_start]
                outcomes.append(outcome)
                traces.append(
                    V2RuntimeStepTrace(
                        operation_index=advanced.operation_index,
                        operation_type=advanced.operation_type,
                        disposition_before=planned_step.disposition,
                        disposition_after=advanced.disposition,
                        read_kinds=tuple(result.kind for result in reads.results),
                        outcome=outcome,
                        verified_parameters=dict(reads.verification.verified_parameters),
                        availability_slots=_verified_availability_slots(reads),
                    )
                )
            break

        outcome = build_step_outcome(
            advanced,
            turn=understanding,
            semantic_context=semantic_context,
            reads=reads if reads.results else None,
            action_result=None,
            active_task_summary=None,
        )
        if step_group_key is not None and active_group_key == step_group_key and active_group_tx is not None:
            rollback_group_savepoint(active_group_tx)
            active_group_tx = None
            active_group_key = None
            outcomes = outcomes[:active_group_outcome_start]
            traces = traces[:active_group_trace_start]
        traces.append(
            V2RuntimeStepTrace(
                operation_index=advanced.operation_index,
                operation_type=advanced.operation_type,
                disposition_before=planned_step.disposition,
                disposition_after=advanced.disposition,
                read_kinds=tuple(result.kind for result in reads.results),
                outcome=outcome,
                verified_parameters=dict(reads.verification.verified_parameters),
                        availability_slots=_verified_availability_slots(reads),
            )
        )
        outcomes.append(outcome)
        selected_snapshot = _selected_appointment_snapshot(
            step=advanced,
            now=local_now,
            turn_id=resolved_turn_id,
        )
        choice_snapshot = _appointment_choice_snapshot(
            step=advanced,
            outcome=outcome,
            now=local_now,
            turn_id=resolved_turn_id,
        )
        outgoing_pending_choice = selected_snapshot or choice_snapshot or outgoing_pending_choice
        if outcome.status == "handoff":
            outgoing_pending_choice = None
            if current_task is not None:
                cancelled_existing_task = persisted is not None
                current_task = None
            break

    if active_group_tx is not None:
        rollback_group_savepoint(active_group_tx)
        outcomes = outcomes[:active_group_outcome_start]
        traces = traces[:active_group_trace_start]
        active_group_tx = None
        active_group_key = None

    persisted_after = _persist_final_task(
        db=db,
        workspace_id=workspace.id,
        conversation_id=conversation_id,
        patient_id=patient.id,
        run_id=run_id,
        initial=persisted,
        final_task=current_task,
        cancelled_existing_task=cancelled_existing_task,
        cancelled_existing_task_reason=cancelled_existing_task_reason,
        replaced_existing_task=replaced_existing_task,
        replaced_existing_task_reason=replaced_existing_task_reason,
        completed_existing_task_result=completed_existing_task_result,
    )

    if pending_write is not None:
        return V2OrchestratedTurn(
            understanding=understanding,
            plan=plan,
            traces=tuple(traces),
            outcomes=tuple(outcomes),
            reply=None,
            responder_model=None,
            active_task=current_task,
            persisted_task=persisted_after,
            pending_write=pending_write,
            pending_choice=outgoing_pending_choice,
            reference_semantic_path_used=reference_interpretation is not None,
            full_interpreter_called=full_interpreter_called,
            reference_action=(
                reference_interpretation.decision.action
                if reference_interpretation is not None
                else None
            ),
            selected_option_ref=(
                reference_interpretation.decision.option_ref
                if reference_interpretation is not None
                else None
            ),
            reference_structured_output_error=(
                reference_interpretation.structured_output_error
                if reference_interpretation is not None
                else False
            ),
        )

    if _terminal_no_reply_allowed(
        understanding,
        active_task=current_task,
        pending_choice=outgoing_pending_choice,
        automation_context=automation_context,
    ):
        return V2OrchestratedTurn(
            understanding=understanding,
            plan=plan,
            traces=tuple(traces),
            outcomes=tuple(outcomes),
            reply=None,
            responder_model="deterministic:no-reply",
            active_task=current_task,
            persisted_task=persisted_after,
            pending_write=None,
            verified_action_context=completed_action_context,
            pending_choice=outgoing_pending_choice,
            reference_semantic_path_used=reference_interpretation is not None,
            full_interpreter_called=full_interpreter_called,
            reference_action=(
                reference_interpretation.decision.action
                if reference_interpretation is not None
                else None
            ),
            selected_option_ref=(
                reference_interpretation.decision.option_ref
                if reference_interpretation is not None
                else None
            ),
            reference_structured_output_error=(
                reference_interpretation.structured_output_error
                if reference_interpretation is not None
                else False
            ),
        )

    if not outcomes:
        raise RuntimeError("V2 runtime produced neither a customer outcome nor a pending write.")

    availability_continuation = _availability_presentation_continuation(
        understanding, plan, recent_read_context
    )
    reply, model = compose_v2_customer_reply(
        clinic_name=clinic_name,
        timezone_name=timezone_name,
        local_now=local_now,
        history=history,
        outcomes=outcomes,
        availability_shown_window_keys=(
            _availability_shown_window_keys(recent_read_context)
            if availability_continuation
            else None
        ),
        availability_continuation=availability_continuation,
    )
    return V2OrchestratedTurn(
        understanding=understanding,
        plan=plan,
        traces=tuple(traces),
        outcomes=tuple(outcomes),
        reply=reply,
        responder_model=model,
        active_task=current_task,
        persisted_task=persisted_after,
        pending_write=None,
        verified_action_context=completed_action_context,
        pending_choice=outgoing_pending_choice,
        reference_semantic_path_used=reference_interpretation is not None,
        full_interpreter_called=full_interpreter_called,
        reference_action=(
            reference_interpretation.decision.action if reference_interpretation is not None else None
        ),
        selected_option_ref=(
            reference_interpretation.decision.option_ref if reference_interpretation is not None else None
        ),
        reference_structured_output_error=(
            reference_interpretation.structured_output_error if reference_interpretation is not None else False
        ),
    )
