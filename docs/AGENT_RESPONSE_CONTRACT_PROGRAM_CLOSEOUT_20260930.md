# Linka Response Contract Program — Program-Level Review & Closeout

Date: 2026-09-30  
Starting main SHA: b71c617a1fd63420df9658e60c443b85602e996d  
Reconciled main SHA: f29d0e856e72d035205a2cf860a9197fb73a3414

## Final verdict

RESPONSE CONTRACT PROGRAM = OPEN

Remaining material response-layer findings:
1. Mixed responses can bypass typed truth ownership and let the generic responder alter exact already-verified values.
2. Several verified-choice families remain generic and can replace/omit canonical choice labels without a completeness/identity guard.

No migration is implemented by this closeout task.

Financial/refund and medical work are explicitly not converted into new Response Contract phases.

## Runtime response inventory

| Family / operation | Response goal | Typical status | Pure owner | Generic reachable | Risk | Recommendation |
| --- | --- | --- | --- | --- | --- | --- |
| Service info | answer_service | answered | ServiceTruth | mixed only | low pure / medium mixed | pure closed; covered by Finding 1 when mixed |
| Service/package pricing | answer_price | answered | CommercialTruth | mixed only | low pure / material mixed | Finding 1 |
| Doctor discovery | answer_doctor | answered | DoctorTruth | mixed only | low pure / medium mixed | legacy doctor completion guard retained |
| Doctor choice | ask_doctor_choice | needs_input | DoctorTruth | no for valid pure | low | closed |
| Clinic info | answer_clinic_info | answered | ClinicTruth | mixed only | low pure / medium mixed | pure closed; Finding 1 when exact clinic facts are mixed |
| General payment policy | answer_clinic_info | answered | none | yes | low | safe generic; no ledger access |
| Availability | present_availability / requested_time_unavailable / no_availability | answered/blocked | AvailabilityTruth | mixed only | low pure / material mixed | Finding 1 |
| Appointment information | answer_customer_history | answered | AppointmentInfoTruth when appointments wrapper present | mixed only | low pure / material mixed | Finding 1 |
| Customer profile | answer_customer_profile | answered | PatientTruth | mixed only | low pure / medium mixed | pure closed |
| Customer history summary | answer_customer_history | answered | none; safe-shaped history | yes | low-medium | acceptable legacy composition; no financial/profile PII |
| Package information | package_information | answered | PackageTruth | mixed/failed-write paths | low pure / medium mixed | pure closed; mixed covered by Finding 1 |
| Pulse information | pulse_information | answered | PulseTruth | mixed/failed-write paths | low pure / material mixed | Finding 1 |
| Completed writes | booking/reschedule/cancel/confirm/package purchase/Pulse purchase/follow-up/marketing | completed goals | ActionTruth / Terminal contract | mixed terminal sets can fall legacy if not pure | low pure / material if mixed with unsupported unit | Finding 1 where mixed |
| Service/device/package/appointment/time choices | ask_*_choice | needs_input | only doctor and priced-device clarification have typed ownership | yes for remaining choice families | material P2 | Finding 2 |
| Generic clarification | clarification | needs_input/blocked | generic safe facts | yes | low-medium | no migration unless exact verified options are present |
| Refund quote | package_refund_quote / ask_package_choice | answered/needs_input/blocked | financial/refund domain read | yes | high financial semantics | Separate Financial/Refund Program |
| Human support / payment dispute / privacy | handoff | handoff | handoff facts | yes except medical | low | acceptable |
| Medical / urgent medical | handoff | handoff | deterministic medical handoff | no responder LLM | low | Separate Medical Safety Program only if routing/safety policy changes |
| Social acknowledgment | social_ack | answered | generic outcome | yes | low | no migration |
| Compatibility failure | clarification-like verified incompatibility | needs_input | deterministic compatibility helper | no generic for supported pure shape | low | retain helper |
| Active-task cancellation / continuation clarifications | clarification / active_task_cancelled | answered/needs_input | state machine facts | yes unless terminal helper covers | low-medium | no new phase |

