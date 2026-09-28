# Agent Response Contract Foundation — 2026-09-28

## Scope

Phase 1 adds a typed deterministic projection between `TurnOutcome` and a future language composer. It is shadow/test-only. The production responder path, prompts, model/provider, guards, planner policy, writes, state continuation, database schema, and frontend are unchanged.

Current path: `TurnOutcome -> customer_visible_outcome() -> current LLM responder -> existing guards`.

Target path: `TurnOutcome -> CustomerResponseContract -> future LLM composer -> small validation`.

## Chosen architecture

Option B: a separate `CustomerResponseContract` projection.

`TurnOutcome` remains the runtime/business result. The response contract is the language-boundary projection describing what the composer must or may say. This avoids coupling business execution results to presentation obligations and adds semantics that `customer_visible_outcome()` does not express: required/optional facts, complete sets, action truth, compound units, and stable fact keys.

Option A (strengthen `TurnOutcome`) has fewer objects but mixes runtime and presentation responsibilities. Option C (enhance `customer_visible_outcome()`) has the lowest migration cost but remains too dictionary-shaped for future fact-reference validation.

## Contract structure

The contract contains ordered `units`. Each unit keeps the authoritative `response_goal` and `status`, optional terminal `action_truth`, typed `facts`, and verified `choices`.

Each fact has:
- `key`: response-safe symbolic identity.
- `semantic_type`: entity_name/date/time/money/count/status/boolean/text/choice.
- `requirement`: required or optional.
- `value`: display-safe value.
- `complete_set`: whether item omission would be incorrect.

The contract deliberately does not add a second response-kind enum that duplicates `response_goal`.

## Required vs optional facts

Phase 1 uses one facts collection with a per-fact `requirement` field instead of parallel required/optional arrays. This keeps identity and policy together, prevents duplication across arrays, and is easier to validate later. `complete_set=true` represents verified sets such as doctor lists and availability windows.

## Compound turns

`build_customer_response_contract(outcomes)` emits one unit per outcome, preserving input order exactly. A turn containing `answer_price` then `present_availability` remains two distinct ordered semantic units.

## Action truth

For terminal writes, success is structural and comes only from authoritative outcome state: `status=completed` plus `action_result.ok=true`. The action name comes from the normalized `action_result.action` already established by `TurnOutcome`. Supporting read facts never decide whether a write succeeded.

## Supporting evidence separation

Booking/reschedule outcomes may carry availability verification evidence. The response contract does not project those windows as availability options for terminal goals. It may extract a single canonical customer-facing field such as service, start, doctor, or device, but it excludes checked-date search metadata, option counts, and availability-window presentation semantics.

Therefore `booking_completed + availability evidence` stays a booking-success unit, and `reschedule_completed + availability evidence` stays a reschedule-success unit.

## Critical relationships

Multi-device pricing remains one `device_price_options` fact containing explicit `device_name + price` pairs. Devices and prices are never split into separate arrays for the future composer to re-match. Verified choices likewise keep each label bound to its own response-safe facts.

## Fact references

Phase 1 does not render text segments. `ResponseFact.key` already provides a safe symbolic response-contract key addressable within a unit (for example `unit[0].facts[service_name]`). A later phase can add explicit segment references without exposing DB IDs or semantic grounding refs.

## Internal-data safety

The projection recursively removes exact internal IDs, every `*_id` / `*_ids`, every `*_ref` / `*_refs`, and raw choice refs. It creates no database identifiers of its own.

Payment-policy projection recognizes the existing payment-info ownership markers and excludes customer financial-ledger payloads from that policy surface. Package offers remain distinct from owned packages. Pulse offers, owned Pulse packs, and balances remain distinct; the projection does not infer ownership.

## Coverage

Terminal write goals produce `action_truth`: booking_completed, reschedule_completed, cancellation_completed, appointment_confirmed, package_purchased, pulse_pack_purchased, follow_up_created, and marketing_updated. `active_task_cancelled` remains an answered state outcome rather than a clinic write.

The builder also accepts all current non-terminal families: availability states, all choice goals, clarification, service/price/doctor/clinic/customer answers, package and Pulse information, refund quote, handoff, and social acknowledgment.

## Why this is not another business-truth layer

The builder performs no DB reads, API calls, LLM calls, availability calculation, pricing calculation, doctor compatibility decision, booking-success decision, or financial ownership decision. It only projects authoritative `TurnOutcome` data.

## Determinism

The builder uses no clock, randomness, UUID generation, environment state, database state, or provider calls. Equal inputs produce equal contracts and compound ordering is stable.

## Migration path

A later phase can migrate one response family at a time: shadow-build, compare required facts with current replies, enable contract input for one narrow family, remove only a guard proven redundant by evidence, then repeat. No terminal migration, prompt change, fact interpolation, or guard removal is part of Phase 1.
