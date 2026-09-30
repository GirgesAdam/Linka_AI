from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnOperation
from app.services.agent_v2.outcome import ResponseGoal
from app.services.agent_v2.state import (
    ActiveTaskState,
    OptionChoice,
    OptionSnapshot,
    RescheduleTaskState,
)
from app.services.agent_v2.state_rules import option_snapshot_is_current

PlanDisposition = Literal[
    "read",
    "clarify",
    "state_update",
    "write_ready",
    "handoff",
    "respond",
    "blocked",
]
ReadKind = Literal[
    "service_catalog",
    "clinic_info",
    "doctors",
    "availability",
    "appointments",
    "customer_profile",
    "customer_history",
    "customer_packages",
    "package_offers",
    "package_refund_quote",
    "pulse_balance",
    "pulse_packs",
    "pulse_pack_offers",
    "pulse_billing_settings",
]
WriteKind = Literal[
    "booking",
    "confirm_appointment",
    "cancel_appointment",
    "reschedule",
    "buy_package",
    "buy_pulse_pack",
    "follow_up",
    "marketing_update",
]
StateAction = Literal[
    "none",
    "start_booking",
    "start_reschedule",
    "update_active",
    "select_active",
    "cancel_active",
]
ClarificationField = Literal[
    "service",
    "doctor",
    "device",
    "date",
    "time",
    "appointment",
    "package",
    "selection",
    "follow_up_time",
    "marketing_consent",
    "intent",
]


class StrictPlannerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadRequest(StrictPlannerModel):
    kind: ReadKind
    parameters: dict[str, object] = Field(default_factory=dict)


class WriteIntent(StrictPlannerModel):
    kind: WriteKind
    authorized: bool
    parameters: dict[str, object] = Field(default_factory=dict)
    requires_verification: bool = True


class PlanStep(StrictPlannerModel):
    operation_index: int
    operation_type: str
    disposition: PlanDisposition
    reads: list[ReadRequest] = Field(default_factory=list)
    write_intent: WriteIntent | None = None
    state_action: StateAction = "none"
    response_goal: ResponseGoal | None = None
    clarification_field: ClarificationField | None = None
    facts: dict[str, object] = Field(default_factory=dict)


class TurnPlan(StrictPlannerModel):
    steps: list[PlanStep] = Field(default_factory=list)
    handoff_category: str | None = None
    handoff_priority: str | None = None


class VerificationFacts(StrictPlannerModel):
    appointment_match_count: int | None = None
    exact_slot_match_count: int | None = None
    package_offer_match_count: int | None = None
    pulse_offer_match_count: int | None = None
    requires_human: bool = False
    verified_parameters: dict[str, object] = Field(default_factory=dict)


@dataclass(frozen=True)
class PlannerContext:
    semantic_context: SemanticContext
    active_task: ActiveTaskState | None
    now: datetime
    pending_choice: OptionSnapshot | None = None


def _canonical_entity(
    operation: TurnOperation,
    field: str,
    *,
    kind: str,
    semantic_context: SemanticContext,
) -> tuple[str | None, bool, list[str]]:
    entity = getattr(operation.entities, field)
    if entity is None:
        return None, False, []
    if entity.ref:
        resolved = semantic_context.resolve(entity.ref, expected_kind=kind)
        if resolved is not None:
            return resolved, False, []
    grounded = [
        item
        for ref in entity.candidate_refs
        if (item := semantic_context.resolve(ref, expected_kind=kind)) is not None
    ]
    if entity.candidate_mode == "set" and grounded:
        return None, False, list(dict.fromkeys(grounded))
    return None, bool(grounded), []


