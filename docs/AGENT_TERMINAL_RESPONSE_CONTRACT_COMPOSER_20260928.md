# Terminal Response Contract Composer — 2026-09-28

## Scope

Phase 2 migrates only pure completed terminal write outcomes to the typed response contract path. Mixed turns and all non-terminal response families remain on the existing responder.

Supported goals:

- booking_completed
- reschedule_completed
- cancellation_completed
- appointment_confirmed
- package_purchased
- pulse_pack_purchased
- follow_up_created
- marketing_updated

active_task_cancelled remains on the legacy responder because it is terminal conversational state, not a clinic write, and including it now would expand the semantic surface without improving write-safety.

## Design comparison

### Option A — contract-only free-form composer

Simple, but the model still authors critical values and can transform completed action semantics into pending/availability/cancellation language. Phase 1 guarantees safe input, not faithful free-form reproduction. This is insufficient for the terminal migration.

### Option B — symbolic fact references + backend resolution

The model chooses presentation structure and references fact keys only. The backend resolves those keys against the original CustomerResponseContract. Exact service, doctor, device, date/time, package and Pulse values are therefore never copied back from model text.

This is the preferred direction.

### Option C — fully deterministic terminal templates

Highest factual safety, but makes every terminal response backend-authored and reduces conversational variation.

## Chosen design: Hybrid B/C

The implementation uses Option B for fact selection, ordering, and compound composition, plus one narrow Option C element: the semantic action acknowledgment is backend-owned.

The model output is a TerminalComposerDraft containing only:

- unit_index
- action_ref = unit_action
- style = plain | warm | friendly
- fact_keys
- transition = sentence | and | then

It never returns a fact value and never returns an action name.

Backend rendering owns:

- action identity
- action-success acknowledgment
- exact response fact values
- deterministic display formatting
- compound unit order

This keeps the critical semantics structural while still letting the model choose tone, optional detail, and connective flow.

## Production cutover

compose_v2_customer_reply first builds CustomerResponseContract.

If every response unit is one of the eight supported terminal goals and every unit has successful action truth:

TurnOutcome -> CustomerResponseContract -> terminal structured composer -> backend fact resolution

Otherwise:

TurnOutcome -> existing responder and existing guards

No half-new / half-old composition is performed.

## Action truth invariant

ActionTruth is derived from authoritative completed outcome state. The terminal composer validator requires:

- the unit exists;
- status/action truth represent a successful completed action;
- action identity exactly matches the response goal mapping;
- each compound unit appears exactly once and in original order.

The model cannot select cancellation for booking, confirmation for cancellation, or pending state for a completed write because no model field exists for those meanings.

## Critical fact strategy

Response facts remain display-safe typed values in CustomerResponseContract. The terminal model receives only fact keys and metadata, not values.

Backend-owned terminal facts currently include canonical aliases for:

- service_name
- doctor_name
- device_name
- start_local
- date
- time
- status
- package_used
- package_name
- pulses_remaining
- pulses_count
- follow_up_at
- marketing_consent

Only a narrow renderable subset is exposed to the composer. Status is intentionally not model-selectable because completed action state is already represented by backend-owned action acknowledgment.

Supporting availability evidence may supply one verified canonical service/start/doctor/device value to a terminal unit, but availability windows, option counts, checked dates, and search metadata are never projected as terminal response semantics.

## Financial safety

Terminal package and Pulse purchase composition does not expose amount_paid, sale price, balance, payment method, settlement, refund, or deduction facts unless a future contract explicitly introduces a safe canonical fact for that purpose.

Therefore:

package_purchased != payment recorded

pulse_pack_purchased != payment recorded

The composer cannot invent payment semantics because it has neither a payment act nor a payment fact reference.

## Compound terminal turns

Each CustomerResponseContract unit stays independent and ordered.

Example:

package_purchased
booking_completed

can become one natural reply, but the package action and booking action each retain their own action truth and fact namespace. A fact key from another unit is rejected.

## Structural validator

The final validator is intentionally small. It checks:

- unit count;
- unit_index and ordering;
- successful action truth;
- expected action identity;
- unique same-unit fact references;
- referenced fact existence;
- required fact selection;
- first-unit transition validity.

It does not inspect generated prose, scan for string values, redo planning, query the DB, or infer business state.

## Fallback

Provider failure, structured-output failure, or invalid references use:

deterministic:terminal-contract-fallback

The fallback renders the same action truth and safe response facts directly from CustomerResponseContract. It performs no reads or writes and does not change transaction ownership.

Because live V2 writes execute with commit=False and the outer live-chat transaction commits only after a reply is available, the fallback preserves successful-action acknowledgment without changing write commit semantics.

## Existing guards

Removed: NONE.

Availability, doctor completeness, device-price, compatibility, medical, and pure-price protections remain unchanged. Pure supported terminal turns bypass those responder guards because they use the terminal contract path. Mixed/non-terminal turns still use the legacy path and guards.

## Prompt changes

The legacy responder prompt is unchanged.

A new terminal composer prompt only describes the structured draft schema and explicitly states that the model must never return fact values. It reuses the existing realtime composer model/fallback chain.

## LLM call count

No additional LLM call is added.

For a pure supported terminal turn, the new composer call replaces the old responder call. The interpreter remains the first call; terminal composer is the second call.

## Deterministic comparison to old path

The new path improves terminal factual safety by construction:

- exact values: backend-resolved instead of model-copied;
- action identity: structural instead of prose-dependent;
- action success: structural instead of prose-dependent;
- PR #131 availability evidence: support-only, never terminal response intent;
- compound order: validated structurally;
- guard dependence: pure terminal path does not rely on post-hoc availability/device/doctor repairs.

The tradeoff is a modest deterministic rendering layer for action phrases and fact fragments. This is intentionally bounded to terminal actions and is not extended to availability, pricing, doctors, or device choice in Phase 2.