## Truth ownership map

- Availability truth -> canonical availability read/booking engine -> AvailabilityTruth -> availability composer -> deterministic fallback.
- Service/package price and device-price truth -> service_catalog/package_offers -> CommercialTruth -> price/device composer -> deterministic fallback.
- Doctor identity -> doctors read / semantic-context verified choices -> DoctorTruth -> deterministic doctor composer.
- Package owned/offer state -> patient package / offer reads -> PackageTruth -> deterministic package composer.
- Patient current profile -> context.patient/current-patient read -> PatientTruth -> deterministic patient composer.
- Appointment upcoming visit information -> appointments read -> AppointmentInfoTruth -> deterministic appointment composer.
- Service details -> service_catalog -> ServiceTruth -> deterministic service composer.
- Clinic information -> clinic_info read -> ClinicTruth -> deterministic clinic composer.
- Pulse balance/owned/offers/overage -> Pulse reads -> PulseTruth -> deterministic Pulse composer.
- Completed writes -> write executor action result -> ActionTruth / terminal contract -> bounded terminal composer -> deterministic fallback.
- Customer history -> workspace+patient scoped history read -> response-safe whitelist -> generic responder.
- General payment policy -> clinic_info + deterministic ownership markers -> generic responder.
- Refund quote -> refund quote service / financial rows -> generic response path; ownership remains outside the Response Contract Program.
- Medical judgment -> no clinical answer owner; deterministic handoff only.

## Overlap / duplicate ownership review

No material duplicate source of truth was found in pure contracts.

- ServiceTruth may expose device names, while CommercialTruth owns device-price bindings. ServiceTruth does not own price.
- PackageTruth owns package state/session counts; CommercialTruth owns package price/currency.
- AppointmentInfoTruth snapshots doctor/service/device names from the appointment read; it does not redefine doctor/service catalog truth.
- PulseTruth owns Pulse-offer/overage commercial values because Phase 3B CommercialTruth does not model Pulse offers/settings.
- Clinic working hours describe clinic operating hours; AvailabilityTruth describes verified bookable appointment options. They are not interchangeable.
- Terminal facts may include service/doctor/device/start values as verified write result context; ActionTruth owns write success and does not replace the informational domain contracts.

## Generic responder reachability

### Safe / acceptable generic use
- general payment-method/timing/policy information after ledger stripping;
- customer-history prose after profile/financial minimization;
- social acknowledgments;
- ordinary handoff wording;
- generic missing-detail clarification without exact verified option sets;
- unsupported low-risk mixed prose where no exact value is business-critical.

### Material mixed exact-value gap
The mixed path uses customer_visible_outcome(), not the typed CustomerResponseContract, and then invokes the free-form responder.

Existing post-hoc protections cover:
- availability semantic state contradiction;
- multi-device priced clarification pairs;
- completion of verified doctor lists.

They do not universally enforce exact values for already-typed units inside mixed responses.

A closeout-only adversarial probe deliberately returned wrong model drafts. All four incorrect drafts passed through:
- verified service price 500.00 EGP -> final mixed reply 900 EGP;
- verified availability 18:00 -> final mixed clinic+availability reply 20:00 while availability_claim stayed options_available;
- verified Pulse balance 1000 and appointment 17:00 -> final mixed reply 700 Pulses and 19:00;
- verified service choices Hydrafacial/PRP -> final reply offered Botox/Filler.

The probe performed zero DB reads and zero writes; it exercised only response composition.

This is a genuine response-layer ownership gap because canonical truth is already stable and the unsafe step is composition.

## Legacy guard inventory

