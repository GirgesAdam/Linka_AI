# Tia AI — Deterministic Response Guard Audit

Date: 2026-09-28
Starting main SHA: `207f84ab9c6cd89c81624201a7fca2c4f73dda52`
Branch: `agent/deterministic-response-guard-audit`
Scope: Agent V2 customer-visible response mechanisms only. No model/provider/API calls were used during Phase 1 discovery or deterministic validation.

## Executive summary

The V2 deterministic response layer is not broadly overengineered. Most mechanisms are narrowly tied to explicit `TurnOutcome.status` / `response_goal` semantics and protect useful invariants: medical escalation, compatibility correction, canonical price/device pairs, complete doctor lists, and ownership after handoff.

One material design defect was confirmed: the availability claim guard inferred customer-facing response intent from the **presence of availability facts** instead of from the **outcome response semantics**. That allowed availability verification evidence retained by `booking_completed` / `reschedule_completed` to override the correct terminal acknowledgment. The same predicate also produced deterministic false positives for constructed non-availability outcomes carrying availability evidence. The fix narrows the guard to explicit availability-facing response goals while preserving the availability truth checks and deterministic fallback.

Post-fix deterministic assessment:

- Necessary protections remain.
- No large responder redesign is justified.
- Availability guard needed targeted narrowing and was fixed.
- No other reproducible material deterministic response failure was found in Phase 1.
- Two terminal-acknowledgment semantic scenarios are candidates for limited live validation because final natural-language behavior is model-dependent even though guard activation is now deterministic and correct.

## Actual V2 response path and precedence

Production V2 uses:

```text
planner / reads / writes
→ TurnOutcome construction
→ customer-visible outcome shaping
→ compose_v2_customer_reply
   1. deterministic medical handoff short-circuit
   2. deterministic compatibility short-circuit
   3. deterministic pure doctor-list short-circuit
   4. deterministic pure price short-circuit
   5. primary responder model / provider fallback model
   6. availability semantic-claim guard
      → if availability fallback is emitted, device-price completeness guard can replace it
   7. device-price completeness guard
   8. verified doctor-list completeness append
→ live_chat ownership / active-handoff suppression
→ same-turn unclaimed AI handoff acknowledgment exception
→ outbound persistence
```

Important boundary: legacy deterministic responses in `backend/app/services/agent_chat.py` are not part of the active V2 reply-composition path when `agent_v2_live_enabled` is on. V2 `live_chat.py` imports legacy storage/history helpers but persists `V2OrchestratedTurn.reply` directly. The legacy `sanitize_customer_reply` path is therefore not counted as an active V2 response override.

## Deterministic response inventory