def _base_parameters(
    operation: TurnOperation,
    context: PlannerContext,
) -> tuple[dict[str, object], dict[str, bool]]:
    values: dict[str, object] = {}
    ambiguous: dict[str, bool] = {}
    for field, kind, output_key, set_key in (
        ("service", "service", "service_id", "service_ids"),
        ("doctor", "doctor", "doctor_id", "doctor_ids"),
        ("device", "device", "device_key", "device_keys"),
        ("appointment", "appointment", "appointment_id", "appointment_ids"),
        ("package", "package", "package_id", "package_ids"),
    ):
        value, is_ambiguous, requested_set = _canonical_entity(
            operation,
            field,
            kind=kind,
            semantic_context=context.semantic_context,
        )
        if value is not None:
            values[output_key] = value
        if requested_set:
            values[set_key] = requested_set
        ambiguous[field] = is_ambiguous

    if operation.entities.date is not None:
        values["date"] = operation.entities.date.model_dump(mode="json")
    if operation.entities.time is not None:
        values["time"] = operation.entities.time.model_dump(mode="json")
    if operation.entities.package_sessions is not None:
        values["package_sessions"] = operation.entities.package_sessions
    if operation.entities.pulse_count is not None:
        values["pulse_count"] = operation.entities.pulse_count
    if operation.entities.marketing_consent is not None:
        values["marketing_consent"] = operation.entities.marketing_consent
    if operation.entities.follow_up_at_local is not None:
        values["follow_up_at_local"] = operation.entities.follow_up_at_local
    values["package_usage"] = operation.package_usage
    return values, ambiguous


def _source_appointment_parameters(
    operation: TurnOperation,
    context: PlannerContext,
    *,
    fallback: dict[str, object],
) -> dict[str, object]:
    """Resolve existing-appointment identity independently from replacement constraints."""
    pending = context.pending_choice
    if (
        pending is not None
        and pending.purpose == "appointment_target"
        and pending.lifecycle_action == operation.type
        and context.now < pending.expires_at
        and len(pending.options) == 1
    ):
        selected_id = pending.options[0].payload.get("appointment_id")
        if selected_id not in (None, ""):
            return {"appointment_id": str(selected_id)}

    selector = operation.source_appointment
    if selector is None:
        if operation.type == "reschedule":
            appointment_id = fallback.get("appointment_id")
            return {"appointment_id": appointment_id} if appointment_id is not None else {}
        return {
            key: value
            for key, value in fallback.items()
            if key in {"appointment_id", "service_id", "doctor_id", "device_key", "date", "time"}
        }

    params: dict[str, object] = {}
    for field, kind, key in (
        ("appointment", "appointment", "appointment_id"),
        ("service", "service", "service_id"),
        ("doctor", "doctor", "doctor_id"),
        ("device", "device", "device_key"),
    ):
        entity = getattr(selector, field)
        if entity is None or entity.ref is None:
            continue
        value = context.semantic_context.resolve(entity.ref, expected_kind=kind)
        if value is not None:
            params[key] = value
    if selector.date is not None:
        params["date"] = selector.date.model_dump(mode="json")
    if selector.time is not None:
        params["time"] = selector.time.model_dump(mode="json")
    return params


def _service_requires_laser_device(
    operation: TurnOperation,
    context: PlannerContext,
) -> bool:
    service = operation.entities.service
    if service is None or service.ref is None:
        return False
    target = context.semantic_context.reference_map.get(service.ref)
    if target is None or target.kind != "service":
        return False
    return target.metadata.get("requires_laser_device") is True


def _slot_ambiguity_field(step: PlanStep) -> ClarificationField:
    params = step.write_intent.parameters if step.write_intent is not None else {}
    if "doctor_id" not in params:
        return "doctor"
    if bool(step.facts.get("service_requires_laser_device")) and "device_key" not in params:
        return "device"
    return "selection"


def _clarify(
    *,
    index: int,
    operation: TurnOperation,
    field: ClarificationField,
    goal: ResponseGoal = "clarification",
    facts: dict[str, object] | None = None,
) -> PlanStep:
    return PlanStep(
        operation_index=index,
        operation_type=operation.type,
        disposition="clarify",
        response_goal=goal,
        clarification_field=field,
        facts=dict(facts or {}),
    )


