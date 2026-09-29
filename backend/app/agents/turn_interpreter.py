from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from app.agents.clinic_grounding import validate_grounded_entity_ids
from app.agents.llm_runtime import invoke_with_model_chain
from app.agents.model_provider import (
    build_realtime_interpreter_emergency_model,
    build_realtime_interpreter_fallback_model,
    build_realtime_interpreter_model,
)
from app.agents.structured_output import (
    StructuredOutputError,
    invoke_typed_structured_output,
)
from app.agents.turn_models import (
    ClearableFlowEntity,
    FlowSignal,
    FlowTurnDecision,
    HandoffCategory,
    PackageIntent,
    Priority,
    RiskFlag,
    SemanticCapability,
    SemanticCapabilityDecision,
    SemanticDomain,
    SemanticEntityHints,
    _require_all_schema_fields,
)
from app.core.config import settings
from app.models.conversation_flow_state import ConversationFlowState

UnifiedTurnAction = Literal[
    "continue",
    "modify",
    "select_option",
    "cancel_flow",
    "interrupt",
]


class UnifiedTurnDecision(BaseModel):
    """Single semantic contract for fresh turns and active workflows.

    The model interprets meaning only. Deterministic Python remains authoritative
    for capability policy, workflow transitions, tool authorization, writes, money,
    and persisted clinic data.
    """

    model_config = ConfigDict(
        extra="forbid", json_schema_extra=_require_all_schema_fields
    )

    domains: list[SemanticDomain]
    capabilities: list[SemanticCapability]
    risk_flags: list[RiskFlag]
    flow_signal: FlowSignal
    package_intent: PackageIntent = "none"
    action: UnifiedTurnAction
    entity_hints: SemanticEntityHints
    clear_entity_fields: list[ClearableFlowEntity] = Field(default_factory=list)
    selection_index: int | None
    selection_time: str | None
    missing_information: list[str]
    recommended_handoff_category: HandoffCategory
    recommended_handoff_priority: Priority
    confidence: float
    reason: str

    def as_semantic_decision(self) -> SemanticCapabilityDecision:
        return SemanticCapabilityDecision(
            domains=self.domains,
            capabilities=self.capabilities,
            risk_flags=self.risk_flags,
            flow_signal=self.flow_signal,
            package_intent=self.package_intent,
            entity_hints=self.entity_hints,
            missing_information=self.missing_information,
            recommended_handoff_category=self.recommended_handoff_category,
            recommended_handoff_priority=self.recommended_handoff_priority,
            confidence=self.confidence,
            reason=self.reason,
        )

    def as_flow_turn_decision(self) -> FlowTurnDecision:
        return FlowTurnDecision(
            action=self.action,
            capabilities=self.capabilities,
            risk_flags=self.risk_flags,
            package_intent=self.package_intent,
            entity_hints=self.entity_hints,
            clear_entity_fields=self.clear_entity_fields,
            selection_index=self.selection_index,
            selection_time=self.selection_time,
            missing_information=self.missing_information,
            recommended_handoff_category=self.recommended_handoff_category,
            recommended_handoff_priority=self.recommended_handoff_priority,
            confidence=self.confidence,
            reason=self.reason,
        )


