# Batch 7 Long-Horizon / Cross-Intent Stress Evaluation — Manual Review

## Runtime and scope

- Branch: `eval/agent-batch-07`
- Runtime / base SHA: `6f651f11def77478ad6b24d5f59f9362f752aebd`
- Scenario version: `batch7-v1`
- Fixture version: `batch7-demo-fixtures-v1`
- Runtime changes: **none**
- Prompt/model/business-rule/schema/frontend changes: **none**
- Primary full raw evidence: `batch_07_raw_20260927T120659Z.json` / `.md`
- Targeted detector-validation reruns were executed for S9/S12/S13/S16 after hardening eval-only counters. Their material outcomes are summarized below; the committed primary raw evidence remains the official 16-scenario run.

The official 16-scenario full run completed with no infrastructure failure. Manual DB-lineage review found lifecycle-state failures that the first-pass direct-child detector missed. The evaluator was then hardened **only in eval tooling** to follow transitive reschedule chains and to detect stale/restarted active tasks. No runtime patch was made.

## Execution summary

- Scenarios: **16**
- Customer turns: **93**
- Average turns/scenario: **5.81**
- Interpreter calls: **97**
- Responder calls: **74**
- Total LLM calls: **171**
- Input tokens: **857,208**
- Cached-read tokens: **526,710**
- Cache-write tokens: **126,875**
- Uncached input: **203,623**
- Output tokens: **32,807**
- Total tokens: **890,015**
- Tokens/customer turn: **9,570.05**
- LLM calls/customer turn: **1.839**
- Provider latency: **497,189 ms**
- E2E turn latency total: **897,014 ms**
- Retries: **4**
- Fallbacks: **0**
- Actual cost: **$0.12234595**
- No-cache cost: **$0.21081000**
- Cache saving: **$0.08846405 (41.96%)**
- Infrastructure failures: **0**

## Manual adjudication

| Scenario | Classification | Severity | Summary |
|---|---|---:|---|
| S1 | Fully Correct | — | Booking survives doctor and price side reads; one canonical Prime Lase booking. |
| S2 | Fully Correct | — | Multiple doctor/date/time corrections correctly invalidate prior scheduling facts. |
| S3 | Fully Correct | — | Candela is replaced by Prime after side reads; final booking and price are Prime-only. |
| S4 | Fully Correct | — | Laser → Hydrafacial replacement clears laser-only device state. |
| S5 | Acceptable | P3 | Standard → laser correctly requires device and writes once; final acknowledgment is overly generic. |
| S6 | Fully Correct | — | Package read stays read-only; financial question hands off; booking task survives handback. |
| S7 | Acceptable | P3 | Package purchase + later package-backed booking is correct; initial package-list response is unnecessarily unhelpful. |
| S8 | Fully Correct | — | Externally invalidated package is revalidated and fails closed with zero stale entitlement usage. |
| S9 | Failed | **P1** | Official full run created a second reschedule replacement after a plain “تمام”. |
| S10 | Acceptable | P3 | Correct appointment A remains target; one unnecessary booking-number clarification occurs before success. |
| S11 | Fully Correct | — | Cancel A then reschedule B targets the correct appointments exactly once. |
| S12 | Failed | **P1** | After honoring Reception's canonical edit, a plain follow-up confirmation reschedules the newly-created replacement again. |
| S13 | Failed | **P2** | External cancellation wins in DB and no stale write occurs, but stale reschedule task remains active and conversation continues as if target were live. |
| S14 | Fully Correct | — | Explicitly abandoned booking is cleared; new laser booking starts clean. |
| S15 | Fully Correct | — | Ambiguous “والليزر؟” does not replace active Hydrafacial task. |
| S16 | Failed | **P2** | Long detour after completed booking loses recent-action acknowledgment; repeat confirmation starts a new booking task and reports own slot unavailable. |

Totals:

- Fully Correct: **9**
- Acceptable: **3**
- Failed: **4**
- P0: **0**
- P1 scenario manifestations: **2**
- P2 scenario manifestations: **2**
- P3 accepted: **3**

## Material Finding 1 — duplicate reschedule after completed lifecycle action

**Severity: P1**

Affected scenarios:
- S9
- S12

### Evidence

S9 official full-run lineage:

1. source appointment `869d...` → `rescheduled`
2. requested replacement `9a09...` at 2026-09-29 10:00 → later marked `rescheduled`
3. plain customer acknowledgment `تمام` creates replacement-of-replacement `80b6...` at the same time → `confirmed`

S12 official full-run lineage:

1. canonical Reception-edited source `a1dd...` → `rescheduled`
2. requested replacement `0301...` at 2026-10-01 10:00 → later marked `rescheduled`
3. `تمام كملي التغيير` creates replacement-of-replacement `3700...` at the same time → `confirmed`

The initial evaluator only used direct `_replacement_rows(source)`, so the second-generation replacement was invisible to `duplicate_writes`. The evaluator now also traverses the full transitive replacement chain.

### First incorrect layer

Interpreter / recent-action lifecycle recognition: after the first reschedule is already completed and active task is cleared, a bare acknowledgment is interpreted as a fresh executable reschedule using recently completed facts, creating new write authority.

### Reproduction stability

- Confirmed in the official full run for both S9 and S12.
- First targeted rerun of S9 and S12 did **not** reproduce the duplicate, so this is intermittent LLM/runtime behavior rather than deterministic every-run behavior.
- Because the official full run contains actual duplicate DB lifecycle writes, severity remains P1.

