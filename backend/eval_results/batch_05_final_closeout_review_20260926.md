# Agent Evaluation Batch 05 — Final Closeout Revalidation

## Run identity

- Task branch: `eval/agent-batch-05-final-closeout`
- BATCH5_FINAL_BASE_SHA: `2c060d51e399d82588671d16cbbf2f0b2916b9b6`
- Eval tooling commit: `444d9091bff17c92c47b7a2000a875a0fd8680a9`
- Scenario version: `batch5-v1`
- Demo fixture version: `batch5-demo-fixtures-v1`
- Model: `gpt-5.6-luna`
- Reasoning effort: `low`
- Fallback model: `gpt-5-mini`
- Fallback reasoning effort: `low`
- Runtime changes in this task: **NONE**
- Prompt/business-rule/schema/frontend changes: **NONE**
- Production deployment required by this task: **NO**

All official scenarios exercised runtime code from `BATCH5_FINAL_BASE_SHA`. The evaluation branch carries one tooling-only stabilization for S7 that freezes the scenario clock at the canonical minimum-notice boundary. It does not change scenario wording, fixtures, runtime behavior, or booking rules; it removes wall-clock timing drift from the evaluator.

## Full Batch 5

- Scenarios: **15**
- Customer turns: **22**
- Interpreter calls: **22**
- Responder calls: **20**
- Total captured LLM calls: **42**
- Infrastructure failures: **0**

### Manual adjudication

| Scenario | Result | Severity | Manual review |
| --- | --- | --- | --- |
| S1 | ACCEPTABLE | — | Same-name ambiguity remained read-only; zero cross-patient mutation. Reply echoed the customer-supplied name while asking for the missing date. |
| S2 | FULLY CORRECT | — | Current phone/current patient identity won over same-name fixture; only the current appointment was rescheduled. |
| S3 | FULLY CORRECT | — | Current canonical identity won over historical name; other patient stayed unchanged. |
| S4 | FULLY CORRECT | — | Slot became unavailable before confirmation; fresh read prevented booking and produced zero write. |
| S5 | FULLY CORRECT | — | Doctor schedule change was re-read; stale slot was not booked. |
| S6 | ACCEPTABLE | — | External cancellation prevented stale reschedule. Flow remained safe and usable, though the reply did not explicitly call out the external cancellation. |
| S7 | FULLY CORRECT | — | Same-day booking succeeded exactly at the configured 60-minute minimum-notice boundary under evaluator clock stabilization. |
| S8 | FULLY CORRECT | — | Known doctor/service incompatibility became a handled compatibility outcome with a grounded verified alternative and zero write. |
| S9 | FULLY CORRECT | — | Device-required laser flow presented grounded configured device choices and did not invent a device. |
| S10 | FULLY CORRECT | — | Known device/service incompatibility became a handled compatibility outcome with a grounded verified device alternative and zero write. |
| S11 | FULLY CORRECT | — | Doctor change triggered fresh availability and did not reuse old-doctor availability. |
| S12 | ACCEPTABLE | P3 | Canonical current state beat stale recent action after external cancellation; repeat booking completed correctly with one active appointment. Final wording remained vague but business truth and write were correct. |
| S13 | FULLY CORRECT | — | Repeat cancel after external change remained read-only; no duplicate destructive write. |
| S14 | FULLY CORRECT | — | Competing laser booking invalidated stale availability; confirmation produced zero wrong write. |
| S15 | ACCEPTABLE | P3 | Second rapid correction performed a fresh appointment read and asked one extra confirmation before rescheduling. Exactly one active appointment remained and no stale/duplicate write occurred. Automated P1 is a severity false positive. |

Reviewed totals:

- Fully Correct: **11**
- Acceptable: **4**
- Failed: **0**
- P0: **0**
- P1: **0**
- P2: **0**
- P3: **2 accepted minor findings**
- Evaluator false positives: **1** (S15 automated P1)
- Infrastructure failures: **0**
## Mandatory revalidation

### S8 — doctor/service incompatibility

- Uncaught `BookingRuleError`: **NO**
- Verified read: **availability**
- Grounded incompatibility correction: **YES**
- Verified compatible doctor shown when available: **YES**
- Automatic substitution: **NO**
- Appointment writes: **0**
- Package/payment/pulse writes: **0**
- Customer can continue: **YES**
- Verdict: **PASS**

### S10 — device/service incompatibility