| Mechanism | File / function | Trigger | Facts inspected | Protection / purpose | Can replace model output? | Can run after write? | Terminal risk | Assessment |
|---|---|---|---|---|---|---|---|---|
| Medical handoff renderer | `agents/v2/responder.py::_deterministic_medical_handoff_reply` | `status=handoff`, `response_goal=handoff`, `category=medical` | category, priority | Urgent/non-urgent medical escalation without generative drift | Short-circuits before model | Planner medical safety is terminal before writes | Low | REQUIRED / WELL-SCOPED |
| Compatibility renderer | `::_deterministic_compatibility_reply` | exactly one `needs_input` outcome containing `compatibility_failure` doctor/device | dimension, requested name, service, compatible options | Prevents incompatible doctor/device from being presented as valid | Short-circuits before model | No | Low | REQUIRED / WELL-SCOPED |
| Pure doctor-list renderer | `::_deterministic_pure_doctor_list_reply` | all outcomes are `answered/answer_doctor` and verified names exist | verified doctor rows | Completeness and canonical names | Short-circuits before model | No | Low | REQUIRED / WELL-SCOPED |
| Pure price renderer | `::_deterministic_pure_price_reply` | exactly one `answered/answer_price` outcome with service catalog | service/device price and requested duration | Prevents price/device invention and silent device selection | Short-circuits before model | No | Low | REQUIRED / WELL-SCOPED |
| Availability claim validator | `::_verified_availability_claim` | **post-fix:** only `present_availability`, `requested_time_unavailable`, `no_availability` outcomes | availability count/windows + explicit response goal | Prevents model from contradicting verified schedule truth | Yes, through availability fallback | Availability evidence may remain after write, but terminal goals no longer activate it | Was High | REQUIRED / NEEDS NARROWING → FIXED |
| Availability fallback renderer | `::_deterministic_availability_guard_reply` | availability claim mismatch on an availability-facing outcome | response-semantic availability payloads | Replaces unsafe availability claim with verified deterministic text | Yes | No terminal activation after fix | Low | REQUIRED / WELL-SCOPED after fix |
| Device/price completeness guard | `::_deterministic_device_price_guard_reply` | `needs_input/clarification`, `needed=device`, 2+ canonical device-price pairs, one or more missing from reply | laser device names/prices | Prevents unbound/transliterated/missing device-price options | Yes | No | Low | REQUIRED / WELL-SCOPED |
| Doctor completeness append | `::_ensure_verified_doctor_list` | current outcomes contain 2+ `answered/answer_doctor` names and model omitted any | doctor names | Prevents partial list when full list was requested/provided | Appends to model output | No | Low | REQUIRED / WELL-SCOPED |
| Empty responder fail-closed | `compose_v2_customer_reply` | structured responder returns empty text | no business facts | Prevents persisting blank customer reply | Suppresses output by raising | Could occur after in-transaction write; write is not independently committed by V2 executor | Low | REQUIRED / WELL-SCOPED |
| Model failover | `compose_v2_customer_reply` model chain | primary provider failure / configured distinct fallback | provider result | Availability/reliability, not deterministic content | Re-runs model, not a deterministic reply | Yes within turn | Low | NOT A DETERMINISTIC RESPONSE GUARD |
| Outcome ID/internal-data shaping | `services/agent_v2/outcome_builder.py::_visible_dict`, `_visible_value`, `customer_visible_outcome` | every responder payload | `_id`, `_ids`, internal keys, billing context, money fields | Keeps internal identifiers/metadata out of responder context | Shapes facts before model | Yes | Low | REQUIRED / WELL-SCOPED |
| Terminal status/action contract | `services/agent_v2/outcome.py::TurnOutcome.validate_shape` | every `TurnOutcome` construction | status, response goal, action result | Prevents terminal goal/status mismatch; stamps successful completed action metadata | No direct prose | Yes | Low | REQUIRED / WELL-SCOPED |
| Domain result → response goal mapping | `outcome_builder.py::build_step_outcome` | verified read/write result | disposition, `ok`, `requires_human`, availability count, write kind | Ensures writes cannot be called completed before execution and maps failures/handoffs deterministically | Defines responder contract | Yes | Low | REQUIRED / WELL-SCOPED |
| Completed booking/reschedule fact shaping | `outcome_builder.py::_facts_for_completed_write` | successful booking/reschedule | availability windows | Removes end time after exact write to prevent session-duration inference | Shapes terminal facts | Yes | Safe; exposed old availability bug only because guard was broad | REQUIRED / WELL-SCOPED |
| Human ownership suppression | `services/agent_v2/live_chat.py::_run_v2_after_inbound` | conversation not AI-owned or active handoff exists | ownership + active handoff | Prevents AI/staff double replies | Suppresses entire model/deterministic reply | Yes, concurrency-safe final gate | Low | REQUIRED / WELL-SCOPED |
| Same-turn handoff ack exception | `live_chat.py::_v2_handoff_ack_allowed` | handoff created this turn, source AI, pending, unassigned | handoff source/status/assignee | Allows one handoff acknowledgment before AI is paused | Allows otherwise-suppressed reply | Handoff only | Low | REQUIRED / WELL-SCOPED |
| Availability range renderer | `agents/availability_presentation.py::format_availability_windows_reply` | called by availability fallback | verified start/end windows or slots | Pure deterministic presentation of verified ranges | Used by guard fallback | Not after terminal after fix | Low | PURE PRESENTATION FALLBACK |

## What is *not* a V2 deterministic override

The following areas were explicitly checked because they were requested in the audit, but they do not contain an additional V2 post-responder override layer:

