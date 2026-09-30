# Linka Response Contract Program — Final Closeout

Date: 2026-09-30  
Starting main SHA: dcb55b34694a6647f4611f8c64fb6de80ae6742a

## Verdict

RESPONSE CONTRACT PROGRAM = OPEN

The two historical blockers were revalidated on the current merged production state.

- Verified Choice Presentation: CLOSED.
- Mixed Typed Truth exact-value corruption: CLOSED.
- Mixed Typed Truth required-unit completeness: FAILED.

Two material P2 response-layer findings remain. No runtime fix is implemented by this closeout.

## Main closeout question

Are there any remaining material response-ownership gaps inside the Response Contract Program?

YES.

A reachable mixed-response path silently drops a requested unsupported unit when another unit has typed ownership. The live example was Pulse balance + Appointment Information: the appointment unit rendered correctly, while the requested Pulse unit disappeared instead of returning the safe lack-of-data answer that the pure Pulse request produces.

## Finding 1 — Mixed requested-unit omission

Severity: material P2  
Affected family: Mixed Typed composition  
Verified truth source: current TurnOutcome set, including AppointmentInfoTruth plus the requested Pulse-information outcome  
Observed failure:
- pure Pulse request on the same production patient: "مش ظاهر قدامي رصيد الـPulses الحالي بتاعك."
- Pulse + appointment request: final reply contained only the verified appointment and omitted the Pulse part entirely.
- isolated response-layer probe reproduced the behavior with source `deterministic:mixed-typed-contract`, zero model calls, and no Pulse/balance text in the final reply.

Why existing contract/guard is insufficient:
- `_compose_mixed_typed_contract_reply` protects typed exact facts by rendering typed units deterministically.
- unsupported units are only preserved when they match the small deterministic low-risk companion set.
- a requested `pulse_information` unit with no PulseTruth is therefore skipped once another typed unit owns the mixed reply.

Minimal recommended follow-up:
- preserve every requested unit in mixed composition.
- for an unsupported/no-truth unit, render a bounded safe "not available/not confirmed" fragment from its verified outcome semantics, without giving a free-form model ownership of typed facts.
- do not introduce a universal response framework.

## Finding 2 — Arabic Service Information presentation corruption

Severity: material P2 presentation-layer defect  
Affected family: ServiceTruth deterministic renderer  
Verified truth source: ServiceTruth  
Observed production failures include:
- pure duration: `??????: PRP للبشرة. ????? ???????? ??????: 45 ?????.`
- service + price: exact 2000 EGP price was preserved, but the service-information portion was garbled.
- appointment + service: appointment truth was correct, while the service-list prefix/separators were garbled.

Source inspection confirms literal `????` strings remain in `backend/app/agents/v2/service_info_composer.py`.

Why existing contract/guard is insufficient:
- ownership and grounding are correct.
- corruption is introduced by deterministic customer-facing literals after truth ownership has already been resolved.
- the result is production-reachable and partially unreadable, so this is more than cosmetic/formality.

Minimal recommended follow-up:
- replace only the damaged Arabic literals in the Service Information renderer.
- add a regression that rejects literal question-mark runs in customer-facing Arabic output.
- do not change ServiceTruth ownership or prompts.

## Previous blocker revalidation

### Mixed typed truth exactness

Adversarial current-main tests passed for:
- 500 EGP preserved against attempted 900 EGP corruption.
- verified 18:00 preserved against attempted 20:00 corruption.
- 1000 Pulses preserved against attempted 700.
- appointment 17:00 preserved against attempted 19:00.
- hybrid service-details + answer_price keeps exact CommercialTruth.
- social acknowledgment + exact typed price cannot introduce contradictory free-form facts.
- representative typed mixes preserve required typed units and bindings.

Focused blocker/choice regression: 54 passed.

Current material counter:
- mixed_omitted_required_units = 1.

### Verified choices

