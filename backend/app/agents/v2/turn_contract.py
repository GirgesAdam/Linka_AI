from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OperationType = Literal[
    "service_info",
    "pricing",
    "doctor_info",
    "clinic_info",
    "payment_info",
    "availability",
    "book",
    "appointment_list",
    "confirm_appointment",
    "cancel_appointment",
    "reschedule",
    "customer_profile",
    "customer_history",
    "package_info",
    "package_compare",
    "buy_package",
    "pulse_info",
    "buy_pulse_pack",
    "refund_quote",
    "follow_up",
    "marketing_update",
    "human_support",
    "continue_active",
    "select_active",
    "cancel_active",
    "social",
    "unclear",
]
SafetySignal = Literal[
    "medical",
    "urgent_medical",
    "complaint",
    "payment_dispute",
    "privacy_issue",
]
PackageUsage = Literal["unspecified", "use_existing", "avoid_existing"]
PackageDetail = Literal["owned", "offers"]
PatientDetail = Literal["name", "phone", "preferred_language"]
ServiceDetail = Literal["price", "duration", "description", "devices"]
ClinicDetail = Literal["name", "address", "contact", "working_hours", "general_info", "open_now"]
PulseDetail = Literal["balance", "owned_packs", "offers", "overage_price", "financial_ledger"]
DateMode = Literal["exact", "range", "from_date", "next_available"]
TimeMode = Literal["exact", "after", "before", "range", "nearest"]
TimeAmbiguity = Literal["none", "twelve_hour"]
SelectionKind = Literal["index", "time", "ref", "relative"]
RelativeSelection = Literal["next", "previous", "first", "last"]
EntityCandidateMode = Literal["ambiguous", "set"]
ExecutionIntent = Literal["informational", "execute"]
FinancialOwnership = Literal["none", "reception"]
ContinuationCondition = Literal["always", "if_previous_no_availability"]
VerifiedReadClearField = Literal["date", "time"]
AppointmentFactChallenge = Literal["none", "time"]
SameTurnServiceSource = Literal["none", "verified_appointment"]
GroupedBookingAction = Literal["preserve_group", "remove_other_components"]
ActiveTaskRelationship = Literal["unspecified", "continue", "replace"]
AutomationContextRelationship = Literal["none", "acknowledge", "appointment_action", "next_session"]
AppointmentActionExplicitField = Literal["date", "time"]
ResponseDisposition = Literal["reply", "no_reply"]
FreshTaskField = Literal["service", "doctor", "device", "appointment", "package", "date", "time", "package_usage"]


def _require_all_schema_fields(schema: dict) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["required"] = list(properties)


class StrictContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=_require_all_schema_fields,
    )


class EntityReference(StrictContractModel):
    """Semantic catalog reference selected by the model, never a database identifier."""

    text: str | None = None
    ref: str | None = None
    candidate_refs: list[str] = Field(default_factory=list)
    candidate_mode: EntityCandidateMode = "ambiguous"


class DateConstraint(StrictContractModel):
    mode: DateMode = Field(
        description=(
            "Use next_available when the customer asks for the nearest/soonest upcoming availability "
            "without fixing a calendar date. A conditional fallback to another date applies only "
            "when its condition is true; if recent_verified_read says availability_found=true, do "
            "not activate a fallback that was conditioned on there being no availability."
        )
    )
    start_date: str | None = None
    end_date: str | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> DateConstraint:
        if self.start_date is not None:
            date.fromisoformat(self.start_date)
        if self.end_date is not None:
            date.fromisoformat(self.end_date)

        if self.mode in {"exact", "from_date"} and self.start_date is None:
            raise ValueError(f"{self.mode} date constraint requires start_date.")
        if self.mode == "range":
            if self.start_date is None or self.end_date is None:
                raise ValueError("range date constraint requires start_date and end_date.")
            if date.fromisoformat(self.end_date) < date.fromisoformat(self.start_date):
                raise ValueError("date range end_date cannot be before start_date.")
        if self.mode == "next_available" and (
            self.start_date is not None or self.end_date is not None
        ):
            raise ValueError("next_available date constraint must not invent a date.")
        return self


