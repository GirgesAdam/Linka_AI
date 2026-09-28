# Patient / CRM Response Contract Audit — 2026-09-28

## Starting baseline

Starting main SHA:
a3bd3fda895446369b66179fb05bf3c7e9e30873

Closed prior phases:
- Phase 3A Availability
- Phase 3B Price / Device
- Phase 3C Doctor / Provider
- Phase 3D Package Information

## Audit decision

Migration justified: YES

Chosen path:
B — Focused Patient / CRM contract migration

Reason:
The runtime already owns current-patient selection and tenant/patient isolation deterministically, but pure customer-profile responses still pass exact identity/contact facts through the generic free-form responder with no patient-specific factual guard. In addition, customer-history outcomes currently carry unnecessary nested profile PII into the response layer.

## Actual Patient / CRM Response Families

### Family: current customer profile

Operation:
customer_profile

Planner:
- disposition=read
- read=customer_profile
- response_goal=answer_customer_profile

Read source:
_read_customer_profile()

Identity scope:
The read never searches by name or model-authored identity. It reads context.patient only.

Verified backend facts currently emitted by the read:
- first_name
- last_name
- phone
- preferred_language
- status

Current responder behavior:
Generic free-form responder.

Existing deterministic protections:
- context.patient is established before orchestration
- internal IDs are recursively filtered
- no patient lookup/search is delegated to the LLM

Existing guards:
No patient-specific post-hoc identity/contact guard exists.

Material failure modes:
- model can rewrite or swap first/last name
- model can alter phone digits
- internal CRM status can be surfaced as though it were ordinary customer profile data
- no deterministic guarantee that only exact backend values are rendered

Migration justified:
YES

Focused Phase 3E customer-safe profile ownership:
- first_name
- last_name
- phone, when present
- preferred_language

Excluded from PatientTruth:
- patient UUID
- workspace ID
- normalized phone
- CRM status
- source/source_detail
- marketing flags
- WhatsApp opt-in metadata
- preferred branch internal binding
- timestamps unless a later explicit customer-safe use case requires them

### Family: customer history

Operation:
customer_history

Planner:
- disposition=read
- read=customer_history
- response_goal=answer_customer_history

Read source:
build_patient_history_context(
    workspace_id=current workspace,
    patient=context.patient,
)

Identity scope:
All appointment/payment/service history queries constrain both workspace_id and patient.id.

Internal DTO includes:
- profile identity/contact/demographic fields
- appointment counts
- service history
- recent appointment lifecycle facts
- money totals
- appointment prices/payment status/payment method/billing context

Current responder behavior:
Generic free-form responder.

Privacy finding:
The current generic visibility filter removes internal IDs but does not remove nested profile phone, gender, birth_date, source timestamps, or financial/history fields. Live validation also showed that a non-financial question about the last visit could cause the generic responder to volunteer a payment amount.

Phase 3E boundary:
customer_history is not migrated into PatientTruth. Appointment lifecycle remains owned by the existing appointment/history path, and money-ledger concerns remain receptionist-owned through financial_ownership=reception.

Minimal required hardening:
Use an answer_customer_history response whitelist that removes:
- nested profile PII
- money totals
- historical price/net-paid values
- payment status/method/billing fields
- package-usage financial context

Keep customer-safe historical lifecycle facts such as service, doctor, branch, dates/times, status, and appointment/visit counts.

### Patient search / ambiguity

V2 has no patient-search read kind and does not expose a candidate-patient list to the interpreter or responder.

Same-name behavior:
Not applicable to the customer agent runtime. Names are not used to select the patient.

Unknown patient behavior:
The live API path resolves Patient by (workspace_id, patient_id) and fails if not found.

### WhatsApp / channel identity

Existing identity:
ChannelIdentity is resolved by workspace_id + channel_connection_id + external_user_id, then its patient is fetched with the same workspace.

New identity:
Phone is normalized server-side. Existing patient reuse is by workspace_id + phone_normalized. The model does not choose a patient.

Conversation binding:
Existing conversations reject a different patient_id.
Existing inbound V2 processing rejects:
- another workspace
- another conversation
- conversation.patient_id != patient.id
- non-patient/non-inbound messages

Display contact:
customer_profile uses patient.phone, not phone_normalized, so the internal normalized value is not customer-visible.