Revalidated:
- Hydrafacial / PRP cannot become Botox / Filler.
- complete A/B/C set renders all three once.
- duplicate visible labels fail safe without leaking internal identity.
- unknown/invented choices do not appear.
- cross-unit choice sections stay bound to their units.
- service, ordinary device, time, appointment and non-financial package choices use exact verified labels.
- DoctorTruth remains the doctor-choice owner.
- priced-device clarification remains CommercialTruth-owned.
- refund-specific package choices remain outside this contract family.

Verified Choices verdict: CLOSED.

## Ownership map

| Domain | Owner | Pure path | Mixed behavior | Fallback | Generic responder reachable for owned truth? | Material ownership risk |
| --- | --- | --- | --- | --- | --- | --- |
| Availability | AvailabilityTruth | bounded composer | deterministic typed unit | deterministic availability fallback | no | no |
| Price / Device | CommercialTruth | bounded composer | deterministic typed commercial unit/projection | deterministic price/device fallback | no | no |
| Doctor | DoctorTruth | deterministic composer | deterministic typed unit | deterministic doctor renderer | no | no |
| Package information | PackageTruth | deterministic composer | deterministic typed unit | deterministic package renderer | no | no |
| Patient profile | PatientTruth | deterministic composer | deterministic typed unit | deterministic patient renderer | no | no |
| Appointment information | AppointmentInfoTruth | deterministic composer | deterministic typed unit | deterministic appointment renderer | no | no |
| Service information | ServiceTruth | deterministic composer | deterministic typed unit | deterministic service renderer | no | presentation defect remains |
| Clinic information | ClinicTruth | deterministic composer | deterministic typed unit | deterministic clinic renderer | no | no |
| Pulse information | PulseTruth | deterministic composer | deterministic typed unit when truth exists | deterministic Pulse renderer | no | missing-unit gap when truth is absent in mixed reply |
| Completed actions | ActionTruth / Terminal contract | bounded terminal composer | deterministic typed unit | deterministic terminal fallback | no | no |
| Verified choices | Choice contract from TurnOutcome.choices | deterministic renderer | deterministic typed unit | deterministic choice renderer | no | no |
| Mixed composition | existing typed owners | n/a | backend concatenation | owner-specific deterministic renderers | typed facts: no | required-unit completeness gap |

## Generic responder final review

Remaining production-reachable generic composition is acceptable for:
- standalone social acknowledgment;
- ordinary non-medical handoff wording;
- safe customer-history prose after response shaping;
- general payment-method/timing policy with no customer ledger ownership;
- generic clarification with no verified choice set;
- low-risk unsupported prose.

Refund quote prose remains generic but belongs to the separate Financial/Refund domain boundary.

Material unsafe generic callers found: 0.

The current OPEN verdict is not caused by generic responder existence. Finding 1 occurs because the generic responder is intentionally bypassed and an unsupported requested unit is silently omitted.

## Legacy guards

Reviewed and regression-tested:
- availability semantic guard;
- device-price guard;
- doctor completion helper;
- compatibility helper;
- patient/package/clinic/Pulse response shaping;
- deterministic medical handoff;
- financial ownership protections;
- recursive internal-ID filtering.

Any guard now unsafe: NO.  
Guard cleanup: OUT OF SCOPE.

## Financial / Refund boundary

Refund / settlement / payment / ledger = Separate Financial / Refund Program.

Current Response Contract does not own:
- payment execution truth;
- settlement truth;
- customer ledger truth;
- refund lifecycle truth.

General payment policy stays informational and strips customer ledger data. Refund-specific package choices remain excluded from Verified Choice hardening.

## Medical boundary

Medical / suitability / urgent medical remains a deterministic safe handoff.

Live urgent-medical response used `deterministic:medical-handoff`; no clinical diagnosis or suitability judgment was generated.

Medical Safety = separate future program if product behavior expands beyond handoff.

## Security / privacy

Focused current-main regression confirms:
- internal appointment/service/doctor/patient/workspace/package/transaction refs are removed from customer-visible contracts;
- clinic provider tokens/API keys are filtered;
- Patient/CRM history removes profile and financial fields;
- Pulse response shaping removes ledger IDs, purchase transaction IDs, workspace IDs and financial metadata;
- current workspace + patient scope is preserved by patient-specific reads;
- refund/ledger/payment internals do not become generic policy facts.

