# Price / Device Response Contract Migration — 2026-09-28

## Scope

Phase 3B migrates read-only commercial price/device responses from model-authored commercial wording toward backend-owned typed truth.

In scope:
- service base-price answers;
- service device-specific price answers;
- multi-device service price answers;
- package-price answers that already use operation=pricing and response_goal=answer_price;
- device clarification outcomes that already carry verified device-price pairs.

Out of scope:
- payment recording;
- checkout;
- settlement;
- appointment payment;
- package purchase/consumption;
- Pulse purchase/consumption;
- availability behavior;
- doctor migration;
- terminal composer changes.

Starting main SHA:
4cf683b263c24d2dfc73f3976578132b45d3fd6f

## Actual runtime trace

Interpreter -> Planner -> Verified Reads -> Outcome Builder -> CustomerResponseContract -> Responder

### Family 1 — ordinary service pricing

Planner:
operation.type=pricing

If the request does not target package sessions/package_id:
- requires service_id;
- reads service_catalog;
- response_goal=answer_price.

Outcome:
- status=answered
- response_goal=answer_price

Outcome builder asks service_catalog shaping for requested detail "price".

Non-device service shape:
service_catalog.service:
- name
- price
- currency (when present)

The price is built from verified price_minor/currency where available.

Current baseline responder behavior:
_pure_price_reply is already deterministic before the free-form responder. This is an important baseline fact: Phase 3B is not replacing a free-form pure base-price response.

Material risks before typed contract:
commercial truth still exists only as loose response facts; compound/mixed responses can still enter the generic responder.

### Family 2 — explicit device-specific service price

The semantic context/planner carries device_key in step facts when the customer selected a concrete device.

service_catalog shaping compares selected_device_key to the verified priced device rows and emits:

service_catalog.service.selected_laser_device:
- device_name
- price

Outcome:
- status=answered
- response_goal=answer_price

Current baseline responder behavior:
deterministic pure-price renderer binds service + selected device + verified price.

Material failure to prevent structurally:
a later composer must never substitute another device price, remove the required device qualifier, or convert the device price into a base price.

### Family 3 — general service price with multiple device prices

For a service requiring a laser device, when no device is selected and multiple configured device prices exist, outcome shaping emits:

service_catalog.service:
- name
- requires_laser_device=true
- laser_devices[]:
  - device_name
  - price

Outcome:
- status=answered
- response_goal=answer_price

Current baseline pure responder is deterministic and renders every device-price pair.

The existing CustomerResponseContract already preserves this relationship as one required complete fact named device_price_options.

Phase 3B strengthens this into typed commercial option bindings so service/device/amount/currency cannot be rematched by the model.

### Family 4 — package pricing inside the pricing operation

Planner already treats package pricing as a subcase of operation.type=pricing.

Trigger:
- package_sessions in parameters; or
- concrete package_id.

Read:
package_offers

Response:
- status=answered
- response_goal=answer_price

Verified package-offer DTO contains:
- service_name
- device_name
- sessions_count
- price_minor
- currency
plus internal identifiers and accounting metadata.

Only the customer-safe commercial tuple is projected into Phase 3B.

Important baseline defect discovered during trace:
planner can pass package_id, but baseline _read_package_offers() ignores it.
Therefore a concrete package-price question can return unrelated active offers.

Phase 3B includes one minimal read correction:
if package_id is supplied, filter the already-verified active offer rows by that UUID before creating the response facts.

This does not change package purchase, package ownership, or financial write semantics.

### Family 5 — device clarification with verified price pairs

Current production guard specifically handles:
- status=needs_input
- response_goal=clarification
- facts.needed=device
- facts.availability.laser_device_options containing device_name + price pairs.

This state appears when the flow needs the user to choose a device and already has verified device-price options.

The existing post-hoc guard:
_deterministic_device_price_guard_reply()

extracts canonical device-price pairs and replaces model prose when a required pair is missing or unbound.

This is the main remaining production case where device-price truth can enter the generic responder before post-hoc repair.

If availability facts contain device_price_conflicts, Phase 3B does not migrate that unit; conflict handling remains on the legacy path.

## Commercial truth model

Phase 3B introduces backend-owned CommercialTruth and CommercialPriceOption.

CommercialPriceOption binds:
- qualifier: base | device | package
- service_name
- amount
- currency
- device_name when applicable
- sessions_count when applicable

Commercial kinds:
- service_base_price
- service_device_price
- service_device_price_options
- package_price_options
- device_price_clarification
- price_unavailable

The model never owns or reconstructs these values.

## Base price vs device-specific price

The qualifier is structural.

Base:
- qualifier=base
- device_name=None

Device:
- qualifier=device
- device_name is required

