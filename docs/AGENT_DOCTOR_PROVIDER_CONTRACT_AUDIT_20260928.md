# Doctor / Provider Response Contract Audit + Conditional Migration — 2026-09-28

## Starting baseline

Starting main SHA:

cc50bc1165f04d14115f8b6281f4af3e9a881468

Phase 3A Availability: CLOSED  
Phase 3B Price / Device: CLOSED

## Audit decision

Migration justified: YES

Chosen path:

B — Focused Doctor/Provider contract migration

The backend already owns most doctor truth correctly and pure doctor-list
responses were already deterministic. The material gap was narrower:

- ask_doctor_choice still used the generic free-form responder even though
  the backend already owned the exact canonical choice set;
- mixed answer_doctor responses used a post-hoc completeness helper that
  prevented omission but did not structurally own doctor identity/ranking.

Phase 3C therefore migrates only pure answer_doctor and ask_doctor_choice.
It does not create a generic provider framework and it does not duplicate
availability, compatibility, pricing, booking, or terminal truth.

## Actual Doctor / Provider Response Families

### 1. Doctor discovery / information

Interpreter capability:
doctor_discovery

Planner operation:
doctor_info

Planner:
- disposition=read
- read kind=doctors
- response_goal=answer_doctor

Runtime outcome:
- status=answered
- response_goal=answer_doctor

_read_doctors() filters the canonical clinic catalog by:
- doctor_id;
- doctor_ids;
- service_id through doctor.service_ids.

Actual doctor catalog records can contain:
- id
- name
- specialization
- service_ids
- branch_ids
- working_hours

Customer-visible outcome shaping strips IDs and *_ids.

Identity-critical:
- name

Compatibility-critical:
- membership in the already filtered verified read result.

Customer-safe DoctorTruth fields:
- name
- specialization, when verified

working_hours is intentionally not imported into DoctorTruth because a
provider working schedule is not a verified bookable appointment slot.

Before Phase 3C:
pure answer_doctor was already rendered deterministically by
_deterministic_pure_doctor_list_reply(), using names only.

### 2. Ambiguous doctor selection

When a doctor reference is ambiguous outside the availability special case,
the planner emits a clarification with:

- status=needs_input
- response_goal=ask_doctor_choice
- clarification_field=doctor

Outcome builder creates choices only from semantic candidate refs that
resolve in canonical SemanticContext.

TurnOutcome validation rejects choice goals with no choices.

Before Phase 3C this exact verified option set still went to the generic
free-form responder.

This is a material identity surface, so Phase 3C migrates it.

### 3. Doctor/service incompatibility

Known incompatibility comes from canonical booking/availability compatibility.

Verified payload:
- dimension=doctor
- service_name
- requested_name
- compatible_options

Unknown doctor identities are explicitly not converted into known
incompatibility.

Responder:
_deterministic_compatibility_reply()

Migration:
NOT REQUIRED

This path is already deterministic and backend-owned.

### 4. Doctor-specific availability

Owned by Phase 3A:
- present_availability
- requested_time_unavailable
- no_availability

DoctorTruth is not created for these outcomes.

doctor exists != doctor supports service  
doctor supports service != doctor has a slot  
doctor has a slot != doctor is recommended

### 5. Doctor availability comparison

A doctor set plus next/earliest availability is normalized to an
informational availability operation.

The planner sends verified doctor_ids to the canonical availability read and
does not create a booking write.

Rendering remains Phase 3A Availability.

No doctor ranking truth is introduced.

### 6. Booking continuation / terminal action

Doctor selection continues through verified option snapshots and existing
planner/read/write state.

Completed booking/reschedule doctor_name remains owned by the Terminal
contract.

No DoctorTruth is created for terminal outcomes.

## Recommendation / best-doctor audit

The V2 runtime has no deterministic ranking score or policy saying one doctor
is best, better, recommended, or clinically preferable.

Therefore DoctorTruth contains no recommendation field.

For a question such as "مين أحسن دكتور؟" the pure Doctor contract can show
the verified matching doctors and their verified specialization, but it
cannot create a ranking.

## Medical / clinical claims

Phase 3C may expose only an already verified catalog specialization.

