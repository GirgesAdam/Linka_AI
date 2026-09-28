# Availability Response Contract Migration — 2026-09-28

## Scope

Phase 3A migrates only these customer-facing availability response goals:

- present_availability
- requested_time_unavailable
- no_availability

Pricing, doctor discovery, device-price choice, compatibility, service information, package/Pulse reads, terminal actions, and mixed supported/unsupported response sets remain on their existing paths.

## Starting baseline

- PR #132: typed CustomerResponseContract foundation
- PR #133: terminal response contract composer
- Starting main SHA: 5c7871de3c6b227343ffda1746d469965c7b3c7a

## Actual runtime outcome shapes

### present_availability

Runtime producer:

1. planner emits response_goal=present_availability and an availability read;
2. read_executor returns verified slots plus checked_dates, matching_slot_count and search_truncated;
3. outcome_builder converts slots through availability_windows_from_slots();
4. TurnOutcome is normally status=answered, response_goal=present_availability.

Customer-visible availability payload can contain:

- service_name
- checked_dates
- availability_windows
- available_option_count
- search_truncated
- price/device-price metadata as supporting read data

Each window can contain:

- doctor_name
- laser_device_name
- start_local
- end_local
- start_time_24h
- end_time_24h

Internal doctor/service/branch/device IDs are stripped before response composition.

### requested_time_unavailable

For an exact time constraint, read_executor filters canonical availability slots by that exact time. If zero slots match:

- exact_slot_match_count = 0
- matching_slot_count = 0
- outcome_builder returns status=blocked
- response_goal=requested_time_unavailable

The same exact-miss outcome does NOT carry alternative slots. Its availability_windows set is empty.

The requested exact time remains available in top-level step facts as the canonical time constraint. Before Phase 3A the CustomerResponseContract projection dropped this value. Phase 3A fixes that projection and exposes it as required requested_time.

A separate availability outcome may coexist in a compound response set and provide alternatives; the exact-miss unit itself does not manufacture them.

### no_availability

For a non-exact availability search with zero matching slots:

- status=blocked
- response_goal=no_availability
- availability_windows is empty
- checked_dates records the dates actually queried
- search_truncated records whether the requested search exceeded the bounded search horizon or used next/from-date bounded search

read_executor currently caps bounded availability date expansion at 14 days.

## Contract changes

CustomerResponseUnit now has backend-owned AvailabilityTruth:

- present_availability -> options_available
- requested_time_unavailable -> requested_time_unavailable
- no_availability -> no_availability

The model cannot author or select this state.

Phase 3A also fixes two projection gaps:

1. requested_time_unavailable now projects the canonical requested exact time from top-level outcome facts.
2. no_availability / requested_time_unavailable now preserve search_truncated alongside checked_dates.

Availability-only eligibility is explicit through is_pure_supported_availability_contract().

## Chosen architecture

Chosen option: separate availability composer with bounded infrastructure reuse.

No shared code was extracted from terminal_composer.py.

Reason:

- terminal composer is already production-stable;
- the overlap is limited to language detection and the existing model-chain invocation pattern;
- extracting those helpers would modify the stable terminal path without changing response safety;
- availability has materially different semantics: complete window sets, search scope, exact-time miss, and no-gap rules.

The availability composer reuses the existing:

- realtime composer model builder;
- fallback model builder;
- model-chain/circuit-breaker infrastructure;
- typed structured-output infrastructure;
- response contract models.

No universal response framework or policy engine was introduced.

## Model input isolation

The availability model receives no availability values and no raw customer/history text.

Its input contains only:

- customer reply language;
- unit_index;
- response_goal;
- backend availability state;
- symbolic window refs;
- window count;
- names of safe optional fact keys.

Example:

unit_0
availability_state=options_available
verified_window_refs=[unit_0_window_0, unit_0_window_1]
available_optional_fact_keys=[service_name]

The model never sees:

- exact slot times;
- exact dates;
- doctor names;
- device names;
- prices;
- service names;
- internal IDs.

History is used locally only to detect reply language. The actual customer message is not sent to the availability composer.

## Structured draft

AvailabilityComposerDraft is intentionally narrow.

Each unit contains:

- unit_index
- availability_ref=unit_availability
- style: plain | warm | friendly
- window_refs
- optional_fact_keys
- presentation_mode: compact | detailed
- closing_action
- transition

There is no free-form reply field.

There is no field that can change availability state.

## Window reference model

For unit N, verified windows are referenced as:

- unit_N_window_0
- unit_N_window_1
- ...