- Appointment reads: represented as structured V2 outcomes and verbalized by the responder.
- Package/Pulse informational replies: structured V2 outcomes; no generic post-LLM package/Pulse override in the V2 path.
- Payment information: `payment_info` currently maps to grounded clinic-information semantics; no deterministic payment-info prose override.
- Financial writes/settlement: not authorized in V2; receptionist ownership becomes structured handoff before response composition.
- Cancellation/package purchase/Pulse-pack purchase/follow-up/marketing successful acknowledgments: terminal `completed` outcomes, no generic availability-style post-LLM override.
- Legacy `format_verified_tool_fallback`, V1 package deterministic responses, V1 cancellation fallback, and V1 `sanitize_customer_reply`: present in the repository but not on the active V2 live reply path.

## Root cause of the production bug

Before the fix:

```text
_verified_availability_claim(outcomes)
  → collect every facts["availability"] payload
  → any positive count/window => options_available
```

This used:

```text
availability facts exist
```

as a proxy for:

```text
the current customer-facing response must present availability
```

That proxy is invalid. Availability facts may be verification evidence retained by a terminal write. For booking/reschedule completion, `_facts_for_completed_write` intentionally strips end times but retains verified start/context facts. The guard therefore classified a completed booking as `options_available`, disagreed with the natural terminal acknowledgment (`availability_claim=not_applicable`), attempted to render a range from start-only evidence, and emitted the generic availability fallback.

The business write was correct; response arbitration was wrong.

## Chosen fix

The guard now derives availability response intent from explicit response semantics:

```text
_AVAILABILITY_RESPONSE_GOALS = {
  present_availability,
  requested_time_unavailable,
  no_availability,
}
```

Only outcomes in that set participate in `_verified_availability_claim` and in deterministic availability fallback rendering.

This preserves the invariant:

```text
availability-facing outcome → availability truth guard applies
```

while rejecting the invalid inference:

```text
availability fact exists → availability guard applies
```

The responder contract was also clarified so the model labels terminal/non-availability replies `not_applicable` even when supporting availability evidence is present.

## Outcome × guard matrix

Legend: YES = expected activation, NO = forbidden, CONDITIONAL = only when the explicit guard-specific semantic contract is present.

| Outcome / response goal | Medical | Compatibility | Pure doctor | Pure price | Availability | Device-price | Doctor append | Ownership gate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `booking_completed` | NO | NO | NO | NO | **NO** | NO | NO | YES if human ownership changed concurrently |
| `reschedule_completed` | NO | NO | NO | NO | **NO** | NO | NO | YES if human ownership changed concurrently |
| `cancellation_completed` | NO | NO | NO | NO | NO | NO | NO | YES if needed |
| `appointment_confirmed` | NO | NO | NO | NO | NO | NO | NO | YES if needed |
| `package_purchased` | NO | NO | NO | NO | NO | NO | NO | YES if needed |
| `pulse_pack_purchased` | NO | NO | NO | NO | NO | NO | NO | YES if needed |
| `follow_up_created` / `marketing_updated` | NO | NO | NO | NO | NO | NO | NO | YES if needed |
| `present_availability` | NO | NO | NO | NO | **YES** | NO unless a separate device clarification outcome exists | NO | YES |
| `requested_time_unavailable` | NO | NO | NO | NO | **YES** | NO | NO | YES |
| `no_availability` | NO | NO | NO | NO | **YES** | NO | NO | YES |
| `clarification` | NO | CONDITIONAL on `compatibility_failure` | NO | NO | **NO** | CONDITIONAL on `needed=device` + canonical price pairs | NO | YES |
| `answer_price` | NO | NO | NO | YES only when pure single outcome | NO | NO | NO | YES |
| `answer_doctor` | NO | NO | YES when pure; otherwise model | NO | NO | NO | CONDITIONAL completeness append | YES |
| `answer_service` / `answer_clinic_info` | NO | NO | NO | NO | NO | NO | NO | YES |
| `package_information` / `pulse_information` | NO | NO | NO | NO | NO | NO | NO | YES |
| `handoff` category medical | **YES** | NO | NO | NO | NO | NO | NO | same-turn ack exception then ownership pause |
| `handoff` non-medical | NO | NO | NO | NO | NO | NO | NO | same-turn ack exception then ownership pause |

### Supporting facts vs response semantics

Facts such as these may legally survive inside an outcome without becoming the dominant reply contract:

- availability windows/counts after booking/reschedule verification;
- device/doctor/service identity supporting a terminal action;
- package/billing context proving how a booking was fulfilled;
- price facts used to support a selection;
- active-task summaries retained for continuity/audit.