def _safety_handoff(turn: TiaTurnUnderstanding) -> TurnPlan | None:
    signals = set(turn.safety_signals)
    if "urgent_medical" in signals:
        return TurnPlan(steps=[], handoff_category="medical", handoff_priority="urgent")
    if "medical" in signals:
        return TurnPlan(steps=[], handoff_category="medical", handoff_priority="high")
    if "payment_dispute" in signals:
        return TurnPlan(steps=[], handoff_category="payment", handoff_priority="normal")
    if "complaint" in signals:
        return TurnPlan(steps=[], handoff_category="complaint", handoff_priority="high")
    if "privacy_issue" in signals:
        return TurnPlan(steps=[], handoff_category="customer_request", handoff_priority="high")
    return None


def _selection_snapshot(context: PlannerContext) -> OptionSnapshot | None:
    state = context.active_task
    if state is not None and option_snapshot_is_current(state, now=context.now):
        return state.option_snapshot
    pending = context.pending_choice
    if pending is not None and context.now < pending.expires_at:
        return pending
    return None


def _selected_snapshot_option(
    operation: TurnOperation,
    context: PlannerContext,
) -> OptionChoice | None:
    snapshot = _selection_snapshot(context)
    if snapshot is None or operation.selection is None:
        return None

    selection = operation.selection
    if selection.kind == "index" and selection.index is not None:
        index = selection.index - 1
        if 0 <= index < len(snapshot.options):
            return snapshot.options[index]
        return None
    if selection.kind == "ref" and selection.ref is not None:
        matches = [item for item in snapshot.options if item.ref == selection.ref]
        return matches[0] if len(matches) == 1 else None
    if selection.kind == "time" and selection.time is not None:
        matches = [
            item
            for item in snapshot.options
            if str(item.payload.get("start_time_24h") or "")[:5] == selection.time[:5]
        ]
        return matches[0] if len(matches) == 1 else None
    return None


def _plan_select_active(index: int, operation: TurnOperation, context: PlannerContext) -> PlanStep:
    state = context.active_task
    snapshot = _selection_snapshot(context)
    selected = _selected_snapshot_option(operation, context)
    if snapshot is None or selected is None:
        return _clarify(index=index, operation=operation, field="selection")

    purpose = snapshot.purpose
    if purpose == "appointment" and snapshot.lifecycle_action is not None:
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="clarify",
            response_goal="clarification",
            clarification_field="date" if snapshot.lifecycle_action == "reschedule" else "intent",
            facts={
                "choice_purpose": purpose,
                "lifecycle_action": snapshot.lifecycle_action,
                "selected_option": selected.model_dump(mode="json"),
            },
        )

    if state is None or state.option_snapshot is None:
        return _clarify(index=index, operation=operation, field="selection")

    if purpose == "booking_slot":
        authorized = (
            operation.execution_intent == "execute"
            and state.task_type == "booking"
            and state.write_authorization.authorized
            and state.write_authorization.operation == "booking"
        )
        if not authorized:
            return PlanStep(
                operation_index=index,
                operation_type=operation.type,
                disposition="respond",
                state_action="select_active",
                response_goal="present_availability",
                facts={"selected_option": selected.model_dump(mode="json")},
            )
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="write_ready",
            state_action="select_active",
            write_intent=WriteIntent(
                kind="booking",
                authorized=True,
                parameters=dict(selected.payload),
                requires_verification=True,
            ),
            response_goal="booking_completed",
            facts={"selected_option_ref": selected.ref},
        )

    if purpose == "reschedule_slot":
        authorized = (
            operation.execution_intent == "execute"
            and isinstance(state, RescheduleTaskState)
            and state.write_authorization.authorized
            and state.write_authorization.operation == "reschedule"
        )
        if not authorized:
            return _clarify(index=index, operation=operation, field="selection")

        target = getattr(state, "target", None)
        target_appointment_id = str(getattr(target, "appointment_id", "") or "").strip()
        selected_appointment_id = str(selected.payload.get("appointment_id") or "").strip()
        if not target_appointment_id:
            return _clarify(index=index, operation=operation, field="appointment")
        if selected_appointment_id and selected_appointment_id != target_appointment_id:
            return PlanStep(
                operation_index=index,
                operation_type=operation.type,
                disposition="blocked",
                response_goal="clarification",
                clarification_field="appointment",
                facts={
                    "reason": "reschedule_target_conflict",
                    "selected_option_ref": selected.ref,
                },
            )

        parameters = dict(selected.payload)
        parameters["appointment_id"] = target_appointment_id
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="write_ready",
            state_action="select_active",
            write_intent=WriteIntent(
                kind="reschedule",
                authorized=True,
                parameters=parameters,
                requires_verification=True,
            ),
            response_goal="reschedule_completed",
            facts={"selected_option_ref": selected.ref},
        )

    return PlanStep(
        operation_index=index,
        operation_type=operation.type,
        disposition="state_update",
        state_action="select_active",
        facts={
            "choice_purpose": purpose,
            "selected_option": selected.model_dump(mode="json"),
        },
    )


