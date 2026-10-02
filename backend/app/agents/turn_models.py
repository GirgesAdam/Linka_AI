from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.laser_booking_context import set_laser_device_key

SemanticDomain = Literal[
    "services", "clinic", "booking", "appointments", "patient", "support", "communications", "general"
]
SemanticCapability = Literal[
    "service_information", "clinic_information", "pricing", "branch_discovery", "doctor_discovery",
    "availability_discovery", "appointment_creation", "appointment_list", "appointment_confirmation",
    "appointment_cancellation", "appointment_reschedule", "customer_profile", "customer_history",
    "package_information", "package_purchase", "package_refund_quote", "follow_up_request",
    "marketing_preferences", "human_support",
]
RiskFlag = Literal["medical", "complaint", "payment", "urgent"]
HandoffCategory = Literal["medical", "complaint", "payment", "customer_request", "booking_exception", "agent_uncertain", "other"]
Priority = Literal["low", "normal", "high", "urgent"]
FlowSignal = Literal["none", "start_booking", "start_reschedule", "interrupt"]
PackageIntent = Literal["none", "inquire", "purchase", "use_existing", "avoid_existing"]
FlowTurnAction = Literal["continue", "modify", "select_option", "cancel_flow", "interrupt"]
LaserDeviceKey = str
PackageSessionsCount = Literal[3, 6, 9]
CompoundItemKind = Literal["appointment", "package_purchase"]
CompoundAppointmentPackageIntent = Literal["none", "use_existing", "avoid_existing"]
ClearableFlowEntity = Literal[
    "service_query", "service_id", "service_candidate_ids", "branch_query", "branch_id",
    "branch_candidate_ids", "doctor_query", "doctor_id", "doctor_candidate_ids", "laser_device_key",
    "requested_date", "requested_start_time", "not_before_time", "not_after_time",
    "appointment_reference", "appointment_id",
]


def _require_all_schema_fields(schema: dict) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["required"] = list(properties)