The dominant customer-facing claim is defined by `status + response_goal + action_result`, not by arbitrary fact presence.

## Terminal outcome audit

| Terminal goal | Supporting facts that may remain | Dominant customer claim | Guards allowed | Guards forbidden |
|---|---|---|---|---|
| `booking_completed` | service/doctor/device/start, package-use fact, availability verification evidence | booking succeeded exactly as action result proves | ownership race suppression | availability presentation, compatibility, device-price choice |
| `reschedule_completed` | target appointment context, new start, availability verification evidence | reschedule succeeded | ownership race suppression | availability presentation |
| `cancellation_completed` | appointment description/status | cancellation succeeded | ownership race suppression | availability guard |
| `appointment_confirmed` | appointment description/status | confirmation succeeded | ownership race suppression | availability guard |
| `package_purchased` | package/session/price/payment-recorded facts | purchase succeeded; no invented payment | ownership race suppression | availability guard |
| `pulse_pack_purchased` | Pulse pack/count/payment facts | pack purchase succeeded | ownership race suppression | availability guard |
| `follow_up_created` | follow-up time/context | follow-up created | ownership race suppression | availability guard |
| `marketing_updated` | consent state | preference updated | ownership race suppression | availability guard |

`active_task_cancelled` is terminal conversational state but `status=answered`, not a clinic write. Its claim is only that the unfinished task was cleared.

## Non-terminal outcome audit

- `present_availability`: availability guard is valuable and required.
- `requested_time_unavailable`: availability guard is valuable; must not be converted to generic no-availability when alternatives are supplied by a separate availability-facing outcome.
- `no_availability`: deterministic zero-availability correction is valuable.
- `clarification`: should be governed by the missing/ambiguous field; availability data may be supporting evidence but must not independently activate availability presentation. Compatibility/device-price guards already test the explicit clarification contract.
- choice goals (`ask_*_choice`): verified choices dominate; no generic fact-presence guard should replace the choice question.
- informational goals: structured facts support the answer; only exact-purpose deterministic completeness mechanisms should run.
- `handoff`: ownership semantics dominate. Medical handoff may use deterministic prose; other categories remain grounded responder output followed by ownership pause.

## Deterministic scenario matrix

Phase 1 tests use mocked responder drafts / constructed `TurnOutcome` objects only; no model calls.

### Availability guard

| Scenario | Expected | Pre-fix | Post-fix |
|---|---|---|---|
| `present_availability` + good windows + wrong model claim | guard activates | PASS | PASS |
| `requested_time_unavailable` + wrong model claim | guard activates | PASS | PASS |
| `no_availability` + wrong model claim | guard activates | PASS | PASS |
| `booking_completed` + start-only availability evidence | no guard | **FAIL** | PASS |
| `reschedule_completed` + start-only availability evidence | no guard | **FAIL** | PASS |
| terminal start-only evidence | no generic availability fallback | **FAIL** | PASS |
| `answer_clinic_info` + availability fact | no guard | **FAIL** (constructed) | PASS |
| `package_information` + availability fact | no guard | **FAIL** (constructed) | PASS |
| `clarification` + availability fact | no availability guard | **FAIL** (constructed) | PASS |
| device clarification + availability + canonical prices | device-price guard, not availability guard | overlapping predicates | PASS; semantically disjoint |

The initial audit matrix intentionally ran on the original code and produced `7 failed, 4 passed`, reproducing the terminal bug and proving the predicate-level overbreadth. After the targeted fix, the expanded audit matrix passes.

### Other guards

Deterministic tests confirm:

- medical renderer does not activate for payment handoff;
- compatibility renderer requires `needs_input` plus explicit `compatibility_failure`;
- doctor completeness does not append names to unrelated outcome types;
- device-price guard does not infer clarification from price fact presence;
- post-fix availability/device-price predicates are semantically disjoint;
- handoff acknowledgment exception requires a newly created, pending, unassigned AI handoff.

Existing responder tests already cover canonical price rendering, device-price completeness, pure doctor-list behavior, compatibility responses, positive availability correction, zero availability, and exact-time miss semantics.

## Guard precedence review

### Before fix

The problematic overlap was:

```text
clarification with availability/device prices
→ availability claim inferred from fact presence
→ availability fallback may run
→ device-price guard then runs on fallback
```