def _informational_write_read(
    index: int,
    operation: TurnOperation,
    params: dict[str, object],
) -> PlanStep | None:
    """Fail closed to reads when the semantic action is mentioned without execution authority."""
    if operation.execution_intent == "execute":
        return None
    if operation.type == "book":
        if "service_id" not in params:
            return _clarify(index=index, operation=operation, field="service")
        if "date" in params:
            return PlanStep(
                operation_index=index,
                operation_type=operation.type,
                disposition="read",
                reads=[ReadRequest(kind="availability", parameters=params)],
                response_goal="present_availability",
                facts=params,
            )
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="service_catalog", parameters={"service_id": params["service_id"]})],
            response_goal="answer_service",
            facts=params,
        )
    if operation.type in {"confirm_appointment", "cancel_appointment"}:
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="appointments", parameters=params)],
            response_goal="answer_customer_history",
            facts=params,
        )
    if operation.type == "reschedule":
        reads = [ReadRequest(kind="appointments", parameters=params)]
        if "date" in params and "service_id" in params:
            reads.append(ReadRequest(kind="availability", parameters={**params, "reschedule": True}))
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=reads,
            response_goal="present_availability" if len(reads) > 1 else "answer_customer_history",
            facts=params,
        )
    if operation.type == "buy_package":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="package_offers", parameters=params)],
            response_goal="package_information",
            facts=params,
        )
    if operation.type == "buy_pulse_pack":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="pulse_pack_offers", parameters=params)],
            response_goal="pulse_information",
            facts=params,
        )
    if operation.type in {"follow_up", "marketing_update"}:
        return _clarify(index=index, operation=operation, field="intent")
    return None


