# Phase 3J — Pulse Information Response Contract Audit

Date: 2026-09-30
Starting main SHA: 91763d426742ee7da742eb7167529ac944afb8c2
Decision: B) Focused Pulse Information contract migration is materially justified.

## Actual Pulse Information Families

### Family: Current Pulse balance
- status: answered
- response_goal: pulse_information
- Read source: pulse_balance -> list_patient_pulse_balances(workspace_id, patient_id)
- Verified backend facts: device_key, device_name, pulses_purchased, pulses_consumed, pulses_remaining, active_pack_count
- Customer-safe facts: device_name, exact pulses_remaining; active pack aggregation is backend-computed
- Commercial facts: none
- Financial/write-owned facts: none
- Current responder behavior: generic free-form responder receives pulse_balance facts
- Existing deterministic protections: exact backend aggregation and patient/workspace scoped read
- Existing guards: prompt says balance may only come from pulse_balance/pulse_packs; no final-text numeric guard
- Material failure modes: model can alter/omit exact remaining count or swap a device label after verified read
- Migration justified: YES

### Family: Owned Pulse packs
- status: answered
- response_goal: pulse_information
- Read source: pulse_packs -> list_patient_pulse_packs(workspace_id, patient_id, device_key, include_financials=False)
- Verified backend facts: device_name, pulses_purchased, pulses_consumed, pulses_remaining, purchased_at, expires_at, status, effective_status
- Customer-safe facts: the verified fields above
- Commercial facts: none in the Agent read
- Financial/write-owned facts: sale price, paid/refunded/due amounts, transaction ids are deliberately excluded
- Current responder behavior: generic free-form responder
- Existing deterministic protections: read whitelist and include_financials=False
- Existing guards: prompt distinguishes owned packs from offers
- Material failure modes: model can confuse owned/offer wording or mutate counts/status
- Migration justified: YES

### Family: Available Pulse pack offers
- status: answered
- response_goal: pulse_information
- Read source: pulse_pack_offers -> list_pulse_pack_offers(workspace_id, active_only=True), optionally filtered by verified device/count
- Verified backend facts: device_name, pulses_count, price_minor, currency, is_active
- Customer-safe facts: device_name, exact pulse quantity, exact display price/currency
- Commercial facts: Pulse offer price is owned by the Pulse offer DTO/read path; Phase 3B CommercialTruth does not currently model Pulse offers
- Financial/write-owned facts: no payment or settlement state
- Current responder behavior: generic free-form responder
- Existing deterministic protections: workspace scope, active-only filter, deterministic device/count filtering
- Existing guards: prompt says offer existence never proves customer ownership
- Material failure modes: model can swap quantity/device/price or phrase an offer as owned
- Migration justified: YES

### Family: Device-specific Pulse overage information
- status: answered
- response_goal: pulse_information
- Read source: pulse_billing_settings -> list_pulse_billing_settings(workspace_id), optionally device filtered
- Verified backend facts: device_name, overage_price_minor, currency; if one device and an explicit count is supplied, Python computes requested_pulse_count and overage_total_minor
- Customer-safe facts: device, exact unit price/currency, exact requested count and deterministic total when available
- Commercial facts: Pulse billing settings are the canonical Pulse overage source; Phase 3B does not duplicate this price
- Financial/write-owned facts: no charge, settlement, payment, or checkout state
- Current responder behavior: generic free-form responder
- Existing deterministic protections: workspace/device filtering and Python arithmetic for counted overage
- Existing guards: no post-responder Pulse device/price guard
- Material failure modes: model can swap device/price pairs or alter deterministic total
- Migration justified: YES

### Family: Pulse pack purchase
- status: completed
- response_goal: pulse_pack_purchased
- Read source: unique verified pulse_pack_offers read, then buy_pulse_pack write
- Verified backend facts: unique offer binding; write result amount_paid_minor=0
- Customer-safe facts: purchased Pulse quantity/device; pack added to account
- Commercial facts: offer verification before write
- Financial/write-owned facts: payment remains Reception-owned; Agent purchase passes amount_paid_minor=0 and payment_method=unknown
- Current responder behavior: already owned by the deterministic terminal contract
- Existing deterministic protections: terminal ActionTruth + terminal composer
- Existing guards: responder prompt and deterministic terminal wording do not claim payment/settlement
- Material failure modes in Phase 3J surface: none requiring migration
- Migration justified: NO; retain existing terminal contract