The second guard often recovered the correct device/price response, but the overlap was unnecessary and made precedence carry semantic responsibility.

### After fix

```text
availability-facing response goals
→ availability guard

explicit device clarification
→ device-price guard
```

These predicates are now semantically disjoint. The device-price guard can still run after an availability fallback defensively, but ordinary correct V2 outcomes do not require that overlap.

Pre-LLM short-circuits are also mutually narrow:

1. medical safety dominates by planner contract;
2. compatibility requires a single compatibility clarification;
3. pure doctor list requires only doctor-answer outcomes;
4. pure price requires exactly one price-answer outcome.

No evidence supports introducing a new central precedence framework at this time.

## Risk register

### H1 — Terminal booking completion overridden by availability evidence

- Severity if true: **P1 / High Risk** — hides a successful write and can encourage a customer retry.
- Likelihood from code: High.
- Deterministic evidence: **CONFIRMED**, reproduced exactly.
- Status: **FIXED** by response-semantic availability activation.

### H2 — Terminal reschedule completion overridden by availability evidence

- Severity if true: **P1 / High Risk** for the same lifecycle ambiguity.
- Likelihood from code: High; identical predicate and completed-write fact shaping.
- Deterministic evidence: **CONFIRMED** with constructed terminal outcome.
- Status: **FIXED**.

### H3 — Non-availability read/clarification overridden solely because availability facts are present

- Severity if true: Material P2.
- Likelihood from code: Predicate-level high; actual producer reachability varies by outcome type.
- Deterministic evidence: Constructed `answer_clinic_info`, `package_information`, and `clarification` outcomes were overridden pre-fix.
- Status: **GUARD-LEVEL CONFIRMED / FIXED GENERICALLY**. No separate runtime patch needed.

### H4 — Availability guard and device-price guard have conflicting semantic ownership

- Severity if wrong: Material P2.
- Pre-fix evidence: Both predicates could be true for device clarification; second guard usually recovered the device-price answer.
- Post-fix evidence: Availability claim is `not_applicable` for device clarification; device-price guard alone owns the correction.
- Status: **FIXED AS A CONSEQUENCE OF H1 FIX**.

### H5 — Doctor completeness append can bleed doctors into unrelated replies

- Severity if true: P2.
- Code evidence: `_verified_doctor_names` only reads `answered/answer_doctor` outcomes.
- Deterministic test: unrelated `answer_service` with doctor-shaped facts is unchanged.
- Status: **DISPROVED / LOW RISK**.

### H6 — Pure price / pure doctor / compatibility renderers infer intent from fact presence

- Severity if true: P2.
- Code evidence: all require exact status/response-goal contracts; price additionally requires exactly one outcome, compatibility exactly one outcome and explicit dimension.
- Existing + new tests: pass.
- Status: **DISPROVED / LOW RISK**.

### H7 — Medical deterministic reply could hide a successful business write from the same turn

- Severity if true: P1.
- Code evidence: `_safety_handoff` returns a terminal medical `TurnPlan` before operation planning; ordinary handoff plans retain only safe read companions and discard non-read companions.
- Deterministic architecture evidence: write companion cannot coexist with the safety medical path.
- Status: **DISPROVED**.

### H8 — Ownership guard suppresses the only acknowledgment for a newly created AI handoff

- Severity if true: Material P2.
- Code evidence: `_v2_handoff_ack_allowed` permits exactly a same-turn AI-created `pending`, unassigned handoff ack, but blocks stale replies after claim/takeover.
- Deterministic test: all four relevant states pass.
- Status: **DISPROVED / WELL-SCOPED**.

### H9 — Availability fallback cannot render incomplete availability data

- Severity: P3 for genuine availability presentation; P1/P2 only when activation itself is wrong.
- Evidence: start-only terminal evidence caused the production fallback pre-fix. For a genuine availability-facing outcome with incomplete evidence, the generic fallback remains truthful and fail-safe.
- Status: **LOW RISK after activation fix; no renderer rewrite**.

### H10 — Terminal natural-language responder could still describe completion incorrectly even though no deterministic guard overrides it