def _plan_operation(
    index: int,
    operation: TurnOperation,
    context: PlannerContext,
    *,
    compound_book: bool = False,
) -> PlanStep:
    params, ambiguous = _base_parameters(operation, context)

    if operation.type == "human_support":
        financial = operation.financial_ownership == "reception"
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="handoff",
            response_goal="handoff",
            facts={
                "category": "payment" if financial else "customer_request",
                "priority": "normal",
                "preserve_active_task": financial,
            },
        )
    if operation.type == "social":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="respond",
            response_goal="social_ack",
        )
    if operation.type == "unclear":
        return _clarify(index=index, operation=operation, field="intent")
    if operation.type == "cancel_active":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="state_update",
            state_action="cancel_active",
            response_goal="clarification",
        )
    if operation.type == "select_active":
        return _plan_select_active(index, operation, context)
    if operation.type == "continue_active":
        if context.active_task is None:
            return _clarify(index=index, operation=operation, field="intent")
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="state_update",
            state_action="update_active",
            facts=params,
        )

    informational = _informational_write_read(index, operation, params)
    if informational is not None:
        return informational

    if ambiguous.get("service") and operation.type not in {
        "confirm_appointment",
        "cancel_appointment",
        "reschedule",
    }:
        return _clarify(index=index, operation=operation, field="service", goal="ask_service_choice")
    if ambiguous.get("doctor") and not (
        operation.type == "availability" or (compound_book and operation.type == "book")
    ):
        return _clarify(index=index, operation=operation, field="doctor", goal="ask_doctor_choice")
    if ambiguous.get("device") and operation.type in {
        "doctor_info",
        "availability",
        "book",
        "package_info",
        "buy_package",
        "pulse_info",
        "buy_pulse_pack",
        "refund_quote",
        "reschedule",
    }:
        return _clarify(index=index, operation=operation, field="device")
    if ambiguous.get("appointment") and operation.type not in {
        "confirm_appointment",
        "cancel_appointment",
        "reschedule",
    }:
        return _clarify(
            index=index,
            operation=operation,
            field="appointment",
            goal="ask_appointment_choice",
        )
    if ambiguous.get("package"):
        return _clarify(index=index, operation=operation, field="package", goal="ask_package_choice")

    if operation.type == "pricing":
        package_pricing = "package_sessions" in params or "package_id" in params
        if package_pricing:
            if "service_id" not in params and "package_id" not in params:
                return _clarify(index=index, operation=operation, field="service")
            return PlanStep(
                operation_index=index,
                operation_type=operation.type,
                disposition="read",
                reads=[ReadRequest(kind="package_offers", parameters=params)],
                response_goal="answer_price",
                facts=params,
            )
        if "service_id" not in params:
            return _clarify(index=index, operation=operation, field="service")
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="service_catalog", parameters={"service_id": params["service_id"]})],
            response_goal="answer_price",
            facts=params,
        )

    if operation.type == "service_info":
        service_params = (
            {"service_id": params["service_id"]}
            if "service_id" in params
            else {}
        )
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="service_catalog", parameters=service_params)],
            response_goal="answer_service",
            facts=params,
        )

    if operation.type == "doctor_info":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="doctors", parameters=params)],
            response_goal="answer_doctor",
            facts=params,
        )

    if operation.type == "clinic_info":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="clinic_info")],
            response_goal="answer_clinic_info",
        )

    if operation.type == "payment_info":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="clinic_info")],
            response_goal="answer_clinic_info",
            facts={
                "booking_requires_payment": False,
                "payment_execution_owner": "reception",
                "active_booking_in_progress": getattr(context.active_task, "task_type", None) == "booking",
            },
        )

    if operation.type == "availability":
        if "service_id" not in params:
            return _clarify(index=index, operation=operation, field="service")
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="availability", parameters=params)],
            response_goal="present_availability",
            facts=params,
        )

    if operation.type == "book":
        # A same-turn multi-service visit must select one common doctor deterministically.
        # Single-service bookings keep the existing explicit doctor-choice behavior.
        if "doctor_ids" in params and not compound_book:
            return _clarify(index=index, operation=operation, field="doctor", goal="ask_doctor_choice")
        if "service_id" not in params:
            return _clarify(index=index, operation=operation, field="service")
        if "date" not in params:
            return _clarify(index=index, operation=operation, field="date")
        exact_time = operation.entities.time is not None and operation.entities.time.mode == "exact"
        requires_device = _service_requires_laser_device(operation, context)
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="availability", parameters=params)],
            write_intent=WriteIntent(
                kind="booking",
                authorized=True,
                parameters=params,
                requires_verification=True,
            ),
            state_action="start_booking",
            response_goal="present_availability",
            facts={
                **params,
                "exact_time_requested": exact_time,
                "service_requires_laser_device": requires_device,
            },
        )

    if operation.type == "appointment_list":
        appointment_params = {
            key: value
            for key, value in params.items()
            if key in {"appointment_id", "service_id", "doctor_id", "device_key", "date", "time"}
        }
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="appointments", parameters=appointment_params)],
            response_goal="answer_customer_history",
        )

    if operation.type in {"confirm_appointment", "cancel_appointment"}:
        source_params = _source_appointment_parameters(operation, context, fallback=params)
        kind: WriteKind = (
            "confirm_appointment" if operation.type == "confirm_appointment" else "cancel_appointment"
        )
        goal: ResponseGoal = (
            "appointment_confirmed" if operation.type == "confirm_appointment" else "cancellation_completed"
        )
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="appointments", parameters=source_params)],
            write_intent=WriteIntent(kind=kind, authorized=True, parameters=source_params),
            response_goal=goal,
            facts={"source_appointment": source_params},
        )

    if operation.type == "reschedule":
        if "doctor_ids" in params:
            return _clarify(index=index, operation=operation, field="doctor")
        source_params = _source_appointment_parameters(operation, context, fallback=params)
        replacement_params = {
            key: value
            for key, value in params.items()
            if key not in {"appointment_id", "appointment_ids"}
        }
        if "date" not in replacement_params:
            return PlanStep(
                operation_index=index,
                operation_type=operation.type,
                disposition="clarify",
                reads=[ReadRequest(kind="appointments", parameters=source_params)],
                state_action="start_reschedule",
                response_goal="clarification",
                clarification_field="date",
                facts={**replacement_params, "source_appointment": source_params},
            )
        exact_time = operation.entities.time is not None and operation.entities.time.mode == "exact"
        requires_device = _service_requires_laser_device(operation, context)
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[
                ReadRequest(kind="appointments", parameters=source_params),
                ReadRequest(
                    kind="availability",
                    parameters={**replacement_params, "reschedule": True},
                ),
            ],
            write_intent=WriteIntent(
                kind="reschedule",
                authorized=True,
                parameters=replacement_params,
            ),
            state_action="start_reschedule",
            response_goal="present_availability",
            facts={
                **replacement_params,
                "source_appointment": source_params,
                "exact_time_requested": exact_time,
                "service_requires_laser_device": requires_device,
            },
        )

    if operation.type == "customer_profile":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[
                ReadRequest(
                    kind="customer_profile",
                    parameters={
                        "requested_patient_details": list(
                            operation.requested_patient_details
                        )
                    },
                )
            ],
            response_goal="answer_customer_profile",
        )

    if operation.type == "customer_history":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="customer_history")],
            response_goal="answer_customer_history",
        )

    if operation.type == "pulse_info":
        details = set(operation.requested_pulse_details)
        if not details:
            details = {"balance"}
        reads: list[ReadRequest] = []
        if "balance" in details:
            reads.append(ReadRequest(kind="pulse_balance", parameters=params))
        if "owned_packs" in details:
            reads.append(ReadRequest(kind="pulse_packs", parameters=params))
        if "offers" in details:
            offer_params = dict(params)
            if "overage_price" in details:
                offer_params.pop("pulse_count", None)
            reads.append(ReadRequest(kind="pulse_pack_offers", parameters=offer_params))
        if "overage_price" in details:
            reads.append(ReadRequest(kind="pulse_billing_settings", parameters=params))
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=reads,
            response_goal="pulse_information",
            facts=params,
        )

    if operation.type == "package_info":
        details = set(operation.requested_package_details) or {"owned"}
        reads: list[ReadRequest] = []
        if "owned" in details:
            reads.append(ReadRequest(kind="customer_packages", parameters=params))
        if "offers" in details:
            reads.append(ReadRequest(kind="package_offers", parameters=params))
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=reads,
            response_goal="package_information",
            facts=params,
        )

    if operation.type == "buy_package":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="package_offers", parameters=params)],
            write_intent=WriteIntent(kind="buy_package", authorized=True, parameters=params),
            response_goal="package_purchased",
            facts=params,
        )

    if operation.type == "buy_pulse_pack":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="pulse_pack_offers", parameters=params)],
            write_intent=WriteIntent(kind="buy_pulse_pack", authorized=True, parameters=params),
            response_goal="pulse_pack_purchased",
            facts=params,
        )

    if operation.type == "refund_quote":
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="read",
            reads=[ReadRequest(kind="package_refund_quote", parameters=params)],
            response_goal="package_refund_quote",
            facts=params,
        )

    if operation.type == "follow_up":
        if "follow_up_at_local" not in params:
            return _clarify(index=index, operation=operation, field="follow_up_time")
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="write_ready",
            write_intent=WriteIntent(
                kind="follow_up",
                authorized=True,
                parameters=params,
                requires_verification=False,
            ),
            response_goal="follow_up_created",
            facts=params,
        )

    if operation.type == "marketing_update":
        if "marketing_consent" not in params:
            return _clarify(index=index, operation=operation, field="marketing_consent")
        return PlanStep(
            operation_index=index,
            operation_type=operation.type,
            disposition="write_ready",
            write_intent=WriteIntent(
                kind="marketing_update",
                authorized=True,
                parameters=params,
                requires_verification=False,
            ),
            response_goal="marketing_updated",
            facts=params,
        )

    return _clarify(index=index, operation=operation, field="intent")