class TimeConstraint(StrictContractModel):
    mode: TimeMode
    start_time: str | None = None
    end_time: str | None = None
    start_time_ambiguity: TimeAmbiguity = "none"
    end_time_ambiguity: TimeAmbiguity = "none"

    @model_validator(mode="after")
    def validate_times(self) -> TimeConstraint:
        parsed_start = time.fromisoformat(self.start_time) if self.start_time else None
        parsed_end = time.fromisoformat(self.end_time) if self.end_time else None

        if self.mode in {"exact", "after", "before"} and parsed_start is None:
            raise ValueError(f"{self.mode} time constraint requires start_time.")
        if self.mode == "range":
            if parsed_start is None or parsed_end is None:
                raise ValueError("range time constraint requires start_time and end_time.")
            if parsed_end < parsed_start:
                raise ValueError("time range end_time cannot be before start_time.")
        # nearest may omit an anchor; Python can inherit the verified previous exact
        # time on a continuation, or otherwise choose the earliest available slot.
        if parsed_start is None and self.start_time_ambiguity != "none":
            raise ValueError("start_time_ambiguity requires start_time.")
        if parsed_end is None and self.end_time_ambiguity != "none":
            raise ValueError("end_time_ambiguity requires end_time.")
        return self


class Selection(StrictContractModel):
    kind: SelectionKind
    index: int | None = None
    time: str | None = None
    ref: str | None = None
    relative: RelativeSelection | None = None
    time_ambiguity: TimeAmbiguity = "none"

    @model_validator(mode="before")
    @classmethod
    def normalize_presented_option_ref(cls, value: object) -> object:
        """Canonicalize redundant model coordinates only for server-owned availability refs.

        ``opt_*`` is an opaque technical namespace whose canonical meaning is validated
        later against the server-owned presented availability snapshot. The model may
        redundantly echo a displayed index/clock alongside that ref because provider
        schemas require every selection field. Those echoes are not authority. Keep the
        ref and discard only those redundant coordinates; all other refs keep the strict
        fail-closed validator below.
        """
        if not isinstance(value, dict):
            return value
        ref = value.get("ref")
        if value.get("kind") != "ref" or not isinstance(ref, str) or not ref.startswith("opt_"):
            return value
        return {
            **value,
            "index": None,
            "time": None,
            "relative": None,
            "time_ambiguity": "none",
        }

    @model_validator(mode="after")
    def validate_selection(self) -> Selection:
        if self.kind == "index":
            if self.index is None or self.index < 1:
                raise ValueError("index selection requires a positive index.")
            if self.time is not None or self.ref is not None or self.relative is not None:
                raise ValueError("index selection cannot also contain time/ref/relative.")
            if self.time_ambiguity != "none":
                raise ValueError("index selection cannot contain time ambiguity.")
        elif self.kind == "time":
            if self.time is None:
                raise ValueError("time selection requires time.")
            time.fromisoformat(self.time)
            if self.index is not None or self.ref is not None or self.relative is not None:
                raise ValueError("time selection cannot also contain index/ref/relative.")
        elif self.kind == "ref":
            if not self.ref:
                raise ValueError("ref selection requires ref.")
            if self.index is not None or self.time is not None or self.relative is not None:
                raise ValueError("ref selection cannot also contain index/time/relative.")
            if self.time_ambiguity != "none":
                raise ValueError("ref selection cannot contain time ambiguity.")
        elif self.kind == "relative":
            if self.relative is None:
                raise ValueError("relative selection requires a relative direction.")
            if self.index is not None or self.time is not None or self.ref is not None:
                raise ValueError("relative selection cannot also contain index/time/ref.")
            if self.time_ambiguity != "none":
                raise ValueError("relative selection cannot contain time ambiguity.")
        return self


