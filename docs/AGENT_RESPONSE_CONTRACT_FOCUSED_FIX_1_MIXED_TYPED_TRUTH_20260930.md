# Linka Response Contract Focused Fix 1 — Mixed Typed-Truth Composition Hardening

Date: 2026-09-30

Starting main SHA:
2f2c85b0647ca043075e427a242e41bca2239805

## Scope

This fix changes only mixed customer-response composition.

It does not migrate a new business domain, change reads/writes, add a factual verifier model, remove legacy guards, or start Verified Choice hardening.

## Architecture decision

Option A — stronger generic responder prompt:
Rejected. The closeout probes already proved prompt-only protection is not a correctness guarantee.

Option B — deterministic rendering of existing typed units, then backend composition:
Chosen.

Option C — symbolic mixed LLM composition:
Not required. Deterministic concatenation is clear enough for the supported mixed families and avoids another model/schema.

Option D — universal response framework:
Rejected. It would expand scope and duplicate existing domain-specific ownership.

## Runtime behavior

Pure response sets are unchanged and continue through their established composers/renderers.

For a mixed response set:
1. build the existing CustomerResponseContract;
2. test each unit independently against the existing domain support predicate;
3. render supported typed units with that domain's existing deterministic fallback/renderer;
4. preserve unit order;
5. concatenate the immutable rendered fragments.

No typed value is passed to the generic responder for supported mixed typed units.

If a mixed set also contains a low-risk generic unit, only that unsupported unit subset is sent to the legacy responder. Allowed low-risk coexistence is intentionally narrow:
- social_ack;
- handoff;
- active_task_cancelled;
- clarification with no verified choices;
- customer-history prose when it is not AppointmentInfoTruth;
- general payment-policy information carrying booking_requires_payment + payment_execution_owner.

Other unsupported factual shapes remain on the pre-existing legacy path rather than being silently promoted into this fix.

Device-price clarification remains excluded from the new deterministic mixed-unit path so the existing mixed device-price guard remains production-reachable. Pure device-price clarification remains owned by the existing Price/Device contract.

## Existing truth owners reused

- ActionTruth / Terminal contract
- AvailabilityTruth
- CommercialTruth
- DoctorTruth
- PackageTruth
- PatientTruth
- AppointmentInfoTruth
- ServiceTruth
- ClinicTruth
- PulseTruth

No new truth model is introduced.

## Adversarial gates

The previous closeout failures are now structurally closed for supported typed mixes:
- verified 500 EGP cannot become 900 EGP;
- verified availability 18:00 remains the same verified time (customer formatting may render it as 6 PM);
- verified Pulse balance 1000 cannot become 700;
- verified appointment 17:00 remains the same verified time (customer formatting may render it as 5 PM).

Representative deterministic mixed controls:
- service + price;
- service + availability;
- clinic + availability;
- appointment + service;
- Pulse + appointment;
- package + service;
- doctor + service;
- terminal write + service.

Complete typed sets are rendered by their existing deterministic domain renderer, so option/item completeness is inherited rather than reimplemented.

## Fallback

Mixed typed composition itself has no provider call.

Therefore provider failure / structured-output failure cannot corrupt or remove its typed fragments.

Each typed fragment is generated from the same already-built verified contract. The mixed layer performs:
- 0 business reads;
- 0 writes;
- 0 repricing;
- 0 availability recalculation;
- 0 Pulse recalculation;
- 0 third-model calls.

Legacy generic segments retain their existing responder fallback/guard behavior and are never given the supported typed outcomes.

## Guard reachability

No guard was removed.

Still required:
- availability legacy semantic guard for unsupported/incomplete legacy availability shapes;
- device-price mixed guard for legacy priced-device clarification composition;
- doctor completion helper for remaining legacy mixed doctor surfaces;
- compatibility deterministic helper;
- patient/package/clinic/Pulse shaping;
- medical deterministic handoff;
- internal-ID filtering;
- financial ownership protections.

Potential cleanup is explicitly out of scope.

## Focused validation

Mixed/adversarial + responder/grounding/architecture:
- 33 passed.

Program-level representative regression:
- 303 passed.

Static:
- Ruff PASS.
- compileall PASS.
- Alembic single head: 0086_all_service_packages.
- git diff --check PASS.

## Required counters

For deterministic representative coverage:
- mixed_wrong_numeric_facts = 0
- mixed_wrong_times = 0
- mixed_wrong_entity_bindings = 0
- mixed_wrong_status_claims = 0
- mixed_omitted_required_units = 0
- mixed_duplicate_units = 0
- mixed_financial_field_leaks = 0
- mixed_internal_id_leaks = 0
- mixed_cross_patient_facts = 0
- false_write_acknowledgments = 0
- additional_business_reads = 0
- additional_writes = 0
- additional_verifier_llm_calls = 0

## STOP rule

This task does not implement:
- Verified Choice Presentation Hardening;
- Financial/Refund migration;
- Medical migration;
- generic responder cleanup;
- guard deletion;
- universal response framework;
- prompt cleanup.