- Severity if true: P1/P2 depending on wording.
- Deterministic evidence: cannot prove model phrasing; outcome contract, prompt, and terminal action-result tests are strong but not equivalent to live model behavior.
- Status: **NEEDS LIMITED LIVE VALIDATION** for booking/reschedule acknowledgment only.

### H11 — V2 lacks the legacy final `sanitize_customer_reply` pass

- Severity if exploited: potentially P2 for internal identifier exposure, but not a deterministic false-positive issue.
- Evidence: V2 outcome shaping recursively strips `_id` / `_ids` and internal reference metadata before responder composition, and the prompt forbids internal identifiers. No reproducible current-path leak was found.
- Status: **LOW RISK / NO CHANGE**. Do not add a legacy semantic rewrite without evidence.

### H12 — Provider/responder failure after a verified write can persist a write with no acknowledgment

- Severity if true: P1.
- Code evidence: live V2 write executor is called with `commit=False`; outbound persistence and commit occur after successful orchestration. Existing transaction design owns the outer commit.
- Status: **LOW RISK / NO RESPONSE-GUARD CHANGE**. Transaction tests remain the appropriate coverage.

## API validation shortlist

Phase 1 is complete before any API use.

### LIVE-1 — Package-backed booking completion acknowledgment

- Why deterministic tests are insufficient: they prove the guard no longer overrides, but not the natural responder text emitted by the configured live model.
- Scenario: synthetic Demo patient, verified package-backed booking, exact slot, confirmation; rollback isolation.
- Expected: booking acknowledgment; no availability presentation/fallback; no duplicate write.
- Risk if failed: P1/P2 customer may retry an already successful booking.
- Estimated calls: 2–4 responder/interpreter calls depending on fixture path.

### LIVE-2 — Reschedule completion acknowledgment

- Why deterministic tests are insufficient: same final language dependency.
- Scenario: synthetic Demo appointment, exact verified reschedule, confirmation; rollback isolation.
- Expected: reschedule acknowledgment; no availability presentation/fallback.
- Risk if failed: P1/P2 lifecycle ambiguity.
- Estimated calls: 2–4.

No random exploratory live conversations are justified. Availability true-positive/zero/exact-miss behavior is fully deterministic at the guard level and covered by unit tests, so no API spend is needed for those paths.

## Architecture verdict

### Necessary deterministic protections

- medical handoff deterministic rendering;
- compatibility correction;
- canonical pure pricing/device pricing;
- doctor completeness for explicit doctor-list outcomes;
- availability truth guard;
- outcome status/action contract;
- outcome internal-ID shaping;
- human ownership suppression and narrow handoff acknowledgment exception.

### Useful but previously over-broad protection

- availability guard activation. It was using fact presence as response intent; narrowed now.

### Redundant protections

No material redundant protection found. There is some defensive overlap where the device-price guard is still invoked after availability fallback, but after the fix normal predicates are disjoint and this does not justify refactoring.

### Risky protections

No remaining reproducible material deterministic override after the availability fix.

### Pure presentation fallbacks

- deterministic availability range renderer;
- generic availability fallback when an availability-facing outcome is valid but renderable detail is incomplete;
- canonical price/doctor list text for pure reads.

### Complexity judgment

`NEEDS TARGETED SIMPLIFICATION`, not a material design problem.

The deterministic layer is small enough and mostly outcome-semantic. The confirmed problem was one over-broad proxy predicate. A central response-policy architecture or removal of deterministic guards would add migration risk without evidence of corresponding benefit.

If a repeated fact-presence pattern appears in future guards, options would be:

1. Narrow each guard to explicit response semantics — lowest risk and preferred while the guard count remains small.
2. Add an explicit response-category field shared by guards — useful only if outcome/guard count grows enough to justify migration.
3. Centralize guard precedence — highest migration surface; not justified by this audit.

Current recommendation: keep Option 1. Stop after the targeted availability fix unless new deterministic or live evidence appears.

## Phase 1 verdict