class CompoundRequestedItem(BaseModel):
    """One independently executable operation inside a multi-item customer request."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_all_schema_fields)

    kind: CompoundItemKind
    service_query: str | None = Field(
        default=None,
        description="Service wording for this specific compound item; this is not a substitute for canonical grounding.",
    )
    service_id: str | None = Field(
        default=None,
        description=(
            "Canonical service UUID from the supplied clinic catalog for this item. When this item clearly refers "
            "to exactly one catalog service, copy that service's exact UUID here. Use null only when no catalog "
            "service is identified uniquely; use service_candidate_ids for genuine ambiguity."
        ),
    )
    service_candidate_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Canonical service UUIDs from the supplied clinic catalog only when this item's service is genuinely "
            "ambiguous. Leave empty when service_id is selected."
        ),
    )
    doctor_query: str | None = Field(
        default=None,
        description="Doctor wording for this specific compound item; this is not a substitute for canonical grounding.",
    )
    doctor_id: str | None = Field(
        default=None,
        description=(
            "Canonical doctor UUID from the supplied clinic catalog for this item. When exactly one catalog doctor "
            "is clearly intended, copy that doctor's exact UUID here."
        ),
    )
    doctor_candidate_ids: list[str] = Field(
        default_factory=list,
        description="Canonical doctor UUIDs from the supplied catalog only for genuine ambiguity in this item.",
    )
    laser_device_key: LaserDeviceKey | None = None
    package_sessions_count: PackageSessionsCount | None = None
    requested_date: str | None = None
    requested_start_time: str | None = None
    not_before_time: str | None = None
    not_after_time: str | None = None
    package_intent: CompoundAppointmentPackageIntent = "none"
    missing_information: list[str] = Field(default_factory=list)


class SemanticEntityHints(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_all_schema_fields)

    service_query: str | None = Field(default=None, description="Service wording intended by the latest customer turn.")
    branch_query: str | None
    doctor_query: str | None
    service_id: str | None = Field(default=None, description="Canonical service UUID from the supplied clinic catalog.")
    service_candidate_ids: list[str] = Field(default_factory=list)
    branch_id: str | None = Field(default=None, description="Canonical branch UUID from the supplied clinic catalog.")
    branch_candidate_ids: list[str] = Field(default_factory=list)
    doctor_id: str | None = Field(default=None, description="Canonical doctor UUID from the supplied clinic catalog.")
    doctor_candidate_ids: list[str] = Field(default_factory=list)
    laser_device_key: LaserDeviceKey | None = Field(
        default=None,
        description=(
            "For a service whose catalog row requires a laser device, select only one of the supplied "
            "configured device keys when the customer clearly chose it. Never guess a device."
        ),
    )
    package_sessions_count: PackageSessionsCount | None = Field(
        default=None,
        description=(
            "For a package inquiry or purchase, capture the requested configured package size only when "
            "the customer clearly chose 3, 6, or 9 sessions. Never infer a package size from an appointment count."
        ),
    )
    requested_items: list[CompoundRequestedItem] = Field(
        default_factory=list,
        max_length=6,
        description=(
            "Use only when the latest customer turn clearly requests two or more independently executable "
            "appointments/package purchases. Preserve the customer's requested operations in order. For each item, "
            "ground an unambiguous service/doctor to the exact canonical catalog UUID just as for top-level hints. "
            "Leave empty for a normal single-operation turn or for alternatives/ambiguity."
        ),
    )
    appointment_id: str | None = Field(
        default=None,
        description=(
            "Canonical ID of the EXISTING appointment being acted on, selected only from supplied current-patient appointments. "
            "For cancellation or reschedule, when the latest turn plus recent conversation clearly singles out exactly one current "
            "appointment, copy that appointment exact ID here. A direct answer to the assistant immediately preceding question about "
            "which appointment to act on is a selection when it uniquely identifies one appointment. An appointment explicitly said "
            "to stay unchanged is excluded rather than kept as an ambiguity candidate. Use null only when more than one current "
            "appointment genuinely still fits."
        ),
    )
    requested_date: str | None = Field(default=None, description="Desired appointment date YYYY-MM-DD from the latest customer turn.")
    requested_start_time: str | None = Field(
        default=None,
        description=(
            "Exact local appointment start HH:MM that the customer intends as the appointment itself. "
            "Do not use this for the beginning of an availability search or for a reply to a question "
            "asking when the search window should start."
        ),
    )
    not_before_time: str | None = Field(
        description=(
            "Lower bound HH:MM for availability discovery. Use this when the customer wants options at or "
            "after a time, including when answering the assistant's question about when the availability "
            "search should begin. This is not an exact appointment selection."
        )
    )
    not_after_time: str | None = Field(
        description=(
            "Upper bound HH:MM for availability discovery. Use this when the customer wants options at or "
            "before a time or answers when the availability search should end. This is not an exact "
            "appointment selection."
        )
    )
    appointment_reference: str | None = Field(default=None, description="Customer-facing reference identifying an existing appointment.")

    @model_validator(mode="after")
    def bind_selected_laser_device(self) -> SemanticEntityHints:
        # Keep direct model validation safe as well, but SemanticCapabilityDecision
        # is the authoritative per-turn boundary because provider adapters may
        # return already-constructed nested models.
        set_laser_device_key(self.laser_device_key)
        return self


class SemanticCapabilityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_all_schema_fields)
    domains: list[SemanticDomain]
    capabilities: list[SemanticCapability]
    risk_flags: list[RiskFlag]
    flow_signal: FlowSignal
    package_intent: PackageIntent = "none"
    entity_hints: SemanticEntityHints
    missing_information: list[str]
    recommended_handoff_category: HandoffCategory
    recommended_handoff_priority: Priority
    confidence: float
    reason: str

    @model_validator(mode="after")
    def bind_turn_laser_device_context(self) -> SemanticCapabilityDecision:
        # as_semantic_decision() constructs this object on every Agent turn.
        # Resetting here guarantees that a previous booking/reschedule slot cannot
        # leak its device through ContextVar into a later non-laser tool call.
        set_laser_device_key(self.entity_hints.laser_device_key)
        return self


class FlowTurnDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_all_schema_fields)
    action: FlowTurnAction
    capabilities: list[SemanticCapability]
    risk_flags: list[RiskFlag]
    package_intent: PackageIntent = "none"
    entity_hints: SemanticEntityHints
    clear_entity_fields: list[ClearableFlowEntity] = Field(default_factory=list)
    selection_index: int | None
    selection_time: str | None
    missing_information: list[str]
    recommended_handoff_category: HandoffCategory
    recommended_handoff_priority: Priority
    confidence: float
    reason: str


def empty_entity_hints() -> SemanticEntityHints:
    return SemanticEntityHints(
        service_query=None,
        branch_query=None,
        doctor_query=None,
        laser_device_key=None,
        package_sessions_count=None,
        requested_items=[],
        requested_date=None,
        requested_start_time=None,
        not_before_time=None,
        not_after_time=None,
        appointment_reference=None,
    )