### Cross-patient / cross-tenant boundary

Current-patient profile:
No DB search; context.patient only.

History:
workspace_id + patient.id.

Appointments/packages/pulse:
existing domain reads already scope to the current patient and remain owned by their existing response/business domains.

## Focused migration direction

Pure answer_customer_profile:
interpreter requested_patient_details
→ planner-scoped customer_profile read
→ verified profile fields only
→ CustomerResponseContract
→ backend-owned PatientTruth
→ deterministic exact rendering

Field-level disclosure:
- name
- phone
- preferred_language

A broad profile/details request uses all three safe fields.
A narrow request reads and renders only the requested safe field(s).

No Patient LLM composer is required.

Mixed responses:
Remain entirely on the existing legacy responder.

History privacy shaping:
answer_customer_history response facts use a non-financial customer-safe whitelist before generic composition. Nested profile PII and financial/payment/package-usage fields are removed.

No financial or appointment ownership is transferred to PatientTruth.

## Minimal runtime changes

Interpreter:
One typed requested_patient_details field is added for customer_profile field-level privacy minimization.

Planner:
Passes only that requested profile scope into the existing customer_profile read.

Read executor:
Returns only requested customer-safe profile fields and no longer emits internal CRM status.

## Non-changes

Patient selection: unchanged
Channel identity resolution: unchanged
Appointment lifecycle: unchanged
Availability/Price/Doctor/Package contracts: unchanged
Financial ownership: unchanged
Patient writes/CRM mutation: unchanged
Auth/permissions: unchanged

No existing guard is deleted.
No verifier model is added.
additional_llm_calls = 0


## Deterministic validation evidence

Focused Patient/CRM contract, planner, interpreter, read, outcome, responder:
- Ruff PASS
- 158 passed

Identity/channel/history isolation regressions:
- 64 passed

Full V2 regression sweep before final history-minimization patch:
- 651 passed

Package/financial business regression set:
- 47 passed

Agent-eval tooling:
- Ruff PASS
- compileall PASS
- 39 passed

Static backend gates:
- Ruff PASS
- compileall PASS
- Alembic single head: 0086_all_service_packages


## Bounded live validation

Final successful one-off staging review:
- workflow: Patient CRM Contract Live Review
- run: 36483022185
- branch commit: 9803902da1c9caa042e5c60e5d37b7eb0ca7a39d

Scenarios:
1. broad current profile
2. name-only after stale wrong assistant name
3. phone-only with exact display formatting
4. preferred-language-only
5. requested phone absent
6. customer history with injected nested profile PII and financial fields
7. mixed phone-profile + service-information response

Pure profile scenarios used:
deterministic:patient-contract

Customer-history and mixed unsupported response sets remained on the existing legacy responder.


Live finding and correction:
The first successful history read still allowed the generic responder to volunteer a 500 EGP payment amount for a non-financial last-visit question. This exposed an unnecessary financial response surface.

The focused correction added a non-financial customer-history response whitelist. The final live reply became:
"آخر زيارة كانت ليزر إبط مع د. مريم بتاريخ 29 أغسطس 2026."

No payment amount, payment status/method, billing field, nested profile PII, or package-usage financial context remained.

Manual review:
- NATURAL: 6
- ACCEPTABLE: 1
- ROBOTIC/MATERIAL: 0

The mixed phone + service reply is ACCEPTABLE: it preserves the exact verified phone and declines to invent unavailable service-list detail.


Material counters after the final live review:
- invented_patients = 0
- wrong_patient_identity = 0
- cross_patient_reads = 0
- cross_patient_response_facts = 0
- cross_tenant_reads = 0
- wrong_phone_claims = 0
- invented_contact_details = 0
- internal_id_leaks = 0
- internal_crm_field_leaks = 0
- medical_field_leaks = 0
- financial_field_leaks = 0
- wrong_appointment_patient_binding = 0
- omitted_required_patient_candidates = 0 (not applicable: V2 exposes no patient candidate search)
- duplicate_patient_candidates = 0 (not applicable: V2 exposes no patient candidate search)
- patient_mutation_writes = 0
- financial_writes = 0
- additional_llm_calls = 0

The live fixture runtime performed zero database reads and zero writes. Production isolation is established separately by deterministic channel/live-chat/read regression coverage.
