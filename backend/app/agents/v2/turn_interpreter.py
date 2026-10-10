from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.agents.explicit_time import extract_single_explicit_hhmm
from app.agents.llm_runtime import invoke_with_model_chain
from app.agents.model_provider import (
    build_realtime_interpreter_fallback_model,
    build_realtime_interpreter_model,
)
from app.agents.structured_output import StructuredOutputError, invoke_typed_structured_output
from app.agents.v2.semantic_context import SemanticContext, ground_turn_references
from app.agents.v2.time_resolution import resolve_turn_times_by_clinic_hours
from app.agents.v2.turn_contract import (
    AppointmentSelector,
    DateConstraint,
    EntityReference,
    Selection,
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
    return f"""You are Linka's V2 semantic turn interpreter for an aesthetic clinic.
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
  same dimension. For availability, a request for additional results from the immediately previous
  verified availability result is a continuation even when the customer switches language or uses
  an elliptical follow-up with no repeated service/date/doctor/device wording. Emit availability
  with continues_previous=true and do not invent replacement constraints. If the customer changes
  a search constraint, represent that change normally; Python compares the canonical verified scope
  and decides whether the presentation cursor is preserved or reset. Without a recent verified
  availability result, do not infer an availability-page continuation from an otherwise contextless
  request for additional results.
  For an appointment_list continuation that explicitly removes a previous date/time
  restriction while keeping the same read, list only that removed dimension in
  cleared_verified_read_fields so Python does not inherit it. Treat making a constrained dimension
  unrestricted (such as asking for any time after a prior before/after/exact-time filter) as an
  explicit clear of that dimension. Do not use that field for a new or unrelated appointment query,
  and do not mark unchanged omitted constraints as cleared. Sharing the same operation type or having
  a recent verified scope is not enough to establish continuation: a broad/new appointment-list
  request (including asking for the next/upcoming appointment as a fresh question) must use
  continues_previous=false unless it actually refines or refers to the previous filtered read.
- A follow-up that questions/corrects the time of the same appointment just verified is not a new
  exact-time search. Only when recent_verified_read contains appointment_ref and the customer is
  challenging that same appointment fact, emit appointment_list with continues_previous=true,
  appointment_fact_challenge=time, and put the customer's claimed time in entities.time as exact.
  Leave entities.appointment unset; Python binds the verified appointment_ref.
  A challenge must semantically presuppose the same verified appointment and dispute/recall its time.
  An existential/filtering question asks whether any appointment matches a time; that is a new lookup,
  even when it immediately follows an appointment answer. For such lookups use continues_previous=false,
  appointment_fact_challenge=none, and keep the stated time as the normal appointment_list filter.
  Requests to move/change the appointment are lifecycle operations (for example reschedule), never
  fact challenges.
  Set continuation_condition=if_previous_no_availability only when the operation is
  explicitly conditional on the immediately previous verified availability having no options; use
  continuation_condition=always for ordinary continuations and unconditional nearest requests.
- execution_intent describes whether the customer authorizes an action now. Use execute only when
  the customer is actually asking Linka to perform the action now. Questions, comparisons,
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
  of the same service, emit one package_compare operation for that service with
  execution_intent=informational. package_compare is read-only and grounds both the customer's owned
  package state and the single-session service price; do not emit package offers, booking, or purchase
  merely because those actions are mentioned as alternatives.
- When a request asks both about a past appointment outcome and its effect on the customer's
  owned-package balance or current owned-package state, preserve both concerns as separate semantic
  operations: customer_history for the historical event and package_info for current package state.
  Do not infer current package balance from customer_history alone.
- A read request never becomes a write request merely because the requested action could be
  executed.
- A harmless informational/social side turn must not be interpreted as cancelling an active task.
- If the customer explicitly clarifies that the prior package/booking discussion was only a question
  and authorizes no action, with no new factual question, emit an informational social acknowledgement
  rather than replaying package discovery/offers. This no-action clarification is turn-local: a later
  explicit request to use a package, buy, or book is a new execute intent and must proceed normally.
- response_disposition is about whether this turn needs an outbound customer message, not about intent routing. Use no_reply only for a pure closing acknowledgement after the previous task/read is already complete and only when the supplied active_task and pending choice are empty, automation_context is not being acknowledged, there is no requested action/question, and there is no safety signal. A short acknowledgement while a task/question/choice is pending is reply, not no_reply. Never classify based on a phrase list; classify the conversational role in the supplied state.
- automation_context is server-owned system-initiated conversational focus, not a verified read/action
  and never write authority. If it is present, a simple acknowledgement of that reminder/follow-up is
  about the automation message, not an unrelated active booking: emit an informational social operation
  with automation_context_relationship=acknowledge and do not mutate/continue the active task. If the
  customer asks to change/cancel/confirm the appointment referenced by automation_context, emit the
  matching lifecycle operation with automation_context_relationship=appointment_action; Python binds
  the canonical appointment target from server metadata, so do not reconstruct an ID from template prose.
  For reminder-linked reschedule, appointment_action_explicit_fields must list date and/or time only when
  that replacement dimension is explicitly stated in the latest customer message. Never mark the current
  appointment date/time from automation_context as explicit. If the customer gives only a replacement time,
  list only time; if they give an explicit new date plus time, list both; if they merely ask to change the
  appointment without either dimension, leave the list empty.
  If a post-visit automation is followed by a clear request for the next session of that treatment, emit
  book with fresh_task=true and automation_context_relationship=next_session. Python may reuse only
  server-verified stable service/device treatment facts; never inherit the old date/time/doctor/slot,
  option snapshot, or write authorization. For unrelated turns leave automation_context_relationship=none.
- When active_task is empty, raw dialogue history alone is never authority for task-local book/reschedule
  constraints. A new booking/reschedule may use fields explicit in the latest customer message, supplied
  verified contexts, or server-owned automation_context, but must not resurrect service/device/doctor/date/
  time/slot/authorization solely from an expired/finished task described in older customer/assistant prose.
- For book/reschedule only, classify the operation's relationship to the explicitly supplied
  active_task in active_task_relationship. Use continue when the latest customer message is still
  working on the same unfinished task, including correcting its service/date/time/doctor/device or
  supplying a missing constraint. Use replace only when the latest customer message itself explicitly
  starts a separate/unrelated booking or reschedule and abandons the unfinished task, including
  language that clearly asks for another/additional/new appointment. Never carry a replace decision
  forward merely because an earlier customer message started the current task. Once Python exposes
  that new task as active_task, later date/time/doctor/device/service answers for it are continuations.
  If the active task is reschedule and the customer explicitly asks to create a new appointment, that
  book operation is replace; a book cannot continue a reschedule. Likewise an explicit new reschedule
  cannot continue a booking. Do not infer replace merely because one constraint changes. Leave
  unspecified for side reads/social turns and when no active task applies. Python, not this marker,
  performs the lifecycle transition.
- Independently set fresh_task=true when the latest customer message explicitly opens a new/separate
  booking or reschedule instead of continuing any prior task or completed action. Also, whenever
  active_task is empty and a book/reschedule is not explicitly continuing supplied verified context,
  treat it as a fresh task even if older dialogue sounds like an unfinished booking. Python enforces
  this fail-closed boundary too. For fresh_task=true, fresh_task_explicit_fields must contain only
  dimensions actually stated in the latest customer message itself
  (service/doctor/device/appointment/package/date/time/package_usage). Never list a field merely because
  recent_verified_action, active_task, assistant prose, or older history contains it. If the customer
  says only that they want a booking, the explicit-field list is empty even if older dialogue had a
  service/date/time/device/doctor. If they explicitly restate Under Arm, list service but do not list
  old device/date/time/doctor unless the latest message itself states them. Later answers inside the
  new active task use fresh_task=false and active_task_relationship=continue where applicable.
- When a customer corrects or changes a requirement in an active task, represent the new semantic
  value only. Python owns dependency invalidation and persisted-state changes.
- Use native recent dialogue to resolve elliptical follow-ups, but prefer active_task,
  recent_verified_read, and recent_verified_action when supplied because those scopes were verified
  by Python. The supplied active_task is authoritative for task-local constraints. If a date/time/
  doctor/device/slot is absent from the current active_task, do not resurrect that constraint from
  an older abandoned task in native dialogue. Do not reconstruct stale constraints from assistant
  prose when a verified structured scope exists.
- presented_availability is a server-owned view of the latest availability items Linka actually
  displayed and may survive harmless side questions. Its option refs are opaque. Pure availability
  references are normally handled by a smaller semantic resolver before this interpreter. If this full
  interpreter is used because the customer also requests a lifecycle action, preserve that action and,
  when it clearly targets a displayed/last-selected option, set selection kind=ref to the supplied opt_*
  ref and continues_previous=true. Never invent an option ref or reconstruct a slot from assistant prose.
  Python validates the ref, binds canonical slot facts, and revalidates availability before any write.
- recent_verified_action describes only the immediately previous completed action when Python exposes
  one. If it is a completed buy_pulse_pack and the customer clearly refers to the Pulses/pack just
  added or purchased, mark the relevant follow-up as continues_previous=true and preserve or use its
  verified device reference instead of asking for that device again. A Pulse pack is not a session
  package: this continuity may inherit the verified device, but must not set package_usage or imply
  session-package consumption unless the customer separately and explicitly refers to a session
  package. Do not inherit the Pulse purchase when the customer starts an unrelated request or names
  a different device.
- If recent_verified_action is a completed book and the customer clearly revokes or undoes that
  just-completed booking, emit cancel_appointment with execution_intent=execute and
  continues_previous=true. Target that exact recent booking through its verified appointment_ref;
  do not reconstruct the target from assistant prose. If the same customer turn also asks to inspect
  availability, preserve both independently meaningful actions in customer order: cancel_appointment
  first, then availability. Do not treat hesitation, dissatisfaction, a price/info question, or
  generic negative sentiment as cancellation. If it is unclear whether the customer actually wants
  to undo the recent booking, do not emit a destructive cancellation; keep the turn non-destructive
  or clarify instead.
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
  independently restating the task's primary operation. In particular, while a booking/reschedule
  task is active, a customer who proposes/checks a missing or changed booking constraint as part of
  completing that task (for example asking whether a particular day or time works) is continuing the
  active task: emit continue_active with that date/time/doctor/device constraint so Python re-verifies
  availability and updates the task. Use standalone availability only when the customer is genuinely
  asking an exploratory availability question that is not a constraint proposal for the active task
  (for example availability for another unrelated service without changing the current booking).
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
  Pulse-pack offers, or per-device overage price. Use balance only for the aggregate remaining Pulse
  balance by device. Use owned_packs when the customer anchors the question to a particular pack they
  own, including that pack's purchased/used/remaining Pulses, status, or expiry. Do not collapse an
  owned-pack question into balance merely because the customer asks "how many are left": for example,
  "الباقة اللي عندي على Candela فاضل فيها كام Pulse؟" and "how many Pulses are left in my Candela pack?"
  are owned_packs; "رصيدي كام Pulse على Candela؟" and "what is my Candela Pulse balance?" are balance.
  A cost/price question about a prepaid Pulse pack is
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
- buy_pulse_pack means the customer is asking Linka to purchase a prepaid Pulse pack now. A direct
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
  customer is actually asking Linka to carry out cancellation/refund/termination now, interpret it as
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
  explanatory guidance from the saved "معلومات Linka" field, not a service-table description. Do not
  add extra details merely because they are available. A pricing operation semantically requests
  price. A generic service-information question requests description unless the customer asks for
  a narrower detail. A request to explain or compare devices for a specific service requests both
  devices and description. Leave requested_service_details empty on unrelated operations.
- When a pricing request refers relationally to the primary service of an appointment that the customer
  also asked Linka to inspect earlier in the same customer turn, keep the pricing operation separate,
  set same_turn_service_source=verified_appointment, and do not guess a service or device reference from
  the wording. Python will bind that pricing read only after the appointment read verifies one canonical
  service and, for laser appointments, its canonical device key. This marker is only for the same-turn
  appointment relationship; it never refers to assistant prose or an arbitrary historical appointment.
  If the customer explicitly names or selects a service for pricing, ground that service normally and
  leave same_turn_service_source=none even when the same turn also asks about an appointment.

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
    automation_context = semantic_context.model_input.get("automation_context")
    automation_order: list[BaseMessage] = []
    if isinstance(automation_context, dict) and automation_context:
        automation_order.append(
            SystemMessage(
                content=(
                    "CONVERSATION_ORDER: The server-owned automation_context in SEMANTIC_CONTEXT "
                    "was sent after all native dialogue messages shown below and immediately before "
                    "the latest customer turn. Treat that automation as the immediate conversational "
                    "focus when interpreting a terse reply. It is context only, not verified read/write "
                    "authority; use only its typed relationship rules and opaque refs."
                )
            )
        )
    return [system, context, *recent, *automation_order, HumanMessage(content=latest_text)]


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
    presented = semantic_context.model_input.get("presented_availability")
    for operation in turn.operations:
        if not operation.continues_previous:
            operations.append(operation)
            continue
        selection = operation.selection
        if (
            isinstance(presented, dict)
            and selection is not None
            and selection.kind == "ref"
            and isinstance(selection.ref, str)
            and _presented_option_by_ref(presented, selection.ref) is not None
        ):
            operations.append(operation)
            continue

        entities = operation.entities
        updates: dict[str, object] = {}
        cleared_temporal = (
            set(operation.cleared_verified_read_fields)
            if operation.type == "appointment_list"
            else set()
        )
        if (
            operation.type == "appointment_list"
            and operation.appointment_fact_challenge == "time"
        ):
            # The challenge target is server-owned. Never allow a model-supplied
            # appointment reference to override or substitute for the immediately
            # previous verified appointment identity.
            updates["appointment"] = _reference_from_verified(
                raw,
                single_key="appointment_ref",
            )

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
        elif (
            entities.date is None
            and previous_date is not None
            and "date" not in cleared_temporal
        ):
            updates["date"] = previous_date

        if fallback_disproved and previous_time is not None:
            updates["time"] = previous_time
        elif (
            entities.time is None
            and previous_time is not None
            and "time" not in cleared_temporal
        ):
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


def _presented_option_by_ref(
    raw: dict[str, object],
    option_ref: str,
) -> dict[str, object] | None:
    options = raw.get("options")
    if not isinstance(options, list):
        return None
    matches = [
        item
        for item in options
        if isinstance(item, dict) and item.get("option_ref") == option_ref
    ]
    return dict(matches[0]) if len(matches) == 1 else None


def merge_presented_availability_context(
    turn: TiaTurnUnderstanding,
    semantic_context: SemanticContext,
) -> TiaTurnUnderstanding:
    """Bind model-understood option refs to safe displayed facts, never raw slot authority."""
    raw = semantic_context.model_input.get("presented_availability")
    if not isinstance(raw, dict) or not raw:
        return turn

    last_selected = raw.get("last_selected_option_ref")
    operations: list = []
    changed = False
    for operation in turn.operations:
        if operation.type not in {"book", "reschedule"}:
            operations.append(operation)
            continue

        selection = operation.selection
        new_constraints_present = any(
            getattr(operation.entities, field) is not None
            for field in ("service", "doctor", "device", "date", "time")
        )
        if (
            selection is None
            and operation.continues_previous
            and not new_constraints_present
            and isinstance(last_selected, str)
            and last_selected
        ):
            selection = Selection(kind="ref", ref=last_selected)

        if selection is None or selection.kind != "ref" or not selection.ref:
            operations.append(operation)
            continue
        option = _presented_option_by_ref(raw, selection.ref)
        if option is None:
            operations.append(
                operation.model_copy(update={"selection": selection})
                if selection != operation.selection
                else operation
            )
            changed = changed or selection != operation.selection
            continue

        updates: dict[str, object] = {}
        inherited = (
            ("service", _reference_from_verified(raw, single_key="service_ref")),
            ("doctor", _reference_from_verified(raw, single_key="doctor_ref")),
            ("device", _reference_from_verified(raw, single_key="device_ref")),
        )
        for field, value in inherited:
            if getattr(operation.entities, field) is None and value is not None:
                updates[field] = value

        if option.get("concrete") is True:
            start_local = option.get("start_local")
            start_time = option.get("start_time_24h")
            if operation.entities.date is None and isinstance(start_local, str) and start_local:
                try:
                    local_dt = datetime.fromisoformat(start_local)
                except ValueError:
                    local_dt = None
                if local_dt is not None:
                    updates["date"] = DateConstraint(
                        mode="exact",
                        start_date=local_dt.date().isoformat(),
                    )
            if operation.entities.time is None and isinstance(start_time, str) and start_time:
                updates["time"] = TimeConstraint(
                    mode="exact",
                    start_time=start_time[:5],
                    start_time_ambiguity="none",
                )

        entities = (
            operation.entities.model_copy(update=updates) if updates else operation.entities
        )
        normalized = operation.model_copy(
            update={
                "entities": entities,
                "selection": selection,
                "continues_previous": True,
                "fresh_task": False,
                "fresh_task_explicit_fields": [],
            }
        )
        changed = changed or normalized != operation
        operations.append(normalized)
    return turn.model_copy(update={"operations": operations}) if changed else turn


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





def enforce_unscoped_task_boundary(
    turn: TiaTurnUnderstanding,
    semantic_context: SemanticContext,
) -> TiaTurnUnderstanding:
    """Fail closed when raw dialogue is the only possible task-local authority.

    With no persisted active task, a booking/reschedule may continue only a typed
    verified read (or the explicitly supported Pulse-purchase continuation). Otherwise
    Python forces a fresh-task boundary before any verified-context merge. This keeps
    old dialogue useful for language while preventing expired/finished task constraints
    from becoming execution state again.
    """
    active_task = semantic_context.model_input.get("active_task")
    if isinstance(active_task, dict) and active_task:
        return turn

    recent_read = semantic_context.model_input.get("recent_verified_read")
    recent_action = semantic_context.model_input.get("recent_verified_action")
    presented_availability = semantic_context.model_input.get("presented_availability")
    has_verified_read = (
        isinstance(recent_read, dict) and bool(recent_read)
    ) or (
        isinstance(presented_availability, dict) and bool(presented_availability.get("options"))
    )
    pulse_action = (
        recent_action.get("operation_type") == "buy_pulse_pack"
        if isinstance(recent_action, dict)
        else False
    )

    # Compound operations are resolved jointly from the latest customer message.
    # Their cross-operation facts are same-turn semantic input, not stale-history recovery.
    # Keep that established path intact; this guard closes only an unscoped standalone
    # booking/reschedule that could otherwise be reconstructed solely from old dialogue.
    if len(turn.operations) != 1:
        return turn

    operations = []
    changed = False
    for operation in turn.operations:
        if operation.type not in {"book", "reschedule"} or operation.execution_intent != "execute":
            operations.append(operation)
            continue

        verified_continuation = bool(
            operation.continues_previous
            and (has_verified_read or (operation.type == "book" and pulse_action))
        )
        automation_targeted = (
            operation.automation_context_relationship == "appointment_action"
        )
        if verified_continuation or automation_targeted:
            operations.append(operation)
            continue

        if operation.fresh_task:
            operations.append(operation)
            continue

        operation = operation.model_copy(update={"fresh_task": True})
        changed = True
        operations.append(operation)

    return turn.model_copy(update={"operations": operations}) if changed else turn


def isolate_fresh_task_context(
    turn: TiaTurnUnderstanding,
) -> TiaTurnUnderstanding:
    """Strip task-local context that the latest message did not explicitly supply."""
    operations = []
    changed = False
    for operation in turn.operations:
        if not operation.fresh_task or operation.type not in {"book", "reschedule"}:
            operations.append(operation)
            continue

        explicit = set(operation.fresh_task_explicit_fields)
        entities = operation.entities
        entity_updates: dict[str, object] = {}
        for field in ("service", "doctor", "device", "appointment", "package", "date", "time"):
            if field not in explicit and getattr(entities, field) is not None:
                entity_updates[field] = None
        if "package" not in explicit and entities.package_sessions is not None:
            entity_updates["package_sessions"] = None

        if entity_updates:
            entities = entities.model_copy(update=entity_updates)

        updates: dict[str, object] = {
            "entities": entities,
            "continues_previous": False,
            "selection": None,
        }
        if "appointment" not in explicit:
            updates["source_appointment"] = None
        if "package_usage" not in explicit:
            updates["package_usage"] = "unspecified"

        isolated = operation.model_copy(update=updates)
        changed = changed or isolated != operation
        operations.append(isolated)

    return turn.model_copy(update={"operations": operations}) if changed else turn


def _verified_automation_local_date(
    raw: dict[str, object],
    *,
    timezone_name: str | None,
) -> DateConstraint | None:
    """Derive the unchanged local appointment date only from verified automation metadata."""
    if not timezone_name:
        return None
    start_at = raw.get("start_at")
    if not isinstance(start_at, str) or not start_at:
        return None
    try:
        parsed = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        timezone = ZoneInfo(timezone_name)
    except (KeyError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return DateConstraint(
        mode="exact",
        start_date=parsed.astimezone(timezone).date().isoformat(),
    )


def merge_automation_context(
    turn: TiaTurnUnderstanding,
    semantic_context: SemanticContext,
    *,
    timezone_name: str | None = None,
) -> TiaTurnUnderstanding:
    """Bind only typed relations to server-verified automation metadata."""
    raw = semantic_context.model_input.get("automation_context")
    if not isinstance(raw, dict) or not raw:
        return turn

    # A social-only operation immediately after a server-owned automation is
    # necessarily an acknowledgement of that conversational focus. Normalizing
    # this marker is deterministic and grants no read/write authority.
    normalized_operations = []
    normalized = False
    for operation in turn.operations:
        if (
            operation.type == "social"
            and operation.execution_intent == "informational"
            and operation.automation_context_relationship == "none"
        ):
            operation = operation.model_copy(
                update={"automation_context_relationship": "acknowledge"}
            )
            normalized = True
        normalized_operations.append(operation)
    if normalized:
        turn = turn.model_copy(update={"operations": normalized_operations})

    appointment = _reference_from_verified(raw, single_key="appointment_ref")
    service = _reference_from_verified(raw, single_key="service_ref")
    device = _reference_from_verified(raw, single_key="device_ref")

    operations = []
    changed = False
    for operation in turn.operations:
        relationship = operation.automation_context_relationship
        if relationship == "appointment_action" and appointment is not None:
            selector = operation.source_appointment or AppointmentSelector()
            selector = selector.model_copy(update={"appointment": appointment})
            operation = operation.model_copy(update={"source_appointment": selector})
            if operation.type == "reschedule":
                explicit = set(operation.appointment_action_explicit_fields)
                entity_updates: dict[str, object] = {}
                if "date" not in explicit and operation.entities.date is not None:
                    entity_updates["date"] = None
                if "time" not in explicit and operation.entities.time is not None:
                    entity_updates["time"] = None
                entities = (
                    operation.entities.model_copy(update=entity_updates)
                    if entity_updates
                    else operation.entities
                )
                if "time" in explicit and "date" not in explicit:
                    preserved_date = _verified_automation_local_date(
                        raw,
                        timezone_name=timezone_name,
                    )
                    if preserved_date is not None:
                        entities = entities.model_copy(update={"date": preserved_date})
                operation = operation.model_copy(update={"entities": entities})
            changed = True
        elif relationship == "next_session" and operation.type == "book":
            entities = operation.entities
            updates: dict[str, object] = {}
            if entities.service is None and service is not None:
                updates["service"] = service
            if entities.device is None and device is not None:
                updates["device"] = device
            if updates:
                operation = operation.model_copy(
                    update={"entities": entities.model_copy(update=updates)}
                )
                changed = True
        operations.append(operation)

    return turn.model_copy(update={"operations": operations}) if changed else turn


def merge_verified_action_context(
    turn: TiaTurnUnderstanding,
    semantic_context: SemanticContext,
) -> TiaTurnUnderstanding:
    """Ground only facts the model explicitly links to the previous verified action."""
    raw = semantic_context.model_input.get("recent_verified_action")
    if not isinstance(raw, dict):
        return turn

    operation_type = raw.get("operation_type")
    operations = []
    changed = False

    if operation_type == "book":
        recent_appointment = _reference_from_verified(
            raw,
            single_key="appointment_ref",
        )
        for operation in turn.operations:
            if (
                operation.continues_previous
                and operation.type == "cancel_appointment"
                and recent_appointment is not None
            ):
                selector = operation.source_appointment or AppointmentSelector()
                current_appointment = selector.appointment
                has_current_appointment_identity = (
                    current_appointment is not None
                    and (
                        current_appointment.ref is not None
                        or current_appointment.text not in (None, "")
                        or bool(current_appointment.candidate_refs)
                    )
                )
                if not has_current_appointment_identity:
                    selector = selector.model_copy(
                        update={"appointment": recent_appointment}
                    )
                    operation = operation.model_copy(
                        update={"source_appointment": selector}
                    )
                    changed = True
            operations.append(operation)
        return (
            turn.model_copy(update={"operations": operations})
            if changed
            else turn
        )

    if operation_type != "buy_pulse_pack":
        return turn

    device = _reference_from_verified(raw, single_key="device_ref")
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
            changed = True
        operations.append(operation)

    if not changed:
        return turn
    return turn.model_copy(update={"operations": operations})


def _same_clock_or_twelve_hour_variant(value: str | None, explicit: str) -> bool:
    raw = str(value or "").strip()[:5]
    if not raw:
        return False
    try:
        raw_hour, raw_minute = (int(part) for part in raw.split(":", 1))
        exp_hour, exp_minute = (int(part) for part in explicit.split(":", 1))
    except (TypeError, ValueError):
        return False
    return raw_minute == exp_minute and (raw_hour == exp_hour or abs(raw_hour - exp_hour) == 12)


def _preserve_explicit_clock_constraints_v2(
    turn: TiaTurnUnderstanding,
    *,
    latest_customer_text: str,
) -> TiaTurnUnderstanding:
    """Make one explicit colon-formatted clock authoritative before AM/PM resolution."""
    explicit = extract_single_explicit_hhmm(latest_customer_text)
    if explicit is None:
        return turn

    operations = []
    changed = False
    for operation in turn.operations:
        entities = operation.entities
        time_constraint = entities.time
        if (
            time_constraint is not None
            and time_constraint.start_time is not None
            and _same_clock_or_twelve_hour_variant(time_constraint.start_time, explicit)
        ):
            time_constraint = time_constraint.model_copy(
                update={
                    "start_time": explicit,
                    "start_time_ambiguity": "none",
                }
            )
            entities = entities.model_copy(update={"time": time_constraint})
            changed = True
        elif (
            len(turn.operations) == 1
            and operation.type in {"book", "reschedule", "availability"}
        ):
            entities = entities.model_copy(
                update={
                    "time": TimeConstraint(
                        mode="exact",
                        start_time=explicit,
                        start_time_ambiguity="none",
                    )
                }
            )
            changed = True

        selection = operation.selection
        if (
            selection is not None
            and selection.kind == "time"
            and _same_clock_or_twelve_hour_variant(selection.time, explicit)
        ):
            selection = selection.model_copy(
                update={"time": explicit, "time_ambiguity": "none"}
            )
            changed = True

        operations.append(
            operation.model_copy(update={"entities": entities, "selection": selection})
            if entities != operation.entities or selection != operation.selection
            else operation
        )
    return turn.model_copy(update={"operations": operations}) if changed else turn


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
    latest_index = _latest_customer_index(history)
    latest_customer_text = _message_text(history[latest_index]) if latest_index is not None else ""
    explicit_safe = _preserve_explicit_clock_constraints_v2(
        invocation.value, latest_customer_text=latest_customer_text
    )
    presented = merge_presented_availability_context(explicit_safe, semantic_context)
    bounded = enforce_unscoped_task_boundary(presented, semantic_context)
    isolated = isolate_fresh_task_context(bounded)
    continued = merge_verified_read_context(isolated, semantic_context)
    continued = merge_verified_action_context(continued, semantic_context)
    continued = merge_automation_context(
        continued,
        semantic_context,
        timezone_name=timezone_name,
    )
    grounded = ground_turn_references(continued, semantic_context)
    grounded = merge_same_turn_pulse_device_context(grounded, semantic_context)
    normalized = normalize_semantic_invariants(grounded)
    resolved = resolve_turn_times_by_clinic_hours(normalized, semantic_context)
    return dedupe_exact_operations(resolved)