### Family: Pulse consumption / settlement / appointment billing
- status: not Agent-owned
- response_goal: none for an Agent write
- Read/write source: receptionist-owned billing domain
- Verified backend facts exposed to Phase 3J: none required
- Customer-safe facts: execution boundary only when relevant
- Financial/write-owned facts: consumption, settlement, payment, cash-vs-Pulse choice, checkout
- Current responder behavior: interpreter/normalization/prompt enforce Reception ownership
- Existing deterministic protections: planner has no Agent Pulse consumption/settlement write intent
- Existing guards: financial_ledger semantic marker is split to handoff and never authorizes a financial read
- Material failure modes: false claims if generic model overstates a read-only outcome
- Migration justified: protect read-only Pulse information; do not alter these write boundaries

## Runtime trace

Interpreter:
- operation families: pulse_info, buy_pulse_pack
- requested Pulse detail families: balance, owned_packs, offers, overage_price, financial_ledger

Planner:
- balance -> pulse_balance
- owned_packs -> pulse_packs
- offers -> pulse_pack_offers
- overage_price -> pulse_billing_settings
- buy_pulse_pack -> verified pulse_pack_offers + existing buy_pulse_pack write

Verified reads:
- patient-owned data is scoped to current workspace + current patient
- clinic offers/settings are scoped to current workspace
- device/count filtering is deterministic in Python
- owned-pack Agent reads explicitly set include_financials=False

Outcome builder:
- current Pulse reads fall through generic _read_facts/_visible_value
- internal IDs are filtered, but Pulse intent-specific customer-safe shaping is absent
- offer timestamps/metadata can remain wider than the requested customer surface

CustomerResponseUnit:
- no PulseTruth exists on the starting baseline
- pulse_information becomes generic ResponseFact entries

Responder:
- pure Pulse information has no deterministic contract branch
- generic LLM receives the exact verified facts and is instructed not to invent
- after model composition there is no Pulse balance/quantity/device/price enforcement

## Existing guards

Guard: workspace + patient scoping in list_patient_pulse_packs/list_patient_pulse_balances
Purpose: prevent cross-patient/cross-tenant ownership reads
Pure path: YES
Mixed/legacy path: YES
Still production reachable: YES

Guard: workspace scoping in offers/settings
Purpose: prevent cross-tenant commercial reads
Pure path: YES
Mixed/legacy path: YES
Still production reachable: YES

Guard: include_financials=False for Agent-owned-pack reads
Purpose: prevent payment/refund/due/transaction leakage
Pure path: YES
Mixed/legacy path: YES
Still production reachable: YES

Guard: financial_ledger normalization split
Purpose: keep receptionist-owned Pulse financial questions out of Agent financial reads
Pure path: N/A
Mixed/legacy path: YES
Still production reachable: YES

Guard: generic responder Pulse prompt rules
Purpose: distinguish offers from owned balance/packs and forbid payment/consumption inference
Pure path after migration: redundant but retained
Mixed/legacy path: YES
Still production reachable: YES

Guard: deterministic terminal contract for pulse_pack_purchased
Purpose: purchase acknowledgment without payment/settlement invention
Pure path: YES
Mixed/legacy path: terminal-only
Still production reachable: YES

## Commercial truth boundary

Phase 3B CommercialTruth currently owns service/package price response goals, not Pulse pack offers or Pulse overage settings. Pulse commercial amounts originate canonically from PulsePackOfferRead and PulseBillingSettingsRead. The Phase 3J contract may project those verified display values, but it must not create payment, settlement, balance-due, or ledger truth.

## Decision

PULSE INFORMATION RESPONSE CONTRACT MIGRATION = REQUIRED

Reason:
The business reads and ownership boundaries are already correct, but exact Pulse balance/count/device/price facts remain model-authored in the final response. A deliberately wrong responder draft can currently pass through because there is no PulseTruth/pure deterministic renderer or post-model Pulse guard. The smallest safe change is a read-only, domain-specific PulseTruth plus deterministic pure Pulse renderer and intent-specific outcome shaping. No planner, booking, checkout, settlement, payment, or consumption redesign is justified.


## Implemented focused migration

OLD:
verified Pulse reads
→ generic customer-visible facts
→ free-form responder
→ prompt-only protection for exact balance/count/device/price facts

NEW:
verified Pulse reads
→ intent-specific customer-safe shaping
→ typed PulseTruth
→ deterministic pure Pulse renderer

The migration is intentionally read-only and domain-specific. No planner, write executor, booking,
checkout, settlement, payment, Pulse-consumption, or frontend behavior changed.

Pure Pulse information now bypasses the generic LLM responder entirely. Mixed Pulse + unsupported-family
responses retain the existing legacy responder and existing Pulse prompt guards.

## Deterministic validation completed before PR

- Focused Phase 3J + existing Pulse/response/terminal suites: 127 passed.
- V2 regression suite from backend/ cwd: 749 passed, 1188 deselected.
- Agent-eval tooling tests: 39 passed.
- Ruff (modified files and CI backend scope): PASS.
- compileall (app, alembic, tests, agent-eval tooling): PASS.
- Alembic: single head 0086_all_service_packages.
- git diff --check: PASS.
- Linka branding grep in new customer-facing Pulse copy: no Tia / Tia AI / تيا.

Manual deterministic review:
- current balance: ACCEPTABLE
- owned Pulse pack: ACCEPTABLE
- available Pulse offer: ACCEPTABLE
- device-scoped counted overage: ACCEPTABLE
- combined balance + offer + overage: ACCEPTABLE

All reviewed replies preserve exact verified numbers/device bindings and make no payment, settlement,
deduction, or automatic-billing claim.

## Material safety counters — deterministic phase

invented_pulse_balances = 0
wrong_pulse_balances = 0
invented_pulse_packs = 0
wrong_owned_pulse_packs = 0
owned_offer_confusion = 0

wrong_pulse_quantities = 0
wrong_device_pulse_bindings = 0
wrong_overage_prices = 0
wrong_currency = 0

false_pulse_consumption_claims = 0
false_payment_claims = 0
false_settlement_claims = 0
automatic_billing_claims = 0

cross_patient_pulse_reads = 0
cross_patient_pulse_response_facts = 0
cross_tenant_pulse_reads = 0

pulse_internal_id_leaks = 0
financial_field_leaks = 0

pulse_consumption_writes = 0
settlement_writes = 0
financial_writes = 0
appointment_writes = 0

additional_llm_calls = 0

The live bounded scenarios are intentionally deferred until the focused PR is merged and the same merged
SHA is available in an execution environment. The existing Railway tia-agent-eval service is pinned to the
older feat/agent-pulse-domain-core branch and is therefore not valid evidence for this Phase 3J patch.

## Live validation finding and focused semantic follow-up

Initial post-merge bounded Demo run on merge SHA 3e35b15892918b334b4a7c7a74690771da4d6648
executed seven selected cases through the existing tools.agent_eval.run_pulse_domain harness with
Railway production environment injection. Each case ran inside its own outer SQL transaction and rolled
back on exit.

Initial result:
- 6/7 fully correct.
- balance: PASS, deterministic:pulse-contract, exact 1000, no write.
- offer price: PASS, deterministic:pulse-contract, exact device/quantity/800.00 EGP, no write.
- overage price: PASS, deterministic:pulse-contract, exact device/unit price, no write.
- counted overage: PASS, deterministic:pulse-contract, 1000 × 1.50 EGP = 1500.00 EGP, no write.
- purchase without payment: PASS; Pulse pack write completed inside rollback transaction, payment count
  unchanged, purchase_transaction_id null, and final reply made no payment/settlement claim.
- financial-ledger question: PASS; no financial read/write, Reception handoff.
- owned-pack remaining: material interpreter classification issue. The realistic phrase
  الباقة اللي عندي على Candela Gentle فاضل فيها كام Pulse؟ was interpreted as balance, producing a
  canonical aggregate balance read instead of owned_packs. The demo happened to have one active pack,
  so the numeric answer was the same, but this is unsafe when multiple packs exist on one device.

Follow-up:
- strengthened only the V2 Pulse semantic contract/prompt discriminator:
  aggregate device balance -> balance; a particular owned pack's purchased/used/remaining/status/expiry
  -> owned_packs.
- no keyword/regex routing or Python raw-text inspection was added.
- focused prompt/schema + Pulse tests: 57 passed.
- targeted live rerun of owned-pack remaining: 1/1 PASS, verified_reads=[pulse_packs],
  deterministic Pulse contract response, zero write attempts.