```text
CURRENT TERMINAL AVAILABILITY BUG = FIXED LOCALLY / PENDING MERGE+DEPLOY

DETERMINISTIC RESPONSE AUDIT = PHASE 1 COMPLETE

Material deterministic risks found:
- H1 terminal booking completion availability override — CONFIRMED/FIXED
- H2 terminal reschedule completion availability override — CONFIRMED/FIXED
- H3 fact-presence availability override family — CONFIRMED AT GUARD LEVEL/FIXED

Need immediate runtime fix:
- Availability activation predicate only (implemented)

Need limited API validation:
- LIVE-1 booking completion acknowledgment
- LIVE-2 reschedule completion acknowledgment

Low-risk / ignored:
- legacy V1 response paths outside V2
- generic availability incomplete-detail fallback after correct activation
- legacy final sanitizer absence without a reproducible V2 leak
- broad deterministic refactor / central precedence redesign

Overall verdict:
NEEDS TARGETED SIMPLIFICATION
```

## Phase 2 — Limited live validation results

Phase 2 ran only the two scenarios shortlisted after Phase 1. The run used the Demo workspace, a synthetic patient, a synthetic zero-paid package entitlement, the eval advisory lock, and an outer database transaction that was rolled back. No production customer/appointment data was used and no persistent test mutation was kept.

### LIVE-1 — Package-backed booking completion acknowledgment

Result: **PASS**

- User intent: exact service/date/time/doctor/device plus explicit existing-package usage.
- Structured terminal result: `status=completed`, `response_goal=booking_completed`, `action=booking`, `status=confirmed`, `package_used=true`.
- Final reply: acknowledged that the booking was completed and that the existing package was used.
- Responder source: `openai:gpt-5.6-luna` (normal responder).
- `deterministic:availability-guard`: **not used**.
- Generic incomplete-window fallback: **not emitted**.
- Exactly one synthetic appointment was created inside the rollback transaction.

### LIVE-2 — Reschedule completion acknowledgment

Result: **PASS**

- Follow-up on the same synthetic appointment to a separately verified future slot.
- Structured terminal result: `status=completed`, `response_goal=reschedule_completed`, `action=reschedule`, `status=confirmed`.
- Final reply: acknowledged the changed appointment date/time and preserved the service/device context.
- Responder source: `openai:gpt-5.6-luna` (normal responder).
- `deterministic:availability-guard`: **not used**.
- Generic incomplete-window fallback: **not emitted**.

Financial invariants across both scenarios:

- payment transaction IDs unchanged;
- payment allocation IDs unchanged;
- Pulse usage IDs unchanged;
- no new persistent package/payment/Pulse test data survived rollback.

### API shortlist disposition

Both material model-dependent hypotheses passed. No additional live scenarios are justified by the deterministic audit, so Phase 2 stops here.

```text
LIVE VALIDATION REQUIRED: COMPLETE
Additional live scenarios: NONE
```

## Final audit status before merge/deploy

```text
CURRENT TERMINAL AVAILABILITY BUG = FIXED + LIVE-VALIDATED / PENDING MERGE+DEPLOY

DETERMINISTIC RESPONSE AUDIT = COMPLETE

Material deterministic risks found:
- terminal booking/reschedule success overridden by availability evidence
- generic fact-presence activation family in the availability guard

Need immediate runtime fix:
- one targeted availability activation narrowing (implemented)

Need later API validation:
- NONE after the two shortlisted scenarios passed

Low-risk / ignored:
- legacy V1 response mechanisms outside active V2 path
- pure presentation fallback wording when correctly activated
- hypothetical identifier leak without reproducible V2 evidence
- broad responder/precedence refactor

Overall verdict:
NEEDS TARGETED SIMPLIFICATION — satisfied by the targeted availability guard narrowing; no broad redesign warranted.
```

## Validation summary

Deterministic/local validation completed before PR:

- Initial pre-fix audit matrix: `7 failed, 4 passed` (expected red phase proving guard overbreadth).
- Post-fix responder/audit set: `38 passed`.
- Expanded targeted V2 safety/continuity gate: `188 passed`.
- Full `test_v2*.py` sweep: `467 passed`; one cwd-sensitive architecture test failed only because the sweep was launched from repository root. Re-running that exact test from the CI-standard `backend/` working directory: `1 passed`.
- Backend Ruff: PASS.
- Backend compileall: PASS.
- Alembic: `0086_all_service_packages (head)`.
- Agent-eval tooling Ruff + compileall: PASS.
- Agent-eval tooling tests: `39 passed`.
- Limited live validation: 2/2 scenarios PASS, rollback isolated.

The repository CI remains the merge gate for clean PostgreSQL migration, complete backend tests, and frontend lint/typecheck/build.
