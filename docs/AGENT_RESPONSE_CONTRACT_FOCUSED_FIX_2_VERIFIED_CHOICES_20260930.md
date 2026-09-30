# Linka Response Contract Focused Fix 2 — Verified Choice Presentation Hardening

Date: 2026-09-30

Starting main SHA:
ca18c82c53596eebd5fa1b32bec5ee18c48395d7

Reconciled latest main before commit:
31eb0fc9946d81b6f611ec5f60520fd217e72a42

## Scope

This task changes only customer-facing presentation of already-verified choices.

It does not change planner selection, semantic resolution, active-task targeting, appointment targeting, booking rules, compatibility, business reads/writes, financial/refund ownership, or medical handling.

Architecture:

verified TurnOutcome.choices
→ existing customer-safe CustomerResponseContract projection
→ deterministic choice renderer
→ customer reply

No verifier model and no new truth graph/schema are introduced.

## Actual choice-family audit

### Service choice

Family:
ask_service_choice

Operation:
Any operation with a semantically ambiguous service where the planner requires one service identity.

Status:
needs_input

Verified choice source:
TurnOperation entity candidate_refs resolved against SemanticContext.

Choice fields:
server-only ref + customer-visible label; response projection strips the ref and internal facts.

Customer-visible label:
canonical service name from semantic context.

Complete set required:
YES — the clarification presents the current verified candidate set.

Generic responder reachable before fix:
YES.

Already protected before fix:
NO.

Financial/lifecycle coupling:
NO.

### Doctor choice

Family:
ask_doctor_choice

Operation:
Doctor ambiguity in supported read/write flows.

Status:
needs_input

Verified choice source:
semantic-context verified doctor candidates.

Choice fields:
doctor identity, with optional visible specialization in DoctorTruth.

Customer-visible label:
verified doctor name / visible specialization.

Complete set required:
YES.

Generic responder reachable before fix:
NO for the supported path.

Already protected before fix:
YES — DoctorTruth + doctor composer.

Financial/lifecycle coupling:
NO.

Action:
No duplicate implementation. Existing owner remains unchanged.

### Ordinary device choice

Family:
reachable runtime shape is clarification + needed=device + TurnOutcome.choices.
The ask_device_choice vocabulary exists, but no direct planner producer was found on the current baseline.

Operation:
Device-sensitive operations such as doctor lookup, availability/booking/package/Pulse/refund/reschedule when a semantic device entity is ambiguous.

Status:
needs_input

Verified choice source:
device candidate_refs resolved against SemanticContext.

Choice fields:
server-only device ref + customer-visible device label.

Customer-visible label:
verified device name.

Complete set required:
YES for a choice-bearing ambiguity.

Generic responder reachable before fix:
YES.

Already protected before fix:
NO for ordinary non-priced device ambiguity.

Financial/lifecycle coupling:
NO for the ordinary device ambiguity hardened here.

Exception:
priced device clarification remains CommercialTruth-owned and is excluded from this renderer.

### Appointment choice

Family:
ask_appointment_choice

Operation:
cancel_appointment, confirm_appointment, reschedule, and verified appointment ambiguity after reads/verification.

Status:
needs_input

Verified choice source:
verified appointment reads or semantic appointment candidates.

Choice fields:
server-only ref/appointment_id plus a customer-visible label.

Customer-visible label:
current builder binds service name + doctor name + start_local where available.

Complete set required:
YES — the current verified appointment candidates must be displayed exactly once.

Generic responder reachable before fix:
YES.

Already protected before fix:
NO.

Financial/lifecycle coupling:
Lifecycle targeting exists, but this task changes display only. Selection/target resolution remains unchanged.

### Ordinary package choice

Family:
ask_package_choice

Operation:
non-financial package ambiguity, including package selection for operational package flows.

Status:
needs_input

Verified choice source:
semantic package candidate_refs or verified package reads.

Choice fields:
server-only ref + customer-visible package label; response-safe facts may exist but the hardened renderer displays only the label.

Customer-visible label:
verified package name.

Complete set required:
YES for the current clarification candidate set.

Generic responder reachable before fix:
YES.

Already protected before fix:
NO.

Financial/lifecycle coupling:
Sometimes.

Boundary:
refund-specific ask_package_choice is explicitly excluded when the unit carries available_quote_count or refund-specific choice facts. That path remains inside the Financial / Refund Program.

### Time choice

Family:
ask_time_choice

Operation:
No direct current planner producer found on this baseline. Availability is normally owned by AvailabilityTruth / availability snapshots instead.

Status:
needs_input when represented.

Verified choice source:
TurnOutcome.choices if such a unit is produced.

Customer-visible label:
exact verified time label.

Complete set required:
YES when used as a canonical choice set.

Generic responder reachable before fix:
Structurally yes, but no direct current producer was found.

Already protected before fix:
NO as a generic choice goal; AvailabilityTruth separately protects its own availability presentation.

Financial/lifecycle coupling:
NO.

Action:
The deterministic renderer supports ask_time_choice without changing availability logic or creating a new producer.

## Customer-safe projection

The response contract already strips:
- ref / *_ref / *_refs;
- internal IDs such as appointment_id, service_id, doctor_id, package_id, workspace_id;
- other internal reference metadata.

Focused Fix 2 renders only ResponseChoice.label.

It does not render raw choice facts.

## Duplicate visible labels

Labels are compared after whitespace normalization and case folding.

If two choices are customer-indistinguishable by the verified visible labels, the renderer does not:
- pick one;
- expose an internal ref/ID;
- invent a suffix.

It fails safely with a deterministic request for additional clinic-visible distinction.

No selection semantics are modified.

## Complete-set / identity guarantees

For supported verified-choice units:
- every verified label is rendered exactly once;
- ordering is backend order;
- no model can invent a label;
- no model can substitute a label;
- no model can omit a required label;
- no model can duplicate a label;
- choice units remain rendered independently in compound responses.

## Existing exceptions preserved

- ask_doctor_choice → DoctorTruth / doctor composer.
- priced device clarification → CommercialTruth / Price-Device composer.
- refund-specific ask_package_choice → Financial / Refund Program boundary.

## Cost / runtime impact

For hardened choice presentation:
- additional_business_reads = 0
- additional_writes = 0
- additional_verifier_llm_calls = 0
- responder LLM calls for supported choice presentation = 0

## Choice counters

Target:
- invented_choice_labels = 0
- omitted_required_choices = 0
- duplicate_choice_labels = 0 in final customer presentation
- wrong_choice_identity = 0
- cross_unit_choices = 0
- wrong_service_choices = 0
- wrong_device_choices = 0
- wrong_appointment_choices = 0
- wrong_package_choices = 0
- wrong_time_choices = 0
- internal_choice_id_leaks = 0
- additional_business_reads = 0
- additional_writes = 0
- additional_verifier_llm_calls = 0

## Focused validation

Initial focused choice/outcome/domain suite:
- 131 passed.

Program-level representative regression:
- 337 passed.

Agent-eval tooling:
- 39 passed.

Static:
- Ruff PASS.
- compileall PASS.
- Alembic single head: 0086_all_service_packages.

Shared CI, live, merge, deploy, and production evidence are recorded during closeout.

## STOP rule

This task does not start:
- Financial / Refund Program;
- Medical Safety Program;
- generic responder cleanup;
- guard removal;
- prompt rewrite;
- model migration.