def _message_text(message: BaseMessage, *, limit: int = 400) -> str:
    if not isinstance(message.content, str) or not message.content.strip():
        return ""
    text = " ".join(message.content.strip().split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _latest_customer_turn(history: list[BaseMessage]) -> str:
    for message in reversed(history):
        if isinstance(message, HumanMessage):
            text = _message_text(message, limit=1200)
            if text:
                return text
    return ""


def _history_excerpt(history: list[BaseMessage]) -> str:
    """Compatibility helper: semantic planning still treats the latest customer turn as authoritative."""
    return _latest_customer_turn(history)


def _recent_conversation_excerpt(
    history: list[BaseMessage],
    *,
    max_messages: int = 4,
) -> str:
    """Provide small conversational context only for reference resolution.

    Persisted workflow state remains the operational memory. A short recent excerpt
    lets the semantic model resolve pronouns, corrections, and follow-up questions
    without duplicating the latest turn or reactivating old intents from a long transcript.
    """
    selected: list[tuple[str, str]] = []
    latest_customer_skipped = False
    for message in reversed(history):
        if isinstance(message, HumanMessage):
            if not latest_customer_skipped:
                latest_customer_skipped = True
                continue
            role = "customer"
        elif isinstance(message, AIMessage):
            role = "assistant"
        else:
            continue
        text = _message_text(message)
        if not text:
            continue
        selected.append((role, text))
        if len(selected) >= max_messages:
            break
    selected.reverse()
    return "\n".join(f"{role}: {text}" for role, text in selected)


def _option_summary(flow: ConversationFlowState | None) -> dict[str, object]:
    if flow is None or not isinstance(flow.option_snapshot, dict):
        return {}

    summary: dict[str, object] = {}
    windows = flow.option_snapshot.get("availability_windows")
    if isinstance(windows, list) and windows:
        summary["availability_windows"] = [
            {
                "doctor_id": window.get("doctor_id"),
                "doctor_name": window.get("doctor_name"),
                "laser_device_key": window.get("laser_device_key") or window.get("device_key"),
                "laser_device_name": window.get("laser_device_name"),
                "start_time_24h": window.get("start_time_24h"),
                "end_time_24h": window.get("end_time_24h"),
            }
            for window in windows[:12]
            if isinstance(window, dict)
        ]
    else:
        slots = flow.option_snapshot.get("slots")
        if isinstance(slots, list):
            summary["slots"] = [
                {
                    "index": index + 1,
                    "start_time_24h": slot.get("start_time_24h"),
                    "end_time_24h": slot.get("end_time_24h"),
                    "doctor_name": slot.get("doctor_name"),
                    "laser_device_key": slot.get("laser_device_key") or slot.get("device_key"),
                    "laser_device_name": slot.get("laser_device_name"),
                }
                for index, slot in enumerate(slots[:8])
                if isinstance(slot, dict)
            ]

    choice_specs = (
        ("services", ("service_name", "name")),
        ("doctors", ("doctor_name", "name")),
    )
    for collection_name, name_keys in choice_specs:
        choices = flow.option_snapshot.get(collection_name)
        if not isinstance(choices, list):
            continue
        summarized: list[dict[str, object]] = []
        for index, choice in enumerate(choices[:8]):
            if not isinstance(choice, dict):
                continue
            display_name = next(
                (choice.get(key) for key in name_keys if choice.get(key)),
                None,
            )
            canonical_id = choice.get("service_id") or choice.get("doctor_id") or choice.get("id")
            summarized.append(
                {
                    "index": index + 1,
                    "id": canonical_id,
                    "name": display_name,
                }
            )
        if summarized:
            summary[collection_name] = summarized
    return summary


def _semantic_catalog_for_single_location(
    clinic_catalog: dict[str, object],
) -> dict[str, object]:
    """Return only facts required for semantic interpretation.

    The full canonical catalog remains unchanged and authoritative for grounding,
    pricing, availability, and execution. This copy removes duplicated and
    operational fields before sending clinic context to the LLM.
    """
    semantic: dict[str, object] = {"services": [], "doctors": []}

    services = clinic_catalog.get("services")
    if isinstance(services, list):
        semantic_services: list[dict[str, object]] = []
        for row in services:
            if not isinstance(row, dict):
                continue
            item: dict[str, object] = {}
            for key in ("id", "name", "category", "requires_laser_device"):
                value = row.get(key)
                if value not in (None, "", []):
                    item[key] = value
            description = str(row.get("description") or "").strip()
            if description:
                item["description"] = description[:240]
            raw_devices = row.get("laser_devices")
            if isinstance(raw_devices, list):
                devices = [
                    {
                        key: device.get(key)
                        for key in ("device_key", "device_name")
                        if device.get(key) not in (None, "")
                    }
                    for device in raw_devices
                    if isinstance(device, dict)
                ]
                devices = [device for device in devices if device]
                if devices:
                    item["laser_devices"] = devices
            if item:
                semantic_services.append(item)
        semantic["services"] = semantic_services

    doctors = clinic_catalog.get("doctors")
    if isinstance(doctors, list):
        semantic_doctors: list[dict[str, object]] = []
        for row in doctors:
            if not isinstance(row, dict):
                continue
            item = {
                key: row.get(key)
                for key in ("id", "name", "specialization", "service_ids")
                if row.get(key) not in (None, "", [])
            }
            if item:
                semantic_doctors.append(item)
        semantic["doctors"] = semantic_doctors

    appointments = clinic_catalog.get("appointments")
    if isinstance(appointments, list):
        appointment_keys = (
            "id", "appointment_id", "service_id", "service_name",
            "doctor_id", "doctor_name", "status", "start_local",
            "laser_device_key", "laser_device_name",
        )
        semantic["appointments"] = [
            {
                key: row.get(key)
                for key in appointment_keys
                if row.get(key) not in (None, "", [])
            }
            for row in appointments
            if isinstance(row, dict)
        ]

    return semantic

def _single_location_entity_state(state: object) -> dict[str, object]:
    if not isinstance(state, dict):
        return {}
    blocked = {
        "branch",
        "branches",
        "branch_id",
        "branch_name",
        "branch_query",
        "branch_candidate_ids",
    }
    return {key: value for key, value in state.items() if key not in blocked}


def _normalize_single_location_decision(
    decision: UnifiedTurnDecision,
) -> UnifiedTurnDecision:
    """Enforce the single-location product invariant after semantic inference."""
    capabilities = [
        capability
        for capability in decision.capabilities
        if str(capability) != "branch_discovery"
    ]
    hints = decision.entity_hints.model_copy(
        update={
            "branch_query": None,
            "branch_id": None,
            "branch_candidate_ids": [],
        }
    )
    clear_fields = [
        field
        for field in decision.clear_entity_fields
        if str(field)
        not in {"branch_query", "branch_id", "branch_candidate_ids"}
    ]
    missing_information = [
        item
        for item in decision.missing_information
        if item not in {"branch", "branch_id", "branch_query", "branch_choice"}
    ]
    return decision.model_copy(
        update={
            "capabilities": capabilities,
            "entity_hints": hints,
            "clear_entity_fields": clear_fields,
            "missing_information": missing_information,
        }
    )


def _interpreter_system_prompt(
    *,
    timezone_name: str,
    local_now: datetime,
    active_flow: bool,
) -> str:
    return f"""You are Linka's single semantic turn interpreter for an aesthetic clinic. Return only the structured schema. Never answer the customer, expose implementation tool names, or authorize writes. Python and the canonical clinic data remain execution authorities.

AUTHORITY AND CAPABILITY MAP
Interpret the latest customer turn; recent conversation is only for references/corrections and persisted workflow state is operational memory, not permission to repeat old intents. Use the smallest capability set required. Map meaning explicitly: service details/existence/duration -> service_information; price -> pricing; which doctor/doctor information -> doctor_discovery; clinic address/phone/hours -> clinic_information; availability/when can I come -> availability_discovery; a clear request to create a booking -> appointment_creation; upcoming appointments -> appointment_list; current-customer profile -> customer_profile; past visits/services/payments or payment status -> customer_history; package details/remaining sessions -> package_information; buying/starting a package -> package_purchase; package cancellation refund amount -> package_refund_quote; reminder/future follow-up -> follow_up_request; marketing consent -> marketing_preferences; explicit human request -> human_support. Combine capabilities when one turn clearly asks for several of these. Do not turn an information question into a write capability.

FLOW AND VERIFIED REFERENCES
For an active flow: continue keeps requirements; modify changes them; select_option chooses a presented option; cancel_flow stops the flow; interrupt transfers ownership. Greetings, acknowledgements, language changes, recall questions, and a harmless side read must not mutate the flow. A verified service/doctor option set remains a valid reference target across one harmless side read or factual answer; an intervening factual answer is not a new option list. Resolve short positional/pronominal follow-ups from that verified set only, and preserve ambiguity when several options still fit. For numbered choices set selection_index.

TIME, AVAILABILITY, AND BOOKING FOLLOW-UPS
Resolve clear relative dates/times from the clinic clock. Exact appointment starts use requested_start_time; search constraints use not_before_time/not_after_time. When availability is presented and the customer responds, distinguish semantically between selecting an appointment start and changing the availability search. If the assistant's immediately preceding question asks which verified time to book and the customer chooses one specific start, including by referring to the start or beginning of a presented period, use action=select_option, selection_time=HH:MM, and include appointment_creation; the customer does not need to repeat the word 'book'. Use not_before_time/not_after_time only when the customer actually wants to search, filter, or constrain availability, not when choosing the appointment itself. Never infer a time that cannot resolve against the verified presented options. If a previously rejected exact time is broadened/replaced, clear requested_start_time and any stale opposite bounds. Never silently round an exact minute. If the same exact time belongs to multiple presented doctors, preserve doctor ambiguity. If the customer rejects a presented date/time and asks for another day, action=modify, preserve relevant service/doctor/time requirements, clear the rejected requested_date as needed, and refresh discovery rather than selecting the old offer.

FRESH BOOKING, RESCHEDULE, AND EXISTING APPOINTMENT EDITS
A fresh explicit booking with sufficient grounded details may include availability_discovery + appointment_creation; Python still verifies exact availability before writing. A question about whether a time is possible is exploratory and not write authorization. FRESH RESCHEDULE AUTHORIZATION: when a fresh turn clearly commands changing one existing appointment now and supplies an exact replacement date/time, use appointment_reschedule, flow_signal=start_reschedule, action=modify, capture requested_date/requested_start_time, and do not require a second confirmation; Python verifies the target before writing. For a presented replacement availability window, choosing a replacement start uses action=select_option + selection_time + appointment_reschedule. If the customer instead says when the replacement search should begin/end or supplies a broader bound, use action=modify + not_before_time/not_after_time. For existing appointment service/device edits, treat it as appointment_reschedule even when the requested date/time stays the same. Resolve the existing appointment from current-patient appointments, never from replacement target fields. If the old doctor was not reaffirmed and is incompatible with a newly selected service, clear the old doctor requirement.

COMPOUND REQUESTS
When the latest turn clearly asks for 2+ independently executable appointment/package operations, emit requested_items in customer order (max 6), one operation per item. Each item owns its own service, doctor, device, date/time, and package intent. Do not create requested_items for alternatives, comparisons, uncertain matching, or one operation with multiple candidates. Package purchase plus its first appointment emits package_purchase first and appointment second with package_intent=use_existing. A direct clarification to the immediately preceding assistant question about a missing compound field reconstructs the same requested_items and changes only that field. Python grounds and sequences the items deterministically. Never collapse two explicitly requested services into ambiguity.

PACKAGES
Use package_intent=none for an ordinary appointment, inquire for package information/comparison, purchase when obtaining/starting a package, use_existing when this appointment should consume an existing package, and avoid_existing when the customer explicitly wants a normal paid appointment instead. For purchase, capture package_sessions_count only for an explicitly chosen configured 3/6/9 tier and laser_device_key only for an explicitly chosen configured device. Package purchase/inquiry is not a booking unless a specific appointment is separately requested. An existing package for one service must not affect a different service. A pure package cancellation refund amount question is package_refund_quote and read-only. Operational package/price/booking guidance is not medical unless the customer asks for clinical judgment.

APPOINTMENTS, CUSTOMER DATA, AND SUPPORT
Cancellation: include appointment_cancellation for a cancellation request or direct clarification to the preceding question about which appointment; select appointment_id only from supplied current-patient appointments when context identifies exactly one, otherwise keep ambiguity. Confirmation uses appointment_confirmation only to change a pending appointment to confirmed; asking whether existing details are correct is read-only. Past visits/services/payments and simple paid/how-paid/amount-paid questions for the current customer use customer_history and do not set payment risk by themselves. Payment disputes, corrections, failed-payment problems, or financial-record changes may use payment risk/handoff. Remaining package sessions use package_information. Medical suitability, diagnosis, contraindications, symptoms, adverse effects, or health-based treatment recommendations set medical risk/handoff; a catalog medical-review flag alone does not make an operational request medical. Explicit requests for a human use human_support.

SINGLE LOCATION AND GROUNDING
This is a single-location customer experience: never emit branch_discovery or branch_query/branch_id/branch_candidate_ids. Resolve services, doctors, devices, and existing appointments only from the supplied canonical catalog. Respect doctor.service_ids compatibility. Emit one canonical ID only when one record is clearly intended; otherwise emit plausible candidates and preserve ambiguity. Never invent IDs. requested_date must be resolved YYYY-MM-DD when clear. Resolve colloquial clock hours using plausible clinic hours unless AM/PM or another explicit meaning is given. When the latest turn replaces a service/doctor/time constraint in an active flow, action=modify and the latest meaning owns that requirement; clear stale incompatible persisted fields. A new service must refresh availability rather than reuse the previous service's snapshot. If a doctor set from the preceding exchange is referenced collectively for earliest availability, inherit the unambiguous service, use availability_discovery, and leave doctor fields unselected so the adapter compares compatible doctors.

Clinic timezone: {timezone_name}. Clinic local date/time: {local_now.isoformat()}. Active workflow present: {str(active_flow).lower()}"""

def _snapshot_has_exact_time(
    flow: ConversationFlowState,
    exact_time: str,
    *,
    doctor_id: str | None,
    requested_date: str | None,
) -> bool:
    """Verify a semantic exact-time choice against the already-presented snapshot."""
    snapshot = flow.option_snapshot if isinstance(flow.option_snapshot, dict) else {}
    snapshot_date = str(snapshot.get("date") or "").strip()
    if requested_date and snapshot_date and str(requested_date) != snapshot_date:
        return False
    normalized = str(exact_time).strip()[:5]
    slots = snapshot.get("slots")
    if not isinstance(slots, list):
        return False
    matches = 0
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        if str(slot.get("start_time_24h") or "").strip()[:5] != normalized:
            continue
        if doctor_id and str(slot.get("doctor_id") or "") != str(doctor_id):
            continue
        matches += 1
    return matches == 1


def _normalize_active_booking_decision(
    decision: UnifiedTurnDecision,
    flow: ConversationFlowState | None,
) -> UnifiedTurnDecision:
    """Normalize structured follow-ups using semantic output + verified flow state only."""
    if flow is None or not flow.is_active or flow.flow_type != "booking":
        return decision

    existing = flow.entity_state if isinstance(flow.entity_state, dict) else {}
    old_service_id = str(existing.get("service_id") or "")
    new_service_id = str(decision.entity_hints.service_id or "")
    service_changed = bool(old_service_id and new_service_id and old_service_id != new_service_id)

    clear_fields = list(decision.clear_entity_fields)
    action = decision.action
    if service_changed:
        action = "modify"
        if (
            not decision.entity_hints.doctor_id
            and not decision.entity_hints.doctor_query
            and not decision.entity_hints.doctor_candidate_ids
        ):
            for field in ("doctor_query", "doctor_id", "doctor_candidate_ids"):
                if field not in clear_fields:
                    clear_fields.append(field)

    exact_time = decision.selection_time or decision.entity_hints.requested_start_time
    capabilities = {str(item) for item in decision.capabilities}
    exact_choice_is_in_snapshot = bool(
        not service_changed
        and exact_time
        and "appointment_creation" in capabilities
        and _snapshot_has_exact_time(
            flow,
            str(exact_time),
            doctor_id=decision.entity_hints.doctor_id,
            requested_date=decision.entity_hints.requested_date,
        )
    )
    if exact_choice_is_in_snapshot:
        return decision.model_copy(
            update={
                "action": "select_option",
                "clear_entity_fields": clear_fields,
                "selection_index": None,
                "selection_time": str(exact_time).strip()[:5],
            }
        )

    if (
        not service_changed
        and exact_time
        and "appointment_creation" in capabilities
        and decision.action == "select_option"
    ):
        # The customer authorized one exact booking time, but the current snapshot
        # is empty or no longer represents that choice. Re-run exact availability
        # through the normal modify/read path in this same turn; do not inspect text
        # and do not guess from an older snapshot.
        for field in ("not_before_time", "not_after_time"):
            if field not in clear_fields:
                clear_fields.append(field)
        return decision.model_copy(
            update={
                "action": "modify",
                "clear_entity_fields": clear_fields,
                "selection_index": None,
                "selection_time": None,
            }
        )

    return decision.model_copy(update={"action": action, "clear_entity_fields": clear_fields})


def interpret_customer_turn(
    *,
    flow: ConversationFlowState | None,
    history: list[BaseMessage],
    timezone_name: str,
    local_now: datetime,
    clinic_catalog: dict[str, object],
) -> UnifiedTurnDecision:
    """Interpret a customer turn through one grounded semantic model call.

    The returned structure is advisory semantic state. Capability policy, flow CAS,
    tool validation, business rules, and PostgreSQL remain execution authorities.
    """
    active_flow = flow is not None and flow.is_active
    system = SystemMessage(
        content=_interpreter_system_prompt(
            timezone_name=timezone_name,
            local_now=local_now,
            active_flow=active_flow,
        )
    )
    state_payload = {
        "active_flow": active_flow,
        "flow_type": flow.flow_type if active_flow else None,
        "flow_status": flow.status if active_flow else None,
        "flow_capabilities": [
            capability
            for capability in (flow.capabilities if active_flow else [])
            if str(capability) != "branch_discovery"
        ],
        "entity_state": _single_location_entity_state(
            flow.entity_state if active_flow else {}
        ),
        "missing_information": flow.missing_information if active_flow else [],
        "options": _option_summary(flow if active_flow else None),
    }
    semantic_catalog = _semantic_catalog_for_single_location(clinic_catalog)
    user = HumanMessage(
        content=(
            "PostgreSQL clinic catalog (canonical IDs; do not invent IDs):\n"
            f"{json.dumps(semantic_catalog, ensure_ascii=False, default=str, separators=(',', ':'))}\n\n"
            "Persisted workflow state:\n"
            f"{json.dumps(state_payload, ensure_ascii=False, default=str, separators=(',', ':'))}\n\n"
            "Recent conversation for reference resolution only:\n"
            f"{_recent_conversation_excerpt(history)}\n\n"
            "Latest-turn authority reminder: before inheriting any service, doctor, date, or time from "
            "persisted workflow state, first ground every such entity explicitly stated in the latest "
            "customer turn. Persisted values may fill only fields the latest turn leaves unchanged. In a "
            "reschedule flow, appointment_id is the canonical existing appointment when one supplied appointment "
            "is clearly identified; appointment_reference is its customer-facing description, while requested_date "
            "and requested_start_time are the replacement target.\n\n"
            "Latest-turn consistency reminder: if presented booking/reschedule options exist and "
            "the latest customer turn explicitly chooses one option or exact clock time and authorizes "
            "the action, capture that choice in selection_index/selection_time or requested_start_time "
            "and include the matching write capability. Never infer a choice the customer did not state.\n\n"
            "Latest customer turn (authoritative):\n"
            f"{_latest_customer_turn(history)}"
        )
    )

    primary_name = settings.gemini_realtime_interpreter_model
    fallback_name = settings.gemini_realtime_interpreter_fallback_model
    emergency_name = settings.gemini_realtime_interpreter_emergency_model
    primary_model = build_realtime_interpreter_model()

    def invoke_semantic_structured(model) -> UnifiedTurnDecision:
        # Provider-side JSON Schema is still the contract. A single bounded retry
        # handles occasional model output that passes provider shaping but fails
        # Linka's stricter local Pydantic validation. There is no text parsing or
        # lexical intent fallback here.
        try:
            return invoke_typed_structured_output(
                model=model,
                schema=UnifiedTurnDecision,
                messages=[system, user],
            )
        except StructuredOutputError:
            return invoke_typed_structured_output(
                model=model,
                schema=UnifiedTurnDecision,
                messages=[system, user],
            )

    def invoke_primary() -> UnifiedTurnDecision:
        return invoke_semantic_structured(primary_model)

    def invoke_fallback() -> UnifiedTurnDecision:
        fallback_model = build_realtime_interpreter_fallback_model()
        if fallback_model is None:
            raise RuntimeError("Unified turn interpreter fallback model is not configured.")
        return invoke_semantic_structured(fallback_model)

    def invoke_emergency() -> UnifiedTurnDecision:
        emergency_model = build_realtime_interpreter_emergency_model()
        if emergency_model is None:
            raise RuntimeError("Unified turn interpreter emergency model is not configured.")
        return invoke_semantic_structured(emergency_model)

    model_calls = [(primary_name, invoke_primary)]
    if fallback_name and fallback_name != primary_name:
        model_calls.append((fallback_name, invoke_fallback))
    if emergency_name and emergency_name not in {primary_name, fallback_name}:
        model_calls.append((emergency_name, invoke_emergency))

    invocation = invoke_with_model_chain(
        model_calls=model_calls,
        operation="unified-turn-interpreter",
        circuit_breaker_cooldown_seconds=settings.llm_realtime_circuit_breaker_cooldown_seconds,
    )
    value = _normalize_single_location_decision(invocation.value)
    grounded_hints = validate_grounded_entity_ids(value.entity_hints, clinic_catalog)
    value = value.model_copy(update={"entity_hints": grounded_hints})
    return _normalize_active_booking_decision(value, flow)