It does not introduce:
- qualifications;
- degrees;
- years of experience;
- success rates;
- safety claims;
- clinical recommendation;
- "best for this condition" claims.

No database fields were added.

## Focused DoctorTruth

Kinds:
- doctor_result_set
- doctor_choice

DoctorOption:
- name
- specialization (optional)

Both use complete_set=true because each migrated unit represents the complete
verified result set for that scoped query.

An explicit doctor query can still yield a one-option complete result.

## Rendering architecture

Chosen:

verified doctor outcome
-> CustomerResponseContract
-> backend DoctorTruth
-> deterministic exact doctor rendering

No Doctor LLM composer is added.

Reason:
pure doctor discovery was already zero-LLM. Adding a model call only for
style would add cost/latency without factual benefit.

additional_llm_calls = 0

## Duplicate visible identity

Internal doctor IDs never cross the response boundary.

If two verified doctors have the same visible name and specialization, the
deterministic renderer fails closed and asks for an additional clinic-visible
distinction.

It does not:
- expose an ID;
- invent a suffix;
- silently collapse the two records.

## Mixed responses

answer_doctor + unsupported family remains entirely on the legacy responder.

No half-new / half-legacy response is introduced.

The mixed legacy system prompt receives one focused doctor rule:
- use only doctor names/specializations supplied by TURN_OUTCOMES;
- never invent qualifications, specialty, experience, ranking, or
  best/better/recommended claims;
- never derive availability from doctor existence/compatibility.

The existing doctor completion helper remains reachable for mixed responses.

It was tightened so it appends only missing verified doctor names rather than
repeating the whole canonical list.

## Existing protections

### _deterministic_pure_doctor_list_reply

Purpose:
legacy pure doctor-list deterministic rendering.

After Phase 3C valid pure DoctorTruth units exit earlier through the new
deterministic Doctor contract.

Removed:
NO

### _ensure_verified_doctor_list

Purpose:
mixed legacy completeness protection.

Pure migrated path:
NO

Mixed/legacy path:
YES

Still production-reachable:
YES

Removed:
NO

### _deterministic_compatibility_reply

Purpose:
known doctor/device incompatibility correction from canonical backend truth.

Still production-reachable:
YES

Moved into Doctor contract:
NO

## Non-changes

Interpreter: UNCHANGED  
Planner: UNCHANGED  
Read executor: UNCHANGED  
Outcome builder: UNCHANGED  
Booking engine: UNCHANGED  
Availability engine/composer: UNCHANGED  
Price/Device composer: UNCHANGED  
Terminal composer: UNCHANGED  
Compatibility rules: UNCHANGED  
Financial/Pulse ownership: UNCHANGED  
Appointment lifecycle: UNCHANGED


## Bounded live validation

One-off staging validation ran on workflow:
Doctor Provider Contract Live Review

Run:
36469432481

Scenarios:
1. explicit doctor with verified specialization
2. complete doctor list
3. doctor ambiguity choice
4. no matching doctor
5. best-doctor question with no ranking truth
6. known doctor/service incompatibility
7. mixed doctor + service legacy response

Observed response sources:
- deterministic:doctor-contract — 5
- deterministic:compatibility — 1
- openai:gpt-5.6-luna mixed legacy responder — 1

No response-layer scenario performed a database read or business write.

Manual review:
- NATURAL: 5
- ACCEPTABLE: 2
- ROBOTIC/MATERIAL: 0

The best-doctor response is ACCEPTABLE because it safely presents verified
options/specializations without inventing a ranking.

The mixed legacy response is ACCEPTABLE. It used the phrase "الدكاترة
المتاحين" generically, but made no claim about a verified appointment date,
time, slot, or bookability. No false appointment-availability claim was made.

Material counters:
- invented_doctors = 0
- wrong_doctor_identity = 0
- wrong_doctor_service_bindings = 0
- omitted_required_doctors = 0
- duplicate_doctors = 0
- invented_qualifications = 0
- invented_specialties = 0
- unsupported_best_doctor_claims = 0
- incorrect_doctor_availability_claims = 0
- cross_unit_doctor_refs = 0
- business_writes = 0
- additional_llm_calls = 0

The only live response-model call was the already-existing generic responder
for the deliberately unsupported mixed response set. No validation/verifier
model call was added.