class AppointmentSelector(StrictContractModel):
    """Existing-appointment identity constraints, separate from replacement constraints."""

    appointment: EntityReference | None = None
    service: EntityReference | None = None
    doctor: EntityReference | None = None
    device: EntityReference | None = None
    date: DateConstraint | None = None
    time: TimeConstraint | None = None


class TurnEntities(StrictContractModel):
    service: EntityReference | None = None
    doctor: EntityReference | None = None
    device: EntityReference | None = None
    appointment: EntityReference | None = None
    package: EntityReference | None = None
    date: DateConstraint | None = None
    time: TimeConstraint | None = None
    package_sessions: int | None = Field(
        default=None,
        description=(
            "Exact session count explicitly associated with a multi-session package/course/bundle. "
            "Preserve this count on pricing questions about that package so deterministic Python can "
            "resolve the matching package offer instead of falling back to the single-session service "
            "price. Do not calculate or invent a session count."
        ),
    )
    pulse_count: int | None = Field(
        default=None,
        description=(
            "Exact Pulse count explicitly attached to a prepaid Pulse pack/offer or to a counted "
            "overage-information question. Preserve the stated count; never calculate or invent one."
        ),
    )
    marketing_consent: bool | None = None
    follow_up_at_local: str | None = None

    @model_validator(mode="after")
    def validate_follow_up_datetime(self) -> TurnEntities:
        if self.package_sessions is not None and self.package_sessions < 1:
            raise ValueError("package_sessions must be positive.")
        if self.pulse_count is not None and self.pulse_count < 1:
            raise ValueError("pulse_count must be positive.")
        if self.follow_up_at_local is not None:
            datetime.fromisoformat(self.follow_up_at_local)
        return self