Package:
- qualifier=package
- sessions_count is required
- device_name is preserved only when the verified offer is device-specific

No code maps a generic base price to an arbitrary device.

For services requiring device pricing, outcome shaping already prefers selected_laser_device or laser_devices and does not expose a base price as a substitute device price.

## Complete-set semantics

Complete-set is not applied universally.

Single verified base price:
complete_set=false

Single explicitly selected device price:
complete_set=false

Multiple device-specific prices for a general service-price question:
complete_set=true

Device clarification with multiple verified price pairs:
complete_set=true

Multiple matching package-price offers:
complete_set=true

The structured validator still requires every option reference that the typed commercial unit says must be represented. For a single exact option this means exactly one bound ref; for complete sets it means every verified option in contract order.

## Architecture options

### Option A — prompt tightening only

Rejected.
Exact money/currency/device bindings are business truth and should not depend on model instruction following.

### Option B — legacy responder + stronger post-hoc guard

Rejected as the target architecture.
It can repair some missing/mismatched pairs but still lets the model author the commercial statement first.

### Option C — typed Price/Device contract

Chosen.

Flow:
verified pricing outcome
-> CustomerResponseContract
-> backend CommercialTruth
-> symbolic commercial refs
-> bounded structured composer when presentation choice is useful
-> deterministic exact-value resolution

Pure ordinary service prices remain zero-LLM deterministic because the baseline already had a safe deterministic path and there is no benefit in adding a model call.

### Option D — universal generic composer framework

Rejected.
Availability, terminal actions, and commercial pricing have different correctness invariants. Extracting a large common framework would increase migration risk for little benefit.

## Symbolic references

The price/device composer sees only references such as:

unit_0_price_option_0
unit_0_price_option_1

The composer input also receives:
- unit index;
- response goal;
- commercial kind;
- option count;
- complete-set flag;
- reply language.

It does not receive:
- service names;
- device names;
- prices;
- currencies;
- session counts;
- DB IDs;
- raw customer/history content.

Customer history is inspected locally only to determine reply language.

## Structured validation

Validation rejects:
- unknown ref;
- duplicate ref;
- missing required ref;
- reordered refs;
- cross-unit ref;
- wrong unit index;
- units without typed CommercialTruth;
- duplicate commercial signatures;
- malformed commercial shapes.

The model cannot decide commercial correctness.

## Deterministic rendering and fallback

Backend rendering resolves each commercial ref to the original typed option.

Provider failure, structured-output failure, invalid refs, schema/validation failure use:

deterministic:price-device-contract-fallback

The fallback:
- uses the same CommercialTruth;
- performs 0 business reads;
- performs 0 writes;
- performs 0 repricing;
- performs 0 availability recalculation;
- adds 0 third-model calls.

## Model-call budget

No validation model is added.

Pure service/base/device price answers:
0 composer calls beyond existing interpreter flow.

Device clarification / package pricing may use one bounded structured composer call where the legacy path already used the generic response composer.

No:
Interpreter -> validator LLM -> response LLM

## Mixed responses

The new path is used only when every response unit is a supported typed commercial unit.

Examples such as:
answer_price + answer_service

remain entirely on the legacy responder.

No half-new / half-legacy composition is introduced.

## Existing device-price guard

Location:
app/agents/v2/responder.py

Functions:
- _device_price_clarification_pairs()
- _device_price_pair_is_visible()
- _deterministic_device_price_guard_reply()

Primary protected production shape:
needs_input clarification where needed=device and verified laser_device_options contain price pairs.

Pure migrated device-price clarification exits before this guard in Phase 3B.

The guard is NOT removed because mixed/legacy response sets remain able to reach it.

## Device pricing is not availability

CommercialTruth never claims:
- a device is available at a specific date/time;
- a device has current appointment capacity;
- a priced device implies an appointment slot.

A verified price binding only means the backend has that commercial mapping for the service/device/package query.

Availability Phase 3A remains unchanged.

## Minimal non-contract change

_read_package_offers() now honors an explicit package_id already produced by the planner.

Reason:
without this filter, an exact package-price query may return multiple unrelated offers, which makes the upstream commercial truth broader than the requested verified target.

This is a read-only filtering correction. No pricing calculation or financial write behavior changes.

## Legacy prompt/guard cleanup

Guard removed:
NONE

General prompt cleanup:
NONE

The legacy device-price guard and generic responder remain necessary for mixed/unsupported response sets.

## Counters required for validation

Material counters:
- hallucinated_prices
- wrong_device_price_bindings
- invented_devices
- invented_services
- wrong_currency
- omitted_required_price_options
- duplicate_price_options
- incorrect_base_price_claims
- financial_writes
- additional_llm_calls

All material correctness counters must finish at zero.