| Guard/helper | Protects | Pure path | Mixed/legacy | Still necessary | Safe to remove |
| --- | --- | --- | --- | --- | --- |
| Availability contract validation/fallback | state, complete windows, exact requested-time semantics | yes | no | yes | no |
| _deterministic_availability_guard_reply | semantic availability-state contradiction after generic composition | no valid pure path | yes | yes | no |
| CommercialTruth / price composer validation | service/device/package amount binding | yes | no | yes | no |
| _deterministic_device_price_guard_reply | multi-device priced clarification pairs | legacy/mixed | yes | yes | no |
| DoctorTruth deterministic renderer | exact doctor set/choice | yes | no | yes | no |
| _ensure_verified_doctor_list | omitted verified doctors in mixed answers | no valid pure path | yes | yes | no |
| _deterministic_compatibility_reply | canonical doctor/device incompatibility | compatibility path | legacy helper | yes | no |
| Package response shaping | strips commercial/financial fields from package-info surface | yes | yes | yes | no |
| Patient/CRM safe shaping | removes profile PII/financial history from customer-history generic payload | profile pure / history generic | yes | yes | no |
| Clinic intent-specific shaping | requested clinic fields, phone-only contact | yes | yes | yes | no |
| Pulse safe shaping + PulseTruth | balance/owned/offer/overage separation and financial filtering | yes | yes | yes | no |
| Medical deterministic handoff | prevents diagnosis/advice generation | yes | n/a | yes | no |
| Outcome internal-ID stripping | UUID/ref/internal-key removal | all | all | yes | no |
| Responder action/payment rules | false write/payment/settlement claims on legacy/mixed | no valid pure terminal path | yes | yes | no |

No guard deletion is recommended in this task.

## Unsupported surfaces classification

### Safe / no action
- social_ack
- ordinary customer_request handoff
- low-risk generic clarification without exact verified choices
- general payment policy (not customer ledger)
- customer-history summary after current whitelist

### Test-only / monitoring
- retain representative stale-history controls and mixed grounding tests;
- keep compatibility/availability/financial-ownership regressions.

### Separate program
- package refund quote/refund lifecycle/payment/settlement/ledger truth -> Separate Financial/Refund Program.
- diagnosis/suitability/contraindication/treatment advice -> Separate Medical Safety Program if current deterministic handoff policy changes.

### Focused Response Contract follow-up candidates
Finding 1: mixed exact-value composition.
Finding 2: remaining verified-choice presentation.

## Finding 1 — mixed exact-value composition

Severity: P2 business correctness.

Affected examples:
- service + price;
- clinic + availability;
- Pulse + appointment;
- other combinations where an already-typed exact fact joins an unsupported unit.

Canonical truth sources:
existing typed owners (CommercialTruth, AvailabilityTruth, PulseTruth, AppointmentInfoTruth, etc.).

Current unsafe path:
verified outcomes -> safe-shaped customer_visible_outcome -> generic responder -> partial post-hoc guards.

Why insufficient:
the generic responder can alter exact numbers, times, and entity/value bindings while satisfying current high-level guards.

Minimal recommended follow-up:
preserve existing typed units when composing mixed replies. Prefer deterministic unit rendering/concatenation when sufficient, or a narrowly bounded symbolic mixed composer that receives references rather than exact values. Do not build a universal truth graph or mega schema. Unsupported generic units can remain generic but must not be allowed to rewrite the resolved typed-unit text.

## Finding 2 — verified choice presentation

Severity: P2 customer-flow/business correctness.

Affected response goals:
ask_service_choice, ordinary ask_device_choice, ask_appointment_choice, ask_package_choice, and any reachable ask_time_choice not owned by AvailabilityTruth. ask_doctor_choice is already DoctorTruth-owned; priced device clarification has CommercialTruth protection.

Canonical truth:
TurnOutcome.choices derived from verified semantic/read/state sources.

Current unsafe path:
verified choices -> refs stripped -> generic responder.

Why insufficient:
TurnOutcome guarantees choices exist, but the final model can omit, rename, reorder, or replace them. No generic complete-set/identity repair exists.

Minimal recommended follow-up:
deterministic choice presentation or a tiny choice contract that renders the exact verified labels once, without exposing refs. Refund-specific ask_package_choice must remain under the Financial/Refund boundary if choice facts carry financial quote amounts.

## Financial / refund boundary