class TurnOperation(StrictContractModel):
    type: OperationType = Field(
        description=(
            "Choose the customer's semantic action. Use payment_info for read-only general clinic "
            "payment-method/timing/policy questions that do not ask to inspect or change a customer's "
            "financial ledger. Use book whenever the customer is asking Linka "
            "to create/reserve a new appointment now, even when required booking details are still "
            "missing and Python will need to clarify them. Do not downgrade an incomplete booking "
            "request to availability. Use availability only when the customer is asking to inspect "
            "possible appointment options without requesting creation of a new appointment. Buying "
            "a package and creating an appointment are separate actions: a request to buy a package "
            "and book its first session requires a buy_package operation plus a separate book "
            "operation; never encode the appointment only inside buy_package. Use pricing for service "
            "costs and session-package offer pricing. A prepaid Pulse-pack price is pulse_info with "
            "requested_pulse_details=[offers], not generic service pricing. When a multi-session "
            "package price is asked and its session count is explicit, preserve that count in "
            "entities.package_sessions."
        )
    )
    entities: TurnEntities
    source_appointment: AppointmentSelector | None = Field(
        default=None,
        description=(
            "For appointment lifecycle operations only, identify the existing/source appointment. "
            "Keep replacement date/time and requested replacement identities in entities. Never put "
            "replacement date/time here."
        ),
    )
    selection: Selection | None = None
    package_usage: PackageUsage = "unspecified"
    requested_service_details: list[ServiceDetail] = Field(default_factory=list)
    same_turn_service_source: SameTurnServiceSource = Field(
        default="none",
        description=(
            "For pricing only, set verified_appointment when the customer asks for the price of "
            "the primary service belonging to an appointment they also asked Linka to inspect earlier "
            "in this same customer turn. This is a semantic relationship marker only: do not invent "
            "or expose a service ID/ref from appointment wording. Python may bind the pricing read only "
            "after that appointment read verifies one canonical service, and may also inherit its "
            "verified laser device key. Leave none when the customer explicitly names/selects a service, "
            "when the price request is independent, or when there is no same-turn appointment relation."
        ),
    )
    requested_clinic_details: list[ClinicDetail] = Field(
        default_factory=list,
        description=(
            "For clinic_info only, include exactly the customer-safe clinic facts requested: name, "
            "address, contact, working_hours, general_info, or open_now. Use open_now only when the "
            "customer asks whether the clinic is currently open; it does not prove appointment "
            "availability. Leave empty only for a broad clinic-information request."
        ),
    )
    requested_patient_details: list[PatientDetail] = Field(
        default_factory=list,
        description=(
            "For customer_profile only, include exactly the customer-safe profile facts requested: "
            "name, phone, preferred_language, or multiple values when the customer asks for multiple "
            "profile fields. Leave empty only for a broad request for the customer's profile/details."
        ),
    )
    requested_package_details: list[PackageDetail] = Field(
        default_factory=list,
        description=(
            "For package_info only, include exactly the session-package facts requested: owned for "
            "the customer's existing packages/current remaining sessions, offers for packages currently "
            "available to purchase, or both when both concerns are asked. Do not infer ownership from "
            "an offer-discovery question."
        ),
    )
    requested_pulse_details: list[PulseDetail] = Field(
        default_factory=list,
        description=(
            "For pulse_info only, include exactly the Pulse facts requested: balance for the aggregate "
            "remaining balance by device; owned_packs when the customer asks about a particular owned "
            "pack's purchased/used/remaining Pulses, status, or expiry; offers; overage_price; and/or "
            "financial_ledger. A particular owned-pack remaining question is owned_packs, not balance. "
            "financial_ledger is a semantic ownership marker for receptionist-owned money/payment facts "
            "about an owned Pulse pack; it never authorizes a financial read. Do not add unrelated Pulse data."
        ),
    )
    financial_ownership: FinancialOwnership = Field(
        default="none",
        description=(
            "Set reception when this operation asks about money already paid, balance still due, "
            "payment/transaction status, checkout, settlement, or a financial-record mutation for the "
            "customer's own account or appointment. General payment-method/timing/policy questions use "
            "payment_info with financial_ownership=none. This is a semantic ownership marker only and "
            "never authorizes a financial read or write. Service/package offer prices remain none."
        ),
    )
    # Required in provider schemas. The default preserves compatibility for direct
    # internal/test construction; production structured output always supplies it.
    execution_intent: ExecutionIntent = "execute"
    active_task_relationship: ActiveTaskRelationship = Field(
        default="unspecified",
        description=(
            "Relationship of this primary task operation to the explicitly supplied active_task. "
            "Use continue only for book/reschedule when the latest customer message is continuing "
            "or correcting that same unfinished task. Use replace only when the latest customer "
            "message itself explicitly starts a separate/unrelated book or reschedule goal and "
            "abandons the unfinished task, including an additional/new booking. Never carry replace "
            "forward from an earlier message: a later date/time/doctor/device/service answer for the "
            "newly active task is continue. Leave unspecified when there is no active_task and for "
            "side reads/social turns. Deterministic Python owns lifecycle transitions and ignores "
            "this marker on non-task operations."
        ),
    )
    automation_context_relationship: AutomationContextRelationship = Field(
        default="none",
        description=(
            "Relationship of the latest customer message to server-owned automation_context. "
            "Use acknowledge for a simple acknowledgement/reply to the automation message with no "
            "requested lifecycle action. Use appointment_action only when the customer asks to "
            "change/cancel/confirm the appointment referenced by automation_context. Use next_session "
            "only when the customer asks for a new next session that clearly refers to the treatment "
            "from a post-visit automation. Leave none when automation_context is absent or unrelated. "
            "This marker never supplies canonical IDs by itself; Python binds only server-verified "
            "automation metadata."
        ),
    )
    appointment_action_explicit_fields: list[AppointmentActionExplicitField] = Field(
        default_factory=list,
        description=(
            "For automation_context_relationship=appointment_action with reschedule only, list date "
            "and/or time exactly when that replacement dimension is explicitly supplied in the latest "
            "customer message. Never mark a dimension copied or inferred from automation_context, "
            "template prose, assistant prose, or older dialogue. Python uses this list to distinguish "
            "a true time-only edit from an unspecified reschedule request."
        ),
    )
    fresh_task: bool = Field(
        default=False,
        description=(
            "True only when the latest customer message itself explicitly starts a new/separate "
            "book or reschedule task that must not inherit task-local constraints from an older "
            "completed action, abandoned task, or conversation history. This is message-local: "
            "later answers inside the newly active task use false."
        ),
    )
    fresh_task_explicit_fields: list[FreshTaskField] = Field(
        default_factory=list,
        description=(
            "For fresh_task=true, list only task fields explicitly supplied in the latest customer "
            "message itself. Never include values recovered only from active_task, recent_verified_action, "
            "assistant prose, or older dialogue. Deterministic Python uses this list as the authority "
            "boundary for fresh task state."
        ),
    )
    grouped_booking_action: GroupedBookingAction = Field(
        default="preserve_group",
        description=(
            "For an active grouped booking only: keep preserve_group unless the customer explicitly "
            "asks to drop the other pending service components and continue with only this operation's "
            "service. Use remove_other_components only for that explicit narrowing; never infer it "
            "merely because the customer mentions or edits one component."
        ),
    )
    cleared_verified_read_fields: list[VerifiedReadClearField] = Field(
        default_factory=list,
        description=(
            "For appointment_list continuations only, list a prior verified temporal scope "
            "dimension (date and/or time) that the customer explicitly removes while keeping "
            "the same read context. A request that makes a previously constrained dimension "
            "unrestricted (for example, any time after a prior before/after/exact-time filter) "
            "is an explicit clear, not an omission. Use this only with continues_previous=true "
            "and only when that dimension has no replacement constraint in entities. Leave empty "
            "when an omitted verified dimension is unchanged, and leave empty for unrelated/new reads."
        ),
    )
    appointment_fact_challenge: AppointmentFactChallenge = Field(
        default="none",
        description=(
            "For appointment_list only, set time when the customer is questioning or correcting "
            "the time of the same appointment identified by recent_verified_read.appointment_ref. "
            "Put the customer's claimed time in entities.time as an exact constraint and set "
            "continues_previous=true. This marker is semantic only: Python re-verifies the prior "
            "appointment identity and never treats the claimed time as read truth or write authority. "
            "Leave none for new filtered appointment lookups and all lifecycle changes."
        ),
    )
    # True only when this operation semantically continues supplied verified
    # one-turn read/action context. Python, not the model, owns the actual merge.
    continues_previous: bool = Field(
        default=False,
        description=(
            "True only when this operation continues recent_verified_read or clearly refers to "
            "recent_verified_action. For availability, requesting additional results from the "
            "immediately previous verified availability result is continuation even if the customer "
            "switches language or omits the unchanged search constraints. A fresh/contextless request "
            "for additional results is not continuation. Respect verified read summaries and use "
            "verified action references instead of reconstructing completed-action identity from prose. "
            "Python owns inheritance, canonical scope comparison, pagination cursor safety, and grounding "
            "of omitted verified scope fields."
        ),
    )
    continuation_condition: ContinuationCondition = Field(
        default="always",
        description=(
            "Use if_previous_no_availability only when this operation is a conditional fallback "
            "that should activate only if recent_verified_read found no availability. Use always "
            "for ordinary continuations and unconditional nearest/next-available requests. Python "
            "evaluates this condition against the verified previous result."
        ),
    )

    @model_validator(mode="after")
    def validate_automation_context_relationship(self) -> TurnOperation:
        explicit = set(self.appointment_action_explicit_fields)
        if self.automation_context_relationship == "none":
            if explicit:
                raise ValueError(
                    "appointment_action_explicit_fields requires appointment_action automation context."
                )
            return self
        if self.automation_context_relationship == "appointment_action" and self.type not in {
            "reschedule",
            "cancel_appointment",
            "confirm_appointment",
        }:
            raise ValueError(
                "appointment_action automation context is only valid for appointment lifecycle operations."
            )
        if self.automation_context_relationship == "next_session" and self.type != "book":
            raise ValueError("next_session automation context is only valid for book.")
        if self.automation_context_relationship == "acknowledge" and self.execution_intent != "informational":
            raise ValueError("automation acknowledgement must remain informational/read-only.")
        if explicit:
            if (
                self.automation_context_relationship != "appointment_action"
                or self.type != "reschedule"
            ):
                raise ValueError(
                    "appointment_action_explicit_fields is only valid for reminder-linked reschedule."
                )
            if ("date" in explicit) != (self.entities.date is not None):
                raise ValueError(
                    "appointment_action explicit date marker must match an explicit date entity."
                )
            if ("time" in explicit) != (self.entities.time is not None):
                raise ValueError(
                    "appointment_action explicit time marker must match an explicit time entity."
                )
        elif (
            self.automation_context_relationship == "appointment_action"
            and self.type == "reschedule"
            and (self.entities.date is not None or self.entities.time is not None)
        ):
            raise ValueError(
                "Reminder-linked replacement date/time entities require explicit-field markers."
            )
        return self

    @model_validator(mode="after")
    def validate_same_turn_service_source(self) -> TurnOperation:
        if self.same_turn_service_source == "none":
            return self
        if self.type != "pricing":
            raise ValueError("same_turn_service_source is only valid for pricing.")
        if self.execution_intent != "informational":
            raise ValueError("same-turn appointment pricing must remain informational/read-only.")
        service = self.entities.service
        if service is not None and (service.ref is not None or bool(service.candidate_refs)):
            raise ValueError(
                "same-turn appointment pricing cannot override an explicit grounded service."
            )
        return self

    @model_validator(mode="after")
    def validate_appointment_fact_challenge(self) -> TurnOperation:
        if self.appointment_fact_challenge == "none":
            return self
        if self.type != "appointment_list":
            raise ValueError("appointment_fact_challenge is only valid for appointment_list.")
        if not self.continues_previous:
            raise ValueError("appointment_fact_challenge requires continues_previous=true.")
        if self.execution_intent != "informational":
            raise ValueError("appointment_fact_challenge must remain informational/read-only.")
        if (
            self.appointment_fact_challenge == "time"
            and (
                self.entities.time is None
                or self.entities.time.mode != "exact"
                or self.entities.time.start_time is None
            )
        ):
            raise ValueError("time challenge requires an exact claimed time.")
        return self