def _safe_read_companion(step: PlanStep) -> bool:
    """Whether a step may safely coexist with an ordinary customer-request handoff."""
    return (
        step.disposition == "read"
        and step.write_intent is None
        and step.state_action == "none"
    )


def plan_turn(turn: TiaTurnUnderstanding, context: PlannerContext) -> TurnPlan:
    safety = _safety_handoff(turn)
    if safety is not None:
        return safety

    steps: list[PlanStep] = []
    executable_book_indexes = {
        index
        for index, operation in enumerate(turn.operations)
        if operation.type == "book" and operation.execution_intent == "execute"
    }
    compound_booking = len(executable_book_indexes) >= 2
    for index, operation in enumerate(turn.operations):
        steps.append(
            _plan_operation(
                index,
                operation,
                context,
                compound_book=compound_booking and index in executable_book_indexes,
            )
        )

    handoff_steps = [step for step in steps if step.disposition == "handoff"]
    if not handoff_steps:
        return TurnPlan(steps=steps)

    handoff = handoff_steps[0]
    companions = [step for step in steps if step.disposition != "handoff"]
    if companions and not all(_safe_read_companion(step) for step in companions):
        companions = []

    return TurnPlan(
        steps=[*companions, handoff],
        handoff_category=str(handoff.facts.get("category") or "customer_request"),
        handoff_priority=str(handoff.facts.get("priority") or "normal"),
    )


