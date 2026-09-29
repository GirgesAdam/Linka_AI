# Phase 3I — Clinic Information Response Contract Audit

Date: 2026-09-29

Starting main SHA: `355b8cfecede9469e3a05ed130f9c3642268fc9c`

## Decision

Path B — Focused Clinic Information contract migration is materially justified.

The current V2 product is customer-facing single-location. There is no branch entity in TurnEntities and no customer branch-selection flow. The clinic-info read resolves only the workspace primary branch (or the deterministic single visible branch fallback). Phase 3I therefore must not invent a multi-branch contract.

## Actual Clinic / Branch Information Families

### Family: clinic_info

status:
`answered`

response_goal:
`answer_clinic_info`

Read source:
`ReadRequest(kind="clinic_info") -> _read_clinic_info()`

Verified backend facts:
- workspace name
- workspace timezone
- active branch rows from the canonical clinic catalog
- primary/single customer location identity
- branch contact/address fields when present in the catalog
- canonical BranchWorkingHour rows already loaded in the catalog
- saved clinic-wide explanatory knowledge from `relevant_knowledge_context(... include_clinic=True)`

Customer-safe facts:
- clinic name
- primary location name
- display address components
- customer-facing phone/email
- branch timezone
- structured weekly working hours
- saved clinic-authored general information/policy text

Internal/configuration facts:
- workspace/branch IDs
- branch code
- provider/integration IDs
- WhatsApp phone_number_id
- credentials/tokens/secrets
- database/config blobs

Current responder behavior:
- `answer_clinic_info` remains on the generic free-form responder.
- exact phone/address/name/policy wording is model-authored.
- generic prompt says TURN_OUTCOMES are authoritative and forbids inventing clinic facts, but there is no deterministic clinic factual validation or exact-value resolution.
- responder receives current local time, so a free-form model can potentially turn weekly-hours prose into an unsupported open/closed claim.

Existing deterministic protections:
- native catalog includes active branches only.
- read path scopes to the workspace catalog.
- primary branch is selected when configured and output is capped to one customer location.
- `_read_clinic_info` uses an explicit location field whitelist rather than exposing raw workspace/branch objects.
- generic outcome shaping strips ID-suffixed fields.
- clinic explanatory text comes from saved clinic knowledge, not general model knowledge.
- payment_info shares `answer_clinic_info` but carries explicit payment ownership markers; Phase 3I must not claim that family.

Existing guards:
- no clinic-specific post-hoc factual guard.
- no exact phone/address validator.
- no working-hours/open-now validator.

Material failure modes:
1. Generic responder can alter exact phone/address/clinic identity even though verified facts exist.
2. Stale conversation wording can compete with current clinic facts.
3. Saved policy/knowledge can be paraphrased into unsupported policy meaning.
4. Canonical BranchWorkingHour rows exist but are currently omitted from `_read_clinic_info`; working-hours questions therefore do not receive the canonical structured schedule.
5. Native catalog currently omits phone/email/full address components from branch projection even though the Branch row is already loaded.
6. Open/closed state is not backed by a holiday/closure truth source. Phase 3I must not turn weekly hours into an unconditional real-time open claim.

Migration justified:
YES

Reason:
Clinic/contact/hours facts are exact operational truth. They are currently safe on input but still model-owned on output, and current native projection drops some already-loaded customer-facing contact facts. A small deterministic ClinicTruth can close this without new reads, availability logic, or a generic framework.

## Scope of the focused migration

- Add clinic-info-only requested detail semantics.
- Expand the already-loaded native branch catalog projection with customer-facing contact/address/timezone fields.
- Include canonical working-hours rows in the clinic_info read.
- Shape clinic_info facts to the exact requested customer-safe categories.
- Build a small ClinicTruth for pure clinic_info only.
- Render pure clinic information deterministically.
- Keep payment_info and mixed unsupported response sets on the existing generic responder after safe shaping.
- For open-now questions, do not claim live open/closed truth from weekly hours alone; return the verified schedule/limitation unless a canonical real-time closure source exists.

No new business reads, writes, availability calculation, pricing logic, doctor scheduling, or third-model verification.