Refs are scoped to one response unit.

Validation rejects:

- unknown refs;
- duplicate refs;
- cross-unit refs;
- reordered/missing refs;
- wrong unit_index.

For options_available, window_refs must exactly equal the complete verified window set in contract order.

## Complete-set semantics

availability_windows remains:

- required
- complete_set=true

The model cannot decide which options to show.

Presentation mode can change layout only. Backend resolution always renders every verified window reference exactly once.

The older format_availability_windows_reply() is not used as the final contract renderer because it intentionally caps displayed groups/windows. Such caps are useful for a legacy prose fallback but are incompatible with complete_set=true.

The existing availability_windows_from_slots() remains the upstream verified grouping source.

## No-gap invariant

Phase 3A performs no new window merging.

All grouping/compression occurs upstream through availability_windows_from_slots(), which groups only verified start-time runs.

The contract renderer resolves each resulting window independently.

Two non-contiguous windows remain two independent options. The renderer never fills the interval between them.

Range wording explicitly says bookable START times (for example, “بدايات حجز من ... لـ...”), never session duration.

## requested_time_unavailable semantics

Backend truth owns the exact-miss act.

The canonical requested time is resolved from the required requested_time fact.

checked_dates scopes where that requested time was actually checked.

The model cannot rewrite the requested time and cannot turn an exact miss into options_available or no_availability.

The current runtime does not put alternatives in the same exact-miss outcome. If a separate present_availability unit exists in the same response set, Phase 3A renders the miss unit first and the verified alternative unit separately.

## no_availability scope

no_availability is always rendered against checked search scope.

Examples:

- one checked date -> “no availability on [date]”
- consecutive checked dates -> scoped date range
- non-consecutive dates -> explicit checked-date set
- missing checked dates -> “the checked search scope”, never global future availability

The renderer never says the clinic has no future availability globally.

## search_truncated

search_truncated is backend-owned contract data.

When true on no_availability, the final response explicitly states that the reported zero-result claim applies only to the checked search scope.

The model does not need to know the search algorithm or 14-day implementation limit.

## Composer fallback

Provider failure, structured-output failure, invalid symbolic refs, invalid ordering, or validation failure returns:

deterministic:availability-contract-fallback

The fallback:

- uses the same AvailabilityTruth;
- resolves the same exact backend values;
- includes the complete window set;
- preserves no-gap behavior;
- preserves checked search scope;
- performs no business reads or writes.

## Production cutover

compose_v2_customer_reply now evaluates response contracts in this order:

1. pure supported terminal contract -> terminal contract composer
2. pure supported availability contract -> availability contract composer
3. everything else -> existing deterministic pre-checks / legacy responder / post-hoc guards

No half-new / half-old composition exists.

## Legacy availability guard reachability

The new pure availability path never calls:

- _verified_availability_claim()
- _deterministic_availability_guard_reply()
- ResponderDraft.availability_claim

These remain reachable for mixed legacy responses, for example:

present_availability + answer_service

or any response set containing an availability unit plus an unsupported non-terminal family.

Therefore guard removal is NOT safe in Phase 3A.

Guard removed: NONE.

## Remaining response families requiring availability_claim

After Phase 3A, availability_claim is no longer required for pure availability-only response sets.

It remains required by the generic ResponderDraft path whenever a mixed/legacy response set can contain one of:

- present_availability
- requested_time_unavailable
- no_availability

Until mixed composition migrates or the responder schema is split, AvailabilityClaim and the legacy availability guard remain production-reachable.

## Legacy prompt cleanup candidates

These responder prompt rules are dead for the pure availability-only path but remain necessary for mixed legacy responses:

- availability_claim semantic-state instructions;
- availability windows are bookable START-time ranges;
- do not infer session duration from availability timestamps;
- exact requested-time unavailable semantics;
- no_availability zero-result semantics;
- candidate-missing is not global future unavailability;
- availability evidence in non-availability outcomes is support-only.

They are documented only. No prompt cleanup is performed in Phase 3A.

## Interpreter / planner / engine

Interpreter changes: NONE.

Planner changes: NONE.

Availability engine/read executor changes: NONE.

Outcome-builder business behavior changes: NONE.

The only pre-composer contract change is deterministic projection of existing runtime facts that were already present but previously omitted.

## Next migration recommendation

The next migration should be PRICE/DEVICE because the current legacy responder still has deterministic device-price correction logic and exact price/device binding is already represented structurally in CustomerResponseContract.

Doctor-list migration can follow independently.

Stop after Phase 3A merge/deploy validation.