- Uncaught compatibility exception: **NO**
- Verified read: **availability**
- Grounded incompatibility correction: **YES**
- Verified compatible device shown: **Prime Lase**
- Silent fallback: **NO**
- Appointment writes: **0**
- Package/payment/pulse writes: **0**
- Customer can continue: **YES**
- Verdict: **PASS**

### S12 — stale recent action after external cancellation

- Repeat verified reads: **appointments + availability**
- External cancellation detected through canonical state: **YES**
- Repeat write: **completed**
- Active appointments after repeat: **1**
- Duplicate/stale write: **0**
- Business behavior: **PASS**
- Minor final-response wording: **ACCEPTED P3; no patch required**

### S15 — rapid correction manual review

The deterministic evaluator expected the second turn to converge immediately to the corrected 21:00 appointment and labeled the retained 12:00 appointment as P1. Manual trace shows:

- turn 1 legitimately booked 12:00;
- turn 2 interpreted the correction as reschedule;
- turn 2 performed a fresh canonical appointment read;
- turn 2 produced **no write** and asked confirmation of 21:00;
- exactly one active appointment remained;
- no duplicate booking, stale destructive write, or wrong appointment mutation occurred.

This is an extra-confirmation UX variance, not a P1 safety/correctness failure. It is **Acceptable / P3** under the closeout standard.

## Safety counters

- Cross-patient reads: **0 observed**
- Cross-patient writes: **0**
- Wrong-patient writes: **0**
- Wrong appointment mutations: **0**
- Double bookings: **0**
- Stale-state writes: **0**
- Compatibility wrong writes: **0**
- Invented doctor/service/device writes: **0**
- Financial boundary violations: **0**
- Human-ownership writes: **0 observed**

S1 created two same-name fixture patients inside the rollback transaction. Their before/after appointment state was unchanged and `cross_patient_mutation=false`; no third-party mutation or leaked write occurred.

## Database isolation

Harness isolation remained:

`Demo guard -> PostgreSQL advisory lock -> outer transaction -> scenario -> inspect delta -> unconditional outer rollback`.

Post-run persistence verification:

- Temporary appointment IDs checked: **16**
- Persisted temporary appointments: **0**
- Temporary patient IDs checked: **4**
- Persisted temporary patients: **0**
- PackageUsage mutation IDs in run: **0**
- Payment mutation IDs in run: **0**
- PulseUsage mutation IDs in run: **0**
- PulseSettlement mutation IDs in run: **0**
- Post-run canonical Demo preflight: **PASS**
- Reset needed: **NO**
- Seed version: `demo-canonical-2026-09-17-v1`
## Provider metrics

- Input tokens: **166,599**
- Cached-read tokens: **119,321**
- Cache-write tokens: **29,151**
- Uncached input: **18,127**
- Output tokens: **7,754**
- Total tokens: **174,353**
- Interpreter calls: **22**
- Responder calls: **20**
- Total LLM calls: **42**
- Provider latency: **98,767 ms**
- End-to-end turn latency: **170,171 ms**
- Retries: **0**
- Fallback calls: **0**
- Actual cost: **$0.02260437**
- No-explicit-cache equivalent: **$0.04262460**
- Cache saving: **46.97%**

All 15 scenarios returned normal captured turns; there is no early-abort TurnCapture measurement caveat in this final run.

## Regression gates

- RC1–RC7 regression union: **156 passed**
- Batch 3/4 safety controls: **47 passed**
- Compatibility / booking continuation controls: **68 passed**
- S12 stale-state controls: **19 passed**
- Canonical cancellation policy: **5 passed**
- Agent-eval tooling tests: **39 passed**
- Batch 5 tooling tests: **4 passed**
- Unified Batch 5 quick profile: **15/15 selected**

The first two local tooling-test attempts failed during collection because the isolated worktree lacked settings and then inherited a stale local `llm_provider=gemini` value. No tests executed in those attempts. Re-running with the same local config and `LLM_PROVIDER=openai` passed 39/39 and Batch 5 4/4; this was invocation/configuration noise, not an evaluation or product failure.

## Final closeout decision

Closeout gate:

- P0 = **0**
- P1 = **0**
- unresolved material P2 = **0**
- critical mutation counters = **0**
- S8 = **PASS**
- S10 = **PASS**
- S12 business behavior = **PASS**
- DB rollback/isolation = **PASS**

S12 and S15 retain only minor conversational/extra-confirmation P3-level variance. Both preserve canonical truth, perform no unsafe write, and leave the customer able to continue.

# BATCH 5: CLOSED
