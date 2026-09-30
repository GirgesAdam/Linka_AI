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

A post-merge adversarial probe found one remaining edge: a separately composed low-risk legacy companion could still invent a conflicting exact value even though it never received the typed outcome. Example: social acknowledgment + verified 500 EGP could produce "900 EGP" in the legacy fragment beside the correct deterministic 500 EGP fragment.

The final hardening therefore removes free-form companion composition whenever a mixed set contains typed truth. Low-risk non-factual companions are rendered deterministically for:
- social_ack;
- handoff;
- active_task_cancelled;
- clarification with no verified choices.

Unsupported-only turns still use the existing generic responder. Unsupported factual/choice units are not promoted into a new contract in this task.

Device-price clarification remains excluded from the per-unit deterministic typed renderer so the existing legacy mixed device-price guard stays reachable when no other protected typed unit exists. When it coexists with another protected typed unit, the same existing CommercialTruth deterministic fallback renders it without a model.

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

Mixed replies containing typed truth do not include any free-form generic responder fragment. Unsupported-only turns retain the existing generic responder fallback/guard behavior.

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
- 34 passed after the hybrid single-unit follow-up.

Additional post-merge adversarial control:
- verified price 500 EGP + low-risk social companion + forced model draft claiming 900 EGP;
- responder model calls = 0;
- final contains 500 EGP and does not contain 900 EGP.

Final production-smoke follow-up:
- a reachable service+price turn produced one hybrid answer_price unit carrying verified price plus service-description facts;
- this shape bypassed the mixed-unit guard and still reported an OpenAI responder source;
- the follow-up narrows only the already-verified CommercialTruth projection for rendering, while the established deterministic price helper remains first for single price+duration behavior;
- no business read/write/repricing logic changed.

Program-level representative regression:
- 304 passed.

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
