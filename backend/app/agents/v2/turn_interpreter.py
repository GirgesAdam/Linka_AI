from __future__ import annotations

import json
from datetime import datetime

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.agents.llm_runtime import invoke_with_model_chain
from app.agents.model_provider import (
    build_realtime_interpreter_fallback_model,
    build_realtime_interpreter_model,
)
from app.agents.structured_output import StructuredOutputError, invoke_typed_structured_output
from app.agents.v2.semantic_context import SemanticContext, ground_turn_references
from app.agents.v2.time_resolution import resolve_turn_times_by_clinic_hours
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
)
from app.agents.v2.turn_normalization import (
    dedupe_exact_operations,
    normalize_semantic_invariants,
)
from app.core.config import settings


def _message_text(message: BaseMessage, *, limit: int = 1200) -> str:
    if not isinstance(message.content, str) or not message.content.strip():
        return ""
    text = " ".join(message.content.strip().split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _latest_customer_index(history: list[BaseMessage]) -> int | None:
    for index in range(len(history) - 1, -1, -1):
        if isinstance(history[index], HumanMessage) and _message_text(history[index]):
            return index
    return None


def _native_context_messages(
    history: list[BaseMessage],
    *,
    latest_customer_index: int,
    limit: int = 6,
) -> list[BaseMessage]:
    selected: list[BaseMessage] = []
    for message in history[:latest_customer_index]:
        if not isinstance(message, (HumanMessage, AIMessage)):
            continue
        text = _message_text(message, limit=900)
        if not text:
            continue
        if isinstance(message, HumanMessage):
            selected.append(HumanMessage(content=text))
        else:
            selected.append(AIMessage(content=text))
    return selected[-limit:]


def _interpreter_system_prompt(*, timezone_name: str, local_now: datetime) -> str:
    return f"""You are Tia's V2 semantic turn interpreter for an aesthetic clinic.
Return only the required structured schema. You understand customer meaning; you do not answer
the customer, choose tools, perform writes, calculate money, or invent clinic facts.

SEMANTIC PRINCIPLES
- Interpret meaning from the latest customer turn, the native recent dialogue, the active task,
  pending choice, recent_verified_read, and supplied semantic catalog references. Do not route by
  lexical triggers, memorized keywords, or phrase matching.
- Select only supplied entity references. If one entity is clearly intended, set ref. If multiple
  supplied entities remain genuinely possible because the identity is unclear, leave ref null, use
  candidate_refs, and candidate_mode=ambiguous. If the customer intentionally refers to several
  entities as a group to inspect or compare, use candidate_refs with candidate_mode=set. Never invent
  a reference.
- A direct comparison between two or more supplied doctors, devices, services, packages, or other
  entities is a set comparison, not an identity ambiguity. Keep the comparison informational and use
  candidate_mode=set for the compared entity set. For an availability comparison with no explicit
  date, use date mode=next_available so Python can verify which requested candidate is available
  sooner. Never ask the customer to choose one candidate merely in order to compare them.
- Set continues_previous=true only when the new operation clearly continues recent_verified_read.
  When true, include only constraints the customer newly states or changes; deterministic Python
  inherits omitted verified dimensions. A newly supplied value replaces the previous value in that
  same dimension. Set continuation_condition=if_previous_no_availability only when the operation is
  explicitly conditional on the immediately previous verified availability having no options; use
  continuation_condition=always for ordinary continuations and unconditional nearest requests.
- execution_intent describes whether the customer authorizes an action now. Use execute only when
  the customer is actually asking Tia to perform the action now. Questions, comparisons,
  hypotheticals, "should I" choices, and requests to inspect consequences/options are informational,
  even if they mention booking, cancellation, rescheduling, or purchasing.
- Preserve multi-part requests as multiple operations in customer order when they are independently
  meaningful. Alternatives are not multiple operations. Emit each semantically identical operation
  only once.
- Resolve the whole latest customer turn jointly before emitting operations. Cross-operation
  references such as the same day, time, doctor, device, or visit may be defined by another
  operation in the same turn even when the explicit value appears later in the sentence. Copy the
  resolved semantic constraint onto every operation that refers to it; do not replace an unresolved
  same-turn reference with the current date/time or another guessed value.
- A same-turn relative reference MUST NOT remain unresolved when its explicit value exists anywhere
  else in the latest customer message. Read the complete customer message before finalizing any
  operation. If one requested appointment says it is on the same day/time/doctor/device as another
  requested appointment whose value is explicit later in the turn, copy that explicit value into the
  first operation too. Leaving that date/time/entity null would incorrectly force a clarification.
- Package purchase and appointment creation are separate independently meaningful actions. If the
  customer asks to buy a package and book its first session in the same turn, emit buy_package plus
  a separate book operation for that session; the booking should use package_usage=use_existing when
  the customer intends it to consume the newly purchased package. If the same turn requests another
  appointment too, emit that as another separate book operation rather than collapsing any action.
- When the customer explicitly compares using an owned package with paying for a standalone session
  of the same service, keep the turn informational and emit both package_info and pricing operations
  for that service so both sides of the comparison are grounded. Do not turn the comparison into a
  booking or purchase request.
- When a request asks both about a past appointment outcome and its effect on the customer's
  owned-package balance or current owned-package state, preserve both concerns as separate semantic
  operations: customer_history for the historical event and package_info for current package state.
  Do not infer current package balance from customer_history alone.
- A read request never becomes a write request merely because the requested action could be
  executed.
- A harmless informational/social side turn must not be interpreted as cancelling an active task.
- When a customer corrects or changes a requirement in an active task, represent the new semantic
  value only. Python owns dependency invalidation and persisted-state changes.
- Use native recent dialogue to resolve elliptical follow-ups, but prefer recent_verified_read and
  recent_verified_action when supplied because those scopes were verified by Python. Do not
  reconstruct stale constraints from assistant prose when a verified structured scope exists.
- Ordinal references to an immediately preceding option list are positional: first/second/third (and
  equivalents such as الأولى/التانية/الثالثة) refer to the corresponding displayed item in that
  list, in order. Never reinterpret "the second" as "the other" or the last item. Ground the chosen
  entity back to the verified recent read when that scope is available.
- recent_verified_action describes only the immediately previous completed action when Python exposes
  one. If it is a completed buy_pulse_pack and the customer clearly refers to the Pulses/pack just
  added or purchased, mark the relevant follow-up as continues_previous=true and preserve or use its
  verified device reference instead of asking for that device again. A Pulse pack is not a session
  package: this continuity may inherit the verified device, but must not set package_usage or imply
  session-package consumption unless the customer separately and explicitly refers to a session
  package. Do not inherit the Pulse purchase when the customer starts an unrelated request or names
  a different device.
- Package usage controls whether an appointment consumes an existing entitlement; it does not erase
  the service identity established by that package or by the immediately relevant dialogue. A
  request to avoid using an existing package can still book the same established service as a
  standalone appointment.
- Use select_active only when structured pending/active options are actually supplied inside
  SEMANTIC_CONTEXT. Recent assistant prose alone is not a verified option snapshot. For a pending
  appointment choice, an ordinal/ref reply selects only that verified appointment; do not reconstruct
  cancel/reschedule or treat the selected appointment's current date/time as replacement authority.
  If pending_choice purpose=appointment_target, that single verified appointment is the lifecycle
  target: use its appointment_ref in source_appointment when the customer next explicitly cancels or
  supplies replacement constraints for reschedule. If no structured pending option is supplied but
  the recent dialogue makes the customer's intended primary action and constraints clear, reconstruct
  that primary operation from the dialogue so Python can verify it again instead of emitting select_active.
- Use continue_active for a requirement that continues an explicitly supplied active task without
  independently restating the task's primary operation.
- cancel_active stops an unfinished conversational task. cancel_appointment concerns an already
  existing appointment. Keep these meanings separate.
- availability means asking what appointment possibilities exist without requesting creation of a
  new appointment. book means requesting creation of a new appointment.
- reschedule means changing an existing appointment. confirm_appointment confirms an existing
  appointment. For cancel_appointment, confirm_appointment, and reschedule, identify the existing
  appointment in source_appointment. For reschedule, entities.date/time and entities doctor/device/service
  are replacement constraints only; never copy the requested new date/time into source_appointment.
  If the customer states both the current appointment date/time and the requested new date/time,
  preserve the current values in source_appointment and the new values in entities. When a lifecycle
  request uses a broad category such as "laser" without naming one exact service, do not expand it
  into a service-catalog choice before current appointments are checked; leave the exact service
  unresolved and let Python present verified current appointments.
- For appointment_list, broad category wording such as asking for "my next laser appointment" must
  not force a service choice from the catalog. Unless one specific service is clearly named, leave
  the service entity null so Python can read the customer's actual appointments first.
- package_info and refund_quote are reads. buy_package is a purchase request. For package_info,
  requested_package_details controls the exact read scope: owned means the customer's existing package
  or remaining-session state; offers means session-package options currently available to purchase; use
  both only when both concerns are explicitly requested. Set requested_package_details to exactly what
  was requested. package_usage describes whether an appointment should consume an existing session
  package, avoid an existing package, or leaves that question unspecified.
- payment_info is a read-only general clinic payment-policy surface: how payment works, which payment
  methods the clinic accepts (for example cash/card/instant transfer when actually configured), when
  payment happens, and whether payment is required while making the booking. These are informational
  questions, not financial transactions. Use payment_info with execution_intent=informational and
  financial_ownership=none. During an active booking/reschedule, payment_info is a harmless side read:
  do not cancel, replace, restart, or hand off the active task merely because payment is mentioned.
  Specific methods must come from verified clinic information; never invent an accepted method.
- Pulse balance/payment selection is not part of appointment booking. Reception handles
  appointment billing, Pulse settlement, and cash-vs-Pulse choices. If a customer asks to book and
  mentions using/not using Pulses, keep the booking semantics and do not encode a billing preference.
- financial_ownership=reception is the general typed marker for customer/appointment money-ledger or
  financial-action meaning: amount already paid, balance still due, payment or transaction status,
  checkout/settlement, recording or correcting a payment, changing an applied payment method, refund,
  or other financial-record intervention. Apply it even inside an active booking/reschedule and even
  when a service/device is in context. A question like "how much is left for me to pay?" is not service
  pricing. General payment methods/timing/policy questions are payment_info and must remain
  financial_ownership=none. Service/package offer-price questions also use financial_ownership=none.
  For a mixed request, keep any separately asked Agent-owned read as its own operation and mark only
  the ledger/action concern reception. The marker never authorizes a financial read or write; Python
  converts it to Reception ownership deterministically.
- pulse_info is read-only information about the customer's Pulse balance/owned Pulse packs, active
  Pulse-pack offers, or per-device overage price. Use balance for an aggregate remaining Pulse balance
  by device. Use owned_packs when the customer asks about a particular pack they own, including that
  pack's purchased/used/remaining Pulses, status, or expiry. A cost/price question about a prepaid Pulse pack is
  pulse_info with requested_pulse_details=[offers]; it does not require a service. A question about
  extra/excess/overage Pulses is pulse_info with requested_pulse_details=[overage_price], even when
  the customer gives an exact Pulse count; preserve that count in entities.pulse_count so Python can
  calculate from the verified unit price. If both pack price and overage unit price are requested,
  include both offers and overage_price. Use financial_ledger when the customer asks about money
  already paid, money still due, payment/transaction status, checkout, settlement, or whether an
  owned Pulse pack is financially paid/closed. financial_ledger is only a semantic ownership marker:
  it does not authorize a financial read. If the same customer turn also asks an Agent-owned Pulse
  fact, include both requested details in the same pulse_info operation so deterministic Python can
  preserve the safe Pulse read and hand the financial concern to Reception. Set requested_pulse_details
  to exactly what was requested. Preserve a clearly referenced laser device in entities.device.
  Never estimate how many Pulses a future treatment will consume unless verified clinic data explicitly
  supplies that fact.
- buy_pulse_pack means the customer is asking Tia to purchase a prepaid Pulse pack now. A direct
  imperative request to obtain/add/provision a Pulse pack now is a purchase action even when phrased
  colloquially and without the literal word "buy"; use execution_intent=execute. Questions about
  whether a pack exists, what it costs, or what options are available remain pulse_info and
  informational. Never upgrade a hypothetical, comparison, or "should I" question into a purchase.
  Pulse-pack purchase is separate from booking. A request to purchase a Pulse pack and book a session
  requires separate buy_pulse_pack and book operations. Never claim or infer that money was paid
  merely because the customer authorized the purchase; payment truth is owned by backend/payment
  records. Questions about amounts already paid, balance due, payment transactions, payment status,
  checkout, or settlement for an owned Pulse pack are receptionist-owned; represent them as
  pulse_info with requested_pulse_details=[financial_ledger] (plus any separately requested safe
  Pulse details) so Python can deterministically enforce human ownership. Do not use financial_ledger
  for a payment dispute: that remains a payment_dispute safety signal.
- Distinguish a hypothetical financial question from an instruction to reverse a purchased package.
  If the customer is only asking what the refund amount or financial consequence would be if the
  package were cancelled, without authorizing cancellation now, interpret it as refund_quote. If the
  customer is actually asking Tia to carry out cancellation/refund/termination now, interpret it as
  human_support because the agent must not execute purchased-package cancellation.
- clinic_info covers clinic-wide operational/informational questions such as location, contact details,
  working hours, non-payment policies, and comparisons between clinic devices when the question is not
  about one specific service. requested_clinic_details controls field-level disclosure: name for the
  clinic name; address for the verified customer location/address; contact for the customer-facing phone number;
  working_hours for the saved weekly branch schedule; general_info for saved clinic-authored information
  or non-payment policy text; open_now only when the customer explicitly asks whether the clinic is open
  right now. open_now never proves appointment availability and must not be inferred from service/doctor
  schedules. Use multiple values only when multiple clinic facts are requested; leave it empty only for
  a broad clinic-information request. Use payment_info for general payment-method/timing/booking-payment
  questions so payment information remains typed separately from receptionist-owned financial actions.
  service_info covers one specific service and open-ended discovery/listing of the clinic's services;
  for an open-ended service-list request, leave the service entity null instead of inventing one.
- appointment_list covers current/upcoming appointments only. A possessive request for the customer's
  own scheduled appointment/date/time (for example "my next appointment" or "معادي اللي جاي") is
  appointment_list even after intervening service, doctor, package, or Pulse questions. Do not reinterpret
  that request as availability merely because a recent turn named a service; availability means open/bookable
  slots, while appointment_list means the customer's existing booking. Requests about prior, past, completed,
  old, previous, or cancelled appointment history belong to customer_history instead. For example,
  a customer asking for "my old appointments and any that were cancelled" is customer_history, not
  appointment_list, even when the word appointment is used.
- customer_profile covers the verified current customer's own profile. requested_patient_details controls
  field-level disclosure: name for the stored name, phone for the stored display phone, preferred_language
  for the stored language preference. Use multiple values only when multiple profile fields are requested;
  leave it empty only for a broad request for the customer's profile/details.
- customer_history covers the customer's own prior visits, services, and lifecycle history. Do not use it
  to expose money already paid, balance due, payment/transaction status, payment method, or settlement;
  those meanings require financial_ownership=reception under the financial rule above. A discrepancy or
  contested payment is additionally payment_dispute.
- Medical suitability/symptom questions are medical safety signals; acute/emergency-seeming medical
  situations use urgent_medical. Explicit requests for a person use human_support.
- Resolve clear relative dates against the clinic-local clock. If a date remains semantically
  underspecified, leave it null rather than guessing.
- For clock times, separate semantic meaning from AM/PM resolution. If the customer's wording itself
  fixes the period (for example an explicit morning/evening meaning or an unambiguous 24-hour clock),
  use time_ambiguity=none. If a clock value from 1 through 12 could still semantically mean either
  AM or PM, use time_ambiguity=twelve_hour. Do not use clinic opening hours to hide that ambiguity;
  deterministic Python resolves the two clock candidates against clinic_operating_hours after this
  call. For an ambiguous clock, encode one 12-hour-form candidate such as 08:00 plus
  time_ambiguity=twelve_hour rather than guessing 20:00.
- Apply the same rule independently to start_time_ambiguity/end_time_ambiguity and to a time
  selection's time_ambiguity. Fields unrelated to a time value use none.
- follow_up_at_local is an ISO local datetime only when the customer supplied enough meaning to
  resolve a specific future follow-up time. Otherwise leave it null.
- requested_service_details describes only service facts the customer explicitly asked for in that
  operation: price, duration, description, and/or devices. Here description means clinic-authored
  explanatory guidance from the saved "معلومات Tia" field, not a service-table description. Do not
  add extra details merely because they are available. A pricing operation semantically requests
  price. A generic service-information question requests description unless the customer asks for
  a narrower detail. A request to explain or compare devices for a specific service requests both
  devices and description. Leave requested_service_details empty on unrelated operations.

DATE/TIME REPRESENTATION
- exact date: mode=exact with start_date.
- date range: mode=range with start_date/end_date.
- starting from a date: mode=from_date with start_date.
- nearest available date: mode=next_available with no invented date.
- exact time: mode=exact with start_time.
- after/before constraints: mode=after or mode=before with start_time. After/from a time includes the
  boundary itself; Python treats this lower bound as inclusive.
- time range: mode=range with start_time/end_time.
- nearest time: mode=nearest. Supply start_time only when the customer gives an anchor in this turn;
  on a continuation Python may inherit the previous verified exact-time anchor.

Clinic timezone: {timezone_name}
Clinic local time: {local_now.isoformat()}
"""


def _with_interpreter_prompt_cache_breakpoint(
    messages: list[BaseMessage],
    *,
    timezone_name: str,
    local_now: datetime,
) -> list[BaseMessage]:
    """Mark only the stable interpreter prefix for provider prompt caching."""

    if not messages or not isinstance(messages[0], SystemMessage):
        raise ValueError("Interpreter messages must start with a system message.")
    content = messages[0].content
    if not isinstance(content, str):
        raise ValueError("Interpreter system message must be plain text before cache wrapping.")

    dynamic_tail = (
        f"Clinic timezone: {timezone_name}\n"
        f"Clinic local time: {local_now.isoformat()}\n"
    )
    if not content.endswith(dynamic_tail):
        raise ValueError("Interpreter system prompt has an unexpected dynamic tail.")

    stable_prefix = content[: -len(dynamic_tail)]
    cached_system = SystemMessage(
        content=[
            {
                "type": "text",
                "text": stable_prefix,
                "prompt_cache_breakpoint": {"mode": "explicit"},
            },
            {"type": "text", "text": dynamic_tail},
        ]
    )
    return [cached_system, *messages[1:]]


def _messages_for_prompt_cache(
    model: object,
    messages: list[BaseMessage],
    *,
    timezone_name: str,
    local_now: datetime,
) -> list[BaseMessage]:
    options = getattr(model, "prompt_cache_options", None)
    if not isinstance(options, dict) or options.get("mode") != "explicit":
        return messages
    return _with_interpreter_prompt_cache_breakpoint(
        messages,
        timezone_name=timezone_name,
        local_now=local_now,
    )


def _build_interpreter_messages(
    *,
    history: list[BaseMessage],
    semantic_context: SemanticContext,
    timezone_name: str,
    local_now: datetime,
) -> list[BaseMessage]:
    latest_index = _latest_customer_index(history)
    if latest_index is None:
        raise ValueError("V2 turn interpretation requires a customer message.")

    latest_text = _message_text(history[latest_index])
    system = SystemMessage(
        content=_interpreter_system_prompt(
            timezone_name=timezone_name,
            local_now=local_now,
        )
    )
    context = SystemMessage(
        content=(
            "SEMANTIC_CONTEXT (ephemeral references only; Python resolves them to canonical data):\n"
            + json.dumps(
                semantic_context.model_input,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
        )
    )
    recent = _native_context_messages(
        history,
        latest_customer_index=latest_index,
    )
    return [system, context, *recent, HumanMessage(content=latest_text)]


def _reference_from_verified(
    verified: dict[str, object],
    *,
    single_key: str,
    set_key: str | None = None,
) -> EntityReference | None:
    single = verified.get(single_key)
    if isinstance(single, str) and single:
        return EntityReference(text=None, ref=single, candidate_refs=[], candidate_mode="ambiguous")
    if set_key is not None:
        raw = verified.get(set_key)
        if isinstance(raw, list):
            refs = [str(item) for item in raw if isinstance(item, str) and item]
            if refs:
                return EntityReference(text=None, ref=None, candidate_refs=refs, candidate_mode="set")
    return None


def _conditional_fallback_disproved(
    verified: dict[str, object],
    *,
    continuation_condition: str,
) -> bool:
    if continuation_condition != "if_previous_no_availability":
        return False
    found = verified.get("availability_found")
    option_count = verified.get("availability_option_count")
    return found is True or (
        isinstance(option_count, int) and not isinstance(option_count, bool) and option_count > 0
    )


def merge_verified_read_context(
    turn: TiaTurnUnderstanding,
    semantic_context: SemanticContext,
) -> TiaTurnUnderstanding:
    """Merge explicitly-declared continuations with the previous verified read scope."""
    raw = semantic_context.model_input.get("recent_verified_read")
    if not isinstance(raw, dict) or not raw:
        return turn

    previous_date = None
    previous_time = None
    if isinstance(raw.get("date"), dict):
        try:
            previous_date = DateConstraint.model_validate(raw["date"])
        except ValueError:
            previous_date = None
    if isinstance(raw.get("time"), dict):
        try:
            previous_time = TimeConstraint.model_validate(raw["time"])
        except ValueError:
            previous_time = None

    operations = []
    for operation in turn.operations:
        if not operation.continues_previous:
            operations.append(operation)
            continue

        entities = operation.entities
        updates: dict[str, object] = {}
        inherited = (
            ("service", _reference_from_verified(raw, single_key="service_ref")),
            (
                "doctor",
                _reference_from_verified(
                    raw,
                    single_key="doctor_ref",
                    set_key="doctor_refs",
                ),
            ),
            ("device", _reference_from_verified(raw, single_key="device_ref")),
        )
        for field, value in inherited:
            if getattr(entities, field) is None and value is not None:
                updates[field] = value

        fallback_disproved = _conditional_fallback_disproved(
            raw,
            continuation_condition=operation.continuation_condition,
        )
        if fallback_disproved and previous_date is not None:
            updates["date"] = previous_date
        elif entities.date is None and previous_date is not None:
            updates["date"] = previous_date

        if fallback_disproved and previous_time is not None:
            updates["time"] = previous_time
        elif entities.time is None and previous_time is not None:
            updates["time"] = previous_time
        elif (
            entities.time is not None
            and entities.time.mode == "nearest"
            and entities.time.start_time is None
            and previous_time is not None
            and previous_time.mode == "exact"
            and previous_time.start_time is not None
        ):
            updates["time"] = entities.time.model_copy(
                update={
                    "start_time": previous_time.start_time,
                    "start_time_ambiguity": previous_time.start_time_ambiguity,
                }
            )
        if updates:
            entities = entities.model_copy(update=updates)
        operations.append(operation.model_copy(update={"entities": entities}))
    return turn.model_copy(update={"operations": operations})


def merge_same_turn_pulse_device_context(
    turn: TiaTurnUnderstanding,
    semantic_context: SemanticContext,
) -> TiaTurnUnderstanding:
    """Carry one explicit Pulse-purchase device into a linked laser booking only.

    This is device continuity, never billing continuity. An explicit booking device wins,
    and non-laser bookings are never assigned a laser device.
    """
    purchase_devices = {
        operation.entities.device.ref
        for operation in turn.operations
        if operation.type == "buy_pulse_pack"
        and operation.entities.device is not None
        and operation.entities.device.ref is not None
    }
    if len(purchase_devices) != 1:
        return turn
    purchase_device_ref = next(iter(purchase_devices))
    purchase_device = EntityReference(
        text=None,
        ref=purchase_device_ref,
        candidate_refs=[],
        candidate_mode="ambiguous",
    )

    operations = []
    changed = False
    for operation in turn.operations:
        entities = operation.entities
        service_ref = entities.service.ref if entities.service is not None else None
        service_target = (
            semantic_context.reference_map.get(service_ref)
            if service_ref is not None
            else None
        )
        if (
            operation.type == "book"
            and entities.device is None
            and service_target is not None
            and service_target.kind == "service"
            and service_target.metadata.get("requires_laser_device") is True
        ):
            entities = entities.model_copy(update={"device": purchase_device})
            operation = operation.model_copy(update={"entities": entities})
            changed = True
        operations.append(operation)

    return turn.model_copy(update={"operations": operations}) if changed else turn


def merge_verified_action_context(
    turn: TiaTurnUnderstanding,
    semantic_context: SemanticContext,
) -> TiaTurnUnderstanding:
    """Inherit only facts the model explicitly links to the previous verified action."""
    raw = semantic_context.model_input.get("recent_verified_action")
    if not isinstance(raw, dict) or raw.get("operation_type") != "buy_pulse_pack":
        return turn

    device = _reference_from_verified(raw, single_key="device_ref")
    operations = []
    for operation in turn.operations:
        entities = operation.entities
        if (
            operation.continues_previous
            and operation.type == "book"
            and entities.device is None
            and device is not None
        ):
            entities = entities.model_copy(update={"device": device})
            operation = operation.model_copy(update={"entities": entities})
        operations.append(operation)

    if operations == turn.operations:
        return turn
    return turn.model_copy(update={"operations": operations})


def interpret_customer_turn_v2(
    *,
    history: list[BaseMessage],
    semantic_context: SemanticContext,
    timezone_name: str,
    local_now: datetime,
) -> TiaTurnUnderstanding:
    """One semantic LLM call; no regex/keyword fallback and no production side effects."""

    messages = _build_interpreter_messages(
        history=history,
        semantic_context=semantic_context,
        timezone_name=timezone_name,
        local_now=local_now,
    )
    primary_name = settings.openai_model
    fallback_name = settings.openai_fallback_model
    primary_model = build_realtime_interpreter_model()

    def invoke_structured(
        model,
        request_messages: list[BaseMessage],
    ) -> TiaTurnUnderstanding:
        try:
            return invoke_typed_structured_output(
                model=model,
                schema=TiaTurnUnderstanding,
                messages=request_messages,
            )
        except StructuredOutputError:
            return invoke_typed_structured_output(
                model=model,
                schema=TiaTurnUnderstanding,
                messages=request_messages,
            )

    def invoke_primary() -> TiaTurnUnderstanding:
        return invoke_structured(
            primary_model,
            _messages_for_prompt_cache(
                primary_model,
                messages,
                timezone_name=timezone_name,
                local_now=local_now,
            ),
        )

    def invoke_fallback() -> TiaTurnUnderstanding:
        fallback_model = build_realtime_interpreter_fallback_model()
        if fallback_model is None:
            raise RuntimeError("V2 turn interpreter fallback model is not configured.")
        return invoke_structured(
            fallback_model,
            _messages_for_prompt_cache(
                fallback_model,
                messages,
                timezone_name=timezone_name,
                local_now=local_now,
            ),
        )

    model_calls = [(primary_name, invoke_primary)]
    if fallback_name and fallback_name != primary_name:
        model_calls.append((fallback_name, invoke_fallback))

    invocation = invoke_with_model_chain(
        model_calls=model_calls,
        operation="v2-turn-interpreter",
        circuit_breaker_cooldown_seconds=settings.llm_realtime_circuit_breaker_cooldown_seconds,
    )
    continued = merge_verified_read_context(invocation.value, semantic_context)
    continued = merge_verified_action_context(continued, semantic_context)
    grounded = ground_turn_references(continued, semantic_context)
    grounded = merge_same_turn_pulse_device_context(grounded, semantic_context)
    normalized = normalize_semantic_invariants(grounded)
    resolved = resolve_turn_times_by_clinic_hours(normalized, semantic_context)
    return dedupe_exact_operations(resolved)