### Recommended focused task

Harden completed-reschedule recent-action acknowledgment / duplicate protection so a semantically empty confirmation cannot create a new reschedule chain when canonical replacement already matches the just-completed action.

## Material Finding 2 — external cancellation does not invalidate persisted reschedule task

**Severity: P2**

Affected scenario:
- S13

### Evidence

Reception/fixture cancels the canonical source appointment before the customer completes the pending reschedule.

Canonical final DB:
- source status: `cancelled`
- replacements: **0**
- stale lifecycle writes: **0**

But subsequent turns still preserve a `reschedule / awaiting_choice` task. The agent checks availability for the requested replacement and asks the customer to continue rather than explaining that the source appointment was cancelled.

Corrected targeted control reproduces:

- `wrong_active_task_target = 1`
- P2
- no write

### First incorrect layer

Active-task lifecycle reconciliation after fresh canonical appointment read. Canonical cancellation is observed sufficiently to prevent a write, but it does not invalidate/clear the persisted reschedule target.

### Recommended focused task

When fresh appointment verification proves the persisted lifecycle target is no longer actionable (cancelled/rescheduled/completed as appropriate), invalidate the lifecycle task before continuing availability or package-choice flow.

## Material Finding 3 — completed booking recent-action identity is lost after long detour

**Severity: P2**

Affected scenario:
- S16

### Evidence

1. Hydrafacial booking is successfully created at 2026-09-28 10:00.
2. Customer asks price.
3. Customer asks clinic info.
4. Customer lists appointments and sees the same booking.
5. Customer says: `تمام احجزيه زي ما اتفقنا`.

No duplicate write occurs, but the turn is interpreted as a new `book` operation. Availability then sees the already-created booking occupying that time and replies that the requested time is unavailable. A new booking task remains active.

Corrected targeted control reproduces:

- `unexpected_task_restart = 1`
- P2
- duplicate DB writes = 0

### First incorrect layer

Interpreter / recent-action revalidation boundary after a long informational detour. The canonical existing appointment is not recognized as the completed action the customer is referring to.

### Recommended focused task

Revalidate completed booking recent-action identity against canonical appointments before starting a new booking task from a repeated confirmation. If the canonical appointment still matches, acknowledge it instead of re-entering booking.

## Reviewed state continuity counters

These are the **material observations from the official full run plus targeted detector validation**, not the original pre-hardening aggregate which missed transitive lifecycle chains.

- stale_service_carryovers: **0**
- stale_doctor_carryovers: **0**
- stale_device_carryovers: **0**
- stale_date_time_carryovers: **0**
- stale_package_carryovers: **0**
- wrong_active_task_target: **1** (S13)
- unexpected_task_restart: **1** (S16)
- unexpected_task_loss: **0**
- duplicate_writes: **2** (S9, S12)
- stale_lifecycle_writes: **2** (S9, S12)
- wrong_appointment_writes: **0**
- side_read_business_writes: **0**
- financial_boundary_violations: **0**
- human_ownership_writes: **0**
- invented_entity_writes: **0**

## DB / safety observations

- No cross-patient or wrong-patient business mutation was observed.
- No financial DB write was produced by the financial interruption control.
- Human ownership boundary in S6 was respected.
- No invented entity write was observed.
- Package invalidation in S8 failed closed.
- S9/S12 are the only observed destructive material family: duplicate lifecycle reschedule chains.
- S13/S16 have no duplicate/destructive write, but materially wrong long-horizon state handling.

## Minor accepted findings

1. S5: successful laser booking can receive a generic availability-style acknowledgment.
2. S7: initial package-list reply says details are unavailable even though later canonical package purchase succeeds.
3. S10: one unnecessary booking-number clarification occurs before the persisted target is correctly rescheduled.

These remain P3/Acceptable because the business result is grounded, safe, and the supported flow remains usable.

## Root-cause grouping

### Finding 1 — completed lifecycle action can be re-executed by a bare acknowledgment
- Affected: S9, S12
- Severity: P1
- Root layer: interpreter/recent-action lifecycle duplicate protection

### Finding 2 — canonical cancellation does not invalidate active reschedule state
- Affected: S13
- Severity: P2
- Root layer: active-task canonical lifecycle reconciliation

### Finding 3 — long-detour repeated booking confirmation restarts booking instead of acknowledging canonical completion
- Affected: S16
- Severity: P2
- Root layer: recent-action booking revalidation / task-start boundary

## Isolation and validation

- Canonical Demo preflight after live + targeted runs: **PASS**
- Canonical reset: **NOT_NEEDED**
- Unified eval tooling: **44 passed**
- Batch 7 quick profile: **16/16**
- Long-horizon regression control set: **169 passed**
- Clean PostgreSQL migrations from zero: **PASS**
- Full backend on clean PostgreSQL: **1621 passed, 4 skipped**
- Ruff (eval tooling): **PASS**
- compileall (eval tooling): **PASS**
- git diff --check: **PASS**
- Alembic single head: `0086_all_service_packages`
- Runtime diff in this branch: **0** (evaluation tooling/evidence only)

## Verdict

**BATCH 7 = BLOCKED BY MATERIAL FINDINGS**

No runtime fix is included in this branch. The next work should be focused fixes grouped by the three root-cause families above, followed by a Batch 7 closeout revalidation.

Do not start Batch 8 from this baseline.