No cross-patient/cross-tenant leak was found in representative coverage.

## Stale context controls

Current verified truth beat stale conversation wording for:
- appointment;
- availability;
- Pulse;
- clinic contact;
- service.

A dedicated closeout availability probe confirmed stale 20:00 assistant wording could not override verified 18:00 availability.

## GPT-6 Luna regression

Production model: gpt-6-luna  
Responses API: active  
Reasoning effort: low  
Structured outputs: enabled

Current production logs during bounded closeout validation show:
- `v2-turn-interpreter model=gpt-6-luna fallback=False`;
- `v2-customer-responder model=gpt-6-luna fallback=False`.

Representative model/provider + contract suite passed. Planner/write gates passed. No GPT-6-specific material semantic failure was observed.

## Validation

Focused historical blockers + doctor/price owners:
- 54 passed.

Security / stale-context / financial / medical / guard regression:
- 167 passed.

Representative program-level Response Contract regression:
- 354 passed.

Planner / interpreter / write-safety regression:
- 94 passed.

Agent-eval tooling:
- 39 passed.

Static gates:
- Ruff: PASS.
- compileall: PASS.
- Alembic single head: `0086_all_service_packages`.
- git diff --check: PASS before this docs-only report.

## Bounded production live review

- service + price: exact price preserved; service prose garbled -> ROBOTIC/MATERIAL.
- service + availability: availability grounded; service prose garbled -> ROBOTIC/MATERIAL.
- clinic + availability: grounded and usable -> NATURAL/ACCEPTABLE.
- appointment + service: appointment grounded; service-list presentation garbled -> ROBOTIC/MATERIAL.
- Pulse + appointment: appointment grounded; requested Pulse unit omitted -> ROBOTIC/MATERIAL.
- ambiguous service choice: exact canonical choices, `deterministic:verified-choice-contract` -> NATURAL/ACCEPTABLE.
- generic acknowledgment: natural, `openai:gpt-6-luna` -> NATURAL.
- urgent medical: safe deterministic handoff -> NATURAL/ACCEPTABLE.
- pure service duration: production-reachable literal `????` corruption -> ROBOTIC/MATERIAL.

Console errors in the bounded browser run: 0.

Appointment choice was not live-triggered because forcing a cancellation ambiguity could cause a real write if only one appointment were selected; it remains covered by deterministic adversarial regression instead.

## Material counters

- wrong_typed_numeric_facts = 0
- wrong_typed_times = 0
- wrong_entity_bindings = 0
- wrong_status_claims = 0
- invented_verified_entities = 0
- invented_choices = 0
- omitted_required_choices = 0
- duplicate_choices = 0
- cross_patient_response_facts = 0
- cross_tenant_response_facts = 0
- internal_id_leaks = 0
- financial_field_leaks = 0
- secret_leaks = 0
- false_write_acknowledgments = 0
- false_payment_claims = 0
- false_settlement_claims = 0
- wrong_tool_calls = 0
- wrong_writes = 0
- additional_verifier_llm_calls = 0
- mixed_omitted_required_units = 1
- material_presentation_defects = 1

Because material counters are non-zero, the program cannot be declared closed.

## Production baseline

Latest successful Railway deployment at review time:
- source commit: `00074aca0db7bd9da46a4b4bdffbd53ae1f7275b`
- status: SUCCESS.

Later main commits are frontend/report/release-toggle changes and do not modify the Agent/Response Contract runtime.

Health:
- `/api/v1/health/live` = 200, app=Linka.
- `/api/v1/health/ready` = 200, app=Linka, database=connected.

WhatsApp:
- transport/tick = 200
- connections_ready = 1
- inbound_failed = 0
- send_failed = 0

Vercel repository state:
- `deploymentEnabled = false`.

This closeout changes documentation only. Deployment = NOT REQUIRED.

## Final decision

RESPONSE CONTRACT PROGRAM = OPEN

Do not start Financial/Refund, Medical, responder cleanup, guard cleanup, prompt cleanup, generic framework work, GPT-6 optimization, or reasoning optimization from this closeout.