General payment policy is informational and intentionally Agent-owned:
- booking_requires_payment=false;
- payment_execution_owner=reception;
- no customer ledger read;
- active booking state is preserved.

Customer financial actions/status/ledger remain receptionist-owned and normalize to handoff.

Refund quote is different: it depends on package purchase/usage, collected amounts, prior refunds, refundable value, and staff-review fail-closed behavior. Even though its final prose is generic, it belongs to a separate Financial/Refund Program rather than a new Response Contract phase.

No refund/payment/settlement migration is performed here.

## Medical boundary

medical and urgent_medical safety signals preempt normal operation planning and create medical handoff outcomes.

The responder renders them through deterministic:medical-handoff with no second responder model call. No diagnosis, suitability judgment, contraindication decision, or treatment recommendation is directly rendered.

Current medical response ownership is safe for the Response Contract closeout. Any expansion beyond handoff belongs to a separate Medical Safety Program.

## Fallback and LLM-call review

Zero-responder-LLM deterministic families:
- Doctor
- Package information
- Patient profile
- Appointment information
- Service information
- Clinic information
- Pulse information
- Medical handoff
- compatibility helper

Bounded composer families:
- Availability
- Price/Device (only where presentation composition is useful)
- Terminal writes

Each bounded composer resolves verified contract references and has a deterministic fallback from the same contract. Fallback does not perform new business reads, writes, repricing, availability searches, or verifier-model calls.

Generic responder remains reachable for mixed/unsupported families listed above.

Third-model factual verification calls: 0.

## Complexity assessment

The domain-specific contracts remain individually small and understandable, but the responder now has many early exits plus a legacy generic seam.

Additional domain-by-domain informational migrations are reaching diminishing returns.

The remaining value is not another Phase 3K domain contract. It is limited to:
1. preserving already-owned typed truth across mixed composition;
2. exact rendering of verified generic choices.

A universal response framework, truth graph, mega schema, or broad guard cleanup would add more complexity than value and is not recommended.

## Validation

Program-level representative regression set:
- 294 passed.

Coverage included:
- Response Contract core;
- terminal composer and terminal state outcomes;
- Availability and availability guard;
- Price / Device;
- Doctor;
- Package;
- Patient / CRM;
- Appointment Information;
- Service Information;
- Clinic Information;
- Pulse Information;
- generic responder + grounding + responder architecture;
- provider/schema compatibility;
- payment-info/financial ownership;
- refund quote;
- medical safety/handoff.

Agent-eval tooling:
- 39 passed.

Static gates:
- Ruff app/tests/alembic: PASS.
- compileall app/alembic/tests: PASS.
- Alembic single head: 0086_all_service_packages.
- git diff --check: PASS before this docs-only change.

Adversarial response-only probe:
- 4/4 deliberately incorrect generic drafts were accepted by current mixed/choice paths, establishing Findings 1 and 2.
- zero DB reads;
- zero writes.

## Program-level counters in representative reviewed coverage

Baseline regression/live evidence:
- cross_patient_response_facts = 0
- cross_tenant_response_facts = 0
- financial_field_leaks = 0
- medical_field_leaks = 0
- internal_id_leaks = 0
- secret_leaks = 0
- false_write_acknowledgments = 0
- false_payment_claims = 0
- false_settlement_claims = 0
- additional_verifier_llm_calls = 0

Adversarial closeout findings:
- wrong_exact_numeric_facts = non-zero (mixed responder can pass deliberately wrong price/Pulse values)
- wrong_entity_bindings / exact time bindings = non-zero (mixed responder can pass deliberately wrong availability/appointment time)
- invented_verified_entities = non-zero in verified generic choice presentation (deliberately invented choices can pass)

Because material counters are non-zero under a reachable response path, the program cannot be declared closed.

## Production / deployment rule

This closeout task changes documentation only and requires no deployment.

Vercel automatic deployments must remain disabled.

No runtime migration, Financial/Refund migration, Medical migration, guard cleanup, prompt cleanup, or generic framework is started by this task.