class TiaTurnUnderstanding(StrictContractModel):
    """The only semantic result required from the V2 language-understanding model."""

    operations: list[TurnOperation] = Field(
        default_factory=list,
        description=(
            "Preserve every independently requested action as its own operation in customer order. "
            "A package purchase does not include appointment creation. If the customer asks to buy "
            "a package and book its first session, emit both buy_package and book. If the same turn "
            "also requests another appointment, emit that additional book operation too. Do not "
            "collapse purchase-plus-booking or multiple requested appointments into fewer actions."
        ),
    )
    safety_signals: list[SafetySignal] = Field(default_factory=list)
    response_disposition: ResponseDisposition = Field(
        default="reply",
        description=(
            "Use no_reply only when the latest customer message is purely a conversational closing "
            "acknowledgement after the prior task/read is already complete, with no active task, pending "
            "choice, automation acknowledgement, requested action, question, or safety concern. This is "
            "semantic classification only; deterministic Python re-checks state before suppressing output."
        ),
    )

    @model_validator(mode="after")
    def validate_non_empty_turn(self) -> TiaTurnUnderstanding:
        if (
            not self.operations
            and not self.safety_signals
            and self.response_disposition != "no_reply"
        ):
            raise ValueError(
                "A turn must contain at least one operation, safety signal, or no-reply disposition."
            )
        if len(self.operations) > 6:
            raise ValueError("A customer turn cannot produce more than six operations.")
        return self