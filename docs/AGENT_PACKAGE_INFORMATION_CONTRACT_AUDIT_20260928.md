# Package Information Response Contract Audit + Conditional Migration — 2026-09-28

## Starting baseline

Starting main SHA:

5a90b4b507c8956551a93334ec8986a892b342da

Stable prior phases:
- Phase 3A Availability: CLOSED
- Phase 3B Price / Device: CLOSED
- Phase 3C Doctor / Provider: CLOSED

## Audit decision

Migration justified: YES

Chosen path:

B — Focused Package Information contract migration

Reason:

The read layer already owns package truth correctly, including tenant/patient isolation, exact remaining sessions, canonical effective status, active package offers, and non-financial filtering. The material gap is at the response boundary: response_goal=package_information still used the generic free-form responder and had no package-specific deterministic guard. Therefore the model could still rewrite an exact remaining-session count, blur offer-vs-owned semantics, or add commercial/package claims not requested.

Phase 3D migrates only pure answered package_information outcomes to backend-owned PackageTruth and deterministic rendering.

No package LLM composer is added.

## Actual Package Information Families

### 1. Patient-owned package information

Interpreter operation:
package_info

Semantic detail:
requested_package_details contains "owned", or defaults to owned when no package detail is supplied.

Planner:
- disposition=read
- read kind=customer_packages
- response_goal=package_information

Runtime outcome:
- status=answered
- response_goal=package_information

Read source:
list_patient_packages()

Isolation:
- workspace_id=context.workspace.id
- patient_id=context.patient.id
- optional service_id filter
- optional package_id filter
- optional device filter after read

Financial mode:
include_financials=False

Customer-safe read fields:
- name
- sessions_purchased
- sessions_reserved
- sessions_consumed
- sessions_remaining
- laser_device_name
- purchased_at
- expires_at
- status
- effective_status
- source

Phase 3D PackageTruth intentionally narrows this further to the facts needed for safe customer package state:
- name
- device_name, when present
- sessions_purchased
- sessions_remaining
- effective_status
- expires_at, when present

The current owned-package read does not expose a customer-visible service_name, so Phase 3D does not invent or add one to OwnedPackageInfo.

Identity-critical:
- package name
- device qualifier when present

Usage/session-critical:
- sessions_purchased
- sessions_remaining

Status-critical:
- effective_status
- expires_at

Financial facts present in the underlying PatientPackageRead domain model but explicitly excluded from the agent read include:
- sale_price_minor
- amount_paid_minor
- amount_refunded_minor
- balance_due_minor
- purchase_transaction_id
- cancellation_default_charge_minor
- standalone_session_price_minor_at_purchase

Current pre-Phase-3D responder:
generic free-form responder.

Current package-specific post-hoc guard:
none.

Material gap:
YES.
Exact package state was verified backend truth but still model-authored prose.

### 2. Clinic package offers

Interpreter operation:
package_info

Semantic detail:
requested_package_details contains "offers".

Planner:
- disposition=read
- read kind=package_offers
- response_goal=package_information

Read:
list_package_offers(
    workspace_id=...,
    service_id=...,
    active_only=True,
)

Additional verified filters can include:
- package_id
- device_key
- package session count

Actual offer DTO includes:
- service_name
- device_name
- sessions_count
- price_minor
- currency
- is_active
- standalone_session_price_minor
- savings_minor
plus internal IDs and timestamps.

Phase 3D package-information ownership intentionally keeps only:
- service_name
- device_name
- sessions_count

Package price/currency/savings remain owned by Phase 3B Price/Device.

A response-shaping whitelist removes commercial fields from package_information outcomes before mixed legacy response composition. The same package_offers read used by response_goal=answer_price is unchanged and retains its commercial facts.

Current pre-Phase-3D responder:
generic free-form responder.

Material gap:
YES.
The model could describe an offer as owned or surface package pricing even when the semantic operation was package information rather than pricing.

### 3. Package information requesting both owned state and offers

requested_package_details can contain both:
- owned
- offers

Planner performs both reads in one package_info step:
- customer_packages
- package_offers

Outcome:
- status=answered
- response_goal=package_information

Phase 3D represents the two sources as separate structural collections:
- owned_packages
- package_offers

They are never merged into one ambiguous package list.

### 4. Package pricing

Operation:
pricing

Read:
package_offers

Response goal:
answer_price

Ownership:
Phase 3B CommercialTruth

Migration in Phase 3D:
NO

PackageTruth contains no amount, currency, savings, or standalone-session price.

### 5. Package purchase

Operation:
buy_package

Read:
package_offers

Write:
buy_package

Terminal response:
package_purchased when completed

Ownership:
Terminal contract + existing purchase/write policy

Migration in Phase 3D:
NO

### 6. Package consumption / appointment package usage

Package reservation/consumption/release and appointment package application remain business/write behavior.

Migration:
NO

Phase 3D performs zero package mutation writes.

### 7. Package refund quote / ask_package_choice

ask_package_choice is shared by more than read-only package information, including refund-quote/package-selection flows.

Because TurnOutcome does not encode a package-info-only discriminator for this shared response goal, Phase 3D does not migrate ask_package_choice.

This avoids silently expanding into refund/purchase semantics.

The existing legacy path remains responsible for that shared clarification.

## Ownership boundaries

Clinic offer exists
!= patient owns it

Patient owns package
!= package has remaining sessions

Remaining sessions > 0
!= package is automatically selected for an appointment

Package definition/state
!= package price

Package price
= Phase 3B CommercialTruth

Package purchase
= write/Terminal path

Package session consumption
= package business/write layer

## Patient isolation

Owned packages are read with both:
- workspace_id
- patient_id

The read test now explicitly asserts those exact context IDs are passed to list_patient_packages().

The agent read also forces:
include_financials=False

No cross-patient package aggregation is used by package_info.

## Canonical remaining sessions and status

sessions_remaining is calculated by patient_packages.package_read():

opening balance
- reserved usage
- consumed usage

with a floor of zero.

effective_status is calculated backend-side:
- cancelled stays cancelled
- expired is derived by canonical package service logic
- active with zero remaining becomes exhausted
- otherwise canonical package status is preserved

The response layer does not recalculate either fact.

## Focused PackageTruth

OwnedPackageInfo:
- name
- device_name?
- sessions_purchased
- sessions_remaining
- effective_status
- expires_at?

PackageOfferInfo:
- service_name
- device_name?
- sessions_count

PackageTruth:
- owned_requested
- offers_requested
- owned_packages[]
- package_offers[]
- owned_complete_set
- offers_complete_set

No financial or commercial price field exists in PackageTruth.

## Complete-set semantics

Package information reads return the full verified set within their scoped read.

Therefore:
- if owned was requested, owned_complete_set=true;
- if offers was requested, offers_complete_set=true.

An explicit package_id/service/device filter can reduce that verified scope to one package; the one-item result remains the complete set for that scoped query.

No complete-set semantics are added to purchase, refund, or pricing flows.

## Duplicate visible identities

Internal IDs do not cross the response boundary.

If two owned packages or two offers are indistinguishable using the PackageTruth fields visible to the customer, deterministic rendering fails closed with a request for an additional clinic-visible distinction.

It does not:
- expose a UUID;
- invent a suffix;
- silently collapse records;
- pick one arbitrarily.

## Architecture options

### Option A — no migration

Rejected after audit.

Backend reads are strong, but response_goal=package_information remained generic free-form with no package factual guard. That leaves exact sessions/status and offer-vs-owned semantics model-authored.

### Option B — prompt tightening only

Insufficient for exact remaining-session/status truth.

### Option C — PackageTruth + deterministic rendering

Chosen.

Flow:

verified package reads
-> TurnOutcome(package_information)
-> CustomerResponseContract
-> backend PackageTruth
-> deterministic exact rendering

No model is needed.

### Option D — symbolic Package LLM composer / universal framework

Rejected.

Package information has no presentation problem that justifies an additional model call. Deterministic sections for owned packages and available offers are simple, natural enough, and materially safer.

## Pure-path rendering

Pure answered package_information exits before the generic responder.

Source:
deterministic:package-contract

Rendering separates:
- "packages you own"
- "package offers available to purchase"

Owned rendering can show exact:
- package name
- device
- purchased sessions
- remaining sessions
- effective status
- expiry

Offer rendering can show exact:
- service
- device
- session count

It intentionally does not render package price.

## Mixed legacy path

If package_information is combined with an unsupported response family, the whole response remains on the existing legacy responder.

No half-new / half-legacy reply is introduced.

A focused legacy rule now states:
- package_offers never prove ownership;
- customer_packages are required for ownership/remaining/status/expiry claims;
- package price/currency belong to explicit pricing outcomes.

Additionally, package_information outcome shaping whitelists the non-commercial package fields before the generic responder sees them.

## Existing guards

Package-specific post-hoc factual guard:
NONE

The pre-existing generic protections still apply:
- internal ID filtering;
- generic grounded TURN_OUTCOMES instruction;
- Phase 3B commercial contract for price answers;
- Terminal contract for purchases;
- package business policies for reservation/consumption.

Guard removed:
NONE

## Non-changes

Interpreter: UNCHANGED
Planner: UNCHANGED
Read executor: UNCHANGED
Package pricing: UNCHANGED
Price/Device composer: UNCHANGED
Terminal composer: UNCHANGED
Availability composer: UNCHANGED
Doctor contract: UNCHANGED
Booking engine: UNCHANGED
Package purchase logic: UNCHANGED
Package consumption logic: UNCHANGED
Pulse logic: UNCHANGED
Financial ownership/payment flow: UNCHANGED
Appointment lifecycle: UNCHANGED

The only response-layer shaping change outside the contract/renderer is a package_information-only whitelist that strips commercial offer fields before generic mixed composition.

## LLM call budget

Pure package information:
0 response-composer LLM calls.

No validator model is added.

additional_llm_calls = 0

## Expected validation counters

- invented_packages
- wrong_package_identity
- wrong_package_service_bindings
- wrong_package_device_bindings
- wrong_session_counts
- wrong_remaining_session_counts
- owned_offer_confusion
- cross_patient_package_reads
- omitted_required_packages
- duplicate_packages
- incorrect_package_status
- financial_field_leaks
- financial_writes
- package_mutation_writes
- additional_llm_calls

All material counters must be zero before closure.


## Deterministic validation evidence

Focused package contract / responder / read boundary:
- 80 passed

Responder architecture + package contract:
- 19 passed

Full V2 regression sweep:
- 638 passed

Package business regressions covering offers, purchase semantics, refund lifecycle, package usage/payment separation, and patient packages:
- 47 passed

Agent-eval tooling:
- Ruff PASS
- compileall PASS
- 39 passed

Backend static checks:
- Ruff PASS
- compileall PASS
- Alembic single head: 0086_all_service_packages

## Bounded live validation

One-off staging validation ran on:
- workflow: Package Information Contract Live Review
- run: 36477701283
- branch commit: 41eed6eb02c0d4b472ebd471b21375e728430d83

Scenarios:
1. owned active package with exact remaining sessions/device/expiry
2. expired owned package
3. multiple owned packages
4. device-specific available package offer
5. owned packages plus available offers
6. no owned packages
7. mixed package-information + service-information legacy response

Observed response sources:
- deterministic:package-contract — 6
- openai:gpt-5.6-luna mixed legacy responder — 1

No scenario performed a database read, financial write, package mutation, repricing, or package-session deduction.

Manual review:
- NATURAL: 5
- ACCEPTABLE: 2
- ROBOTIC/MATERIAL: 0

The expired-package response is ACCEPTABLE: it reports canonical expired status and the exact historical expiry date, though the phrasing is slightly formal.

The mixed legacy response is ACCEPTABLE: it describes the clinic package as currently offered and the requested service description, without claiming patient ownership, package balance, or any package price.

Material counters:
- invented_packages = 0
- wrong_package_identity = 0
- wrong_package_service_bindings = 0
- wrong_package_device_bindings = 0
- wrong_session_counts = 0
- wrong_remaining_session_counts = 0
- owned_offer_confusion = 0
- cross_patient_package_reads = 0
- omitted_required_packages = 0
- duplicate_packages = 0
- incorrect_package_status = 0
- financial_field_leaks = 0
- financial_writes = 0
- package_mutation_writes = 0
- additional_llm_calls = 0

Patient/tenant isolation is established separately by deterministic read coverage asserting the current workspace_id and patient_id are passed to list_patient_packages() and include_financials=False.

The only live response-model call was the already-existing generic responder for the deliberately mixed unsupported response set. Pure PackageTruth rendering adds zero model calls. No validation/verifier model call exists.