def advance_step_after_verification(
    step: PlanStep,
    verification: VerificationFacts,
) -> PlanStep:
    """Promote an authorized semantic write only after deterministic verification."""
    if step.write_intent is None:
        if step.state_action != "start_reschedule":
            return step
        appointment_count = verification.appointment_match_count
        if appointment_count == 1:
            return step.model_copy(
                update={"facts": {**step.facts, **verification.verified_parameters}}
            )
        if appointment_count == 0:
            return step.model_copy(
                update={"disposition": "blocked", "response_goal": "clarification"}
            )
        if appointment_count is not None and appointment_count > 1:
            return step.model_copy(
                update={
                    "disposition": "clarify",
                    "clarification_field": "appointment",
                    "response_goal": "ask_appointment_choice",
                }
            )
        return step
    if not step.write_intent.authorized:
        return step
    if verification.requires_human:
        return step.model_copy(
            update={
                "disposition": "handoff",
                "response_goal": "handoff",
                "facts": {**step.facts, **verification.verified_parameters},
            }
        )

    kind = step.write_intent.kind
    verified_parameters = {
        **step.write_intent.parameters,
        **verification.verified_parameters,
    }

    if kind == "booking":
        if not bool(step.facts.get("exact_time_requested")):
            return step
        count = verification.exact_slot_match_count
        if count == 1:
            if (
                bool(step.facts.get("service_requires_laser_device"))
                and not verified_parameters.get("device_key")
            ):
                return step.model_copy(
                    update={
                        "disposition": "clarify",
                        "clarification_field": "device",
                        "response_goal": "clarification",
                    }
                )
            return step.model_copy(
                update={
                    "disposition": "write_ready",
                    "write_intent": step.write_intent.model_copy(
                        update={"parameters": verified_parameters}
                    ),
                }
            )
        if count == 0:
            return step.model_copy(
                update={"disposition": "blocked", "response_goal": "requested_time_unavailable"}
            )
        if count is not None and count > 1:
            field = _slot_ambiguity_field(step)
            return step.model_copy(
                update={
                    "disposition": "clarify",
                    "clarification_field": field,
                    "response_goal": "ask_doctor_choice" if field == "doctor" else "clarification",
                }
            )
        return step

    if kind in {"confirm_appointment", "cancel_appointment"}:
        count = verification.appointment_match_count
        if count == 1:
            return step.model_copy(
                update={
                    "disposition": "write_ready",
                    "write_intent": step.write_intent.model_copy(
                        update={"parameters": verified_parameters}
                    ),
                }
            )
        if count == 0:
            return step.model_copy(
                update={"disposition": "blocked", "response_goal": "clarification"}
            )
        if count is not None and count > 1:
            return step.model_copy(
                update={
                    "disposition": "clarify",
                    "clarification_field": "appointment",
                    "response_goal": "ask_appointment_choice",
                }
            )
        return step

    if kind == "reschedule":
        appointment_count = verification.appointment_match_count
        if appointment_count is not None and appointment_count != 1:
            if appointment_count > 1:
                return step.model_copy(
                    update={
                        "disposition": "clarify",
                        "clarification_field": "appointment",
                        "response_goal": "ask_appointment_choice",
                    }
                )
            return step.model_copy(
                update={"disposition": "blocked", "response_goal": "clarification"}
            )
        if not bool(step.facts.get("exact_time_requested")):
            return step
        slot_count = verification.exact_slot_match_count
        if slot_count == 1 and appointment_count == 1:
            if (
                bool(step.facts.get("service_requires_laser_device"))
                and not verified_parameters.get("device_key")
            ):
                return step.model_copy(
                    update={
                        "disposition": "clarify",
                        "clarification_field": "device",
                        "response_goal": "clarification",
                    }
                )
            return step.model_copy(
                update={
                    "disposition": "write_ready",
                    "write_intent": step.write_intent.model_copy(
                        update={"parameters": verified_parameters}
                    ),
                }
            )
        if slot_count == 0:
            return step.model_copy(
                update={"disposition": "blocked", "response_goal": "requested_time_unavailable"}
            )
        if slot_count is not None and slot_count > 1:
            field = _slot_ambiguity_field(step)
            return step.model_copy(
                update={
                    "disposition": "clarify",
                    "clarification_field": field,
                    "response_goal": "ask_doctor_choice" if field == "doctor" else "clarification",
                }
            )
        return step

    if kind == "buy_pulse_pack":
        count = verification.pulse_offer_match_count
        if count == 1:
            return step.model_copy(
                update={
                    "disposition": "write_ready",
                    "write_intent": step.write_intent.model_copy(
                        update={"parameters": verified_parameters}
                    ),
                }
            )
        if count == 0:
            return step.model_copy(
                update={"disposition": "blocked", "response_goal": "pulse_information"}
            )
        if count is not None and count > 1:
            return step.model_copy(
                update={
                    "disposition": "clarify",
                    "clarification_field": "intent",
                    "response_goal": "clarification",
                }
            )
        return step

    if kind == "buy_package":
        count = verification.package_offer_match_count
        if count == 1:
            return step.model_copy(
                update={
                    "disposition": "write_ready",
                    "write_intent": step.write_intent.model_copy(
                        update={"parameters": verified_parameters}
                    ),
                }
            )
        if count == 0:
            return step.model_copy(
                update={"disposition": "blocked", "response_goal": "package_information"}
            )
        if count is not None and count > 1:
            return step.model_copy(
                update={
                    "disposition": "clarify",
                    "clarification_field": "package",
                    "response_goal": "ask_package_choice",
                }
            )
        return step

    return step.model_copy(
        update={
            "disposition": "write_ready",
            "write_intent": step.write_intent.model_copy(update={"parameters": verified_parameters}),
        }
    )