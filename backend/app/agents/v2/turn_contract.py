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
ServiceDetail = Literal["price", "duration", "description", "devices"]
PulseDetail = Literal["balance", "owned_packs", "offers", "overage_price", "financial_ledger"]
DateMode = Literal["exact", "range", "from_date", "next_available"]
TimeMode = Literal["exact", "after", "before", "range", "nearest"]
TimeAmbiguity = Literal["none", "twelve_hour"]
SelectionKind = Literal["index", "time", "ref"]
EntityCandidateMode = Literal["ambiguous", "set"]
ExecutionIntent = Literal["informational", "execute"]
FinancialOwnership = Literal["none", "reception"]
ContinuationCondition = Literal["always", "if_previous_no_availability"]
GroupedBookingAction = Literal["preserve_group", "remove_other_components"]


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
    time_ambiguity: TimeAmbiguity = "none"

    @model_validator(mode="after")
    def validate_selection(self) -> Selection:
        if self.kind == "index":
            if self.index is None or self.index < 1:
                raise ValueError("index selection requires a positive index.")
            if self.time is not None or self.ref is not None:
                raise ValueError("index selection cannot also contain time/ref.")
            if self.time_ambiguity != "none":
                raise ValueError("index selection cannot contain time ambiguity.")
        elif self.kind == "time":
            if self.time is None:
                raise ValueError("time selection requires time.")
            time.fromisoformat(self.time)
            if self.index is not None or self.ref is not None:
                raise ValueError("time selection cannot also contain index/ref.")
        elif self.kind == "ref":
            if not self.ref:
                raise ValueError("ref selection requires ref.")
            if self.index is not None or self.time is not None:
                raise ValueError("ref selection cannot also contain index/time.")
            if self.time_ambiguity != "none":
                raise ValueError("ref selection cannot contain time ambiguity.")
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
            "financial ledger. Use book whenever the customer is asking Tia "
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
            "For pulse_info only, include exactly the Pulse facts requested: balance, owned_packs, "
            "offers, overage_price, and/or financial_ledger. financial_ledger is a semantic ownership "
            "marker for receptionist-owned money/payment facts about an owned Pulse pack; it never "
            "authorizes a financial read. Do not add unrelated Pulse data."
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
    grouped_booking_action: GroupedBookingAction = Field(
        default="preserve_group",
        description=(
            "For an active grouped booking only: keep preserve_group unless the customer explicitly "
            "asks to drop the other pending service components and continue with only this operation's "
            "service. Use remove_other_components only for that explicit narrowing; never infer it "
            "merely because the customer mentions or edits one component."
        ),
    )
    # True only when this operation semantically continues the supplied verified
    # one-turn read context. Python, not the model, owns the actual merge.
    continues_previous: bool = Field(
        default=False,
        description=(
            "True only when this operation continues recent_verified_read. Respect its verified "
            "result summary: a conditional fallback must not replace a successful prior read when "
            "availability_found=true. Python owns inheritance of omitted scope fields."
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

    @model_validator(mode="after")
    def validate_non_empty_turn(self) -> TiaTurnUnderstanding:
        if not self.operations and not self.safety_signals:
            raise ValueError("A turn must contain at least one operation or safety signal.")
        if len(self.operations) > 6:
            raise ValueError("A customer turn cannot produce more than six operations.")
        return self