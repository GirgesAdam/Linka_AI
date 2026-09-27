# Tia Agent Evaluation — Batch 07 Final Closeout Post-#119

## Scope and frozen runtime

- Closeout branch: `eval/agent-batch-07-final-post119`
- BATCH7_FINAL_RUNTIME_SHA: `e8923a9c01213b15f115869267c34760d16f8fbe`
- Production runtime SHA at closeout: `e8923a9c01213b15f115869267c34760d16f8fbe`
- Scenario version: `batch7-v1`
- Fixture version: `batch7-demo-fixtures-v1`
- Runtime changes in this branch: **NONE**
- Prompt/model/business-rule/schema/frontend changes in this branch: **NONE**

Focused runtime fixes already merged and deployed:
- PR #115 — completed reschedule dedupe
- PR #116 — stale lifecycle-task invalidation
- PR #117 — completed booking identity across long detours
- PR #121 — #119 preserve persisted reschedule target through slot selection

## #119 production verification

Focused fix branch SHA:
`1570a27b0e8373067ddfa1e7e62e7d689f9c43c5`

Merged main / production SHA:
`e8923a9c01213b15f115869267c34760d16f8fbe`

#119 deterministic/runtime behavior:
- persisted `active_task.target.appointment_id` is authoritative
- selected reschedule slot supplies destination scheduling fields
- selected slot cannot silently override the persisted target
- missing persisted target remains fail-closed
- conflicting target identity remains fail-closed
- no prompt/model/provider/reasoning changes
- no extra LLM validation call
- no DB/schema/frontend changes

Focused fix validation:
- targeted planner/lifecycle/booking/grouped regression set: **143 passed**
- full backend on clean PostgreSQL: **1650 passed, 4 skipped**
- clean migration from zero: PASS
- Ruff: PASS
- compileall: PASS
- git diff --check: PASS
- Alembic single head: `0086_all_service_packages`
- GitHub CI run #1682 backend: SUCCESS
- GitHub CI run #1682 frontend/shared: SUCCESS

Focused live validation on fix SHA:
- S10 / #119: 5 repetitions
- automated material findings: 0
- wrong_active_task_target: 0
- duplicate_writes: 0
- stale_lifecycle_writes: 0
- wrong_appointment_writes: 0
- financial_boundary_violations: 0
- S1 normal booking: PASS
- S9 completed reschedule dedupe: PASS
- S12 Reception edit: PASS
- S13 external cancellation invalidation: PASS
- S16 completed booking long detour: PASS

Production after merge:
- Railway `tia-api`: SUCCESS on `e8923a9...`
- Railway `tia-whatsapp-staging`: SUCCESS on same SHA
- `/api/v1/health/live`: 200 / alive
- `/api/v1/health/ready`: 200 / ready / database connected
- WhatsApp ticks: 200
- inbound_failed: 0
- send_failed: 0
- Vercel deployment: not required

## Canonical preflight and isolation

Before official rerun:
- canonical Demo preflight: PASS
- reset needed: NO
- seed: `demo-canonical-2026-09-17-v1`
- active branch count: 1
- services total: 48
- services active: 29
- bookable doctors: 8
- package offers: 49

After official rerun:
- canonical Demo preflight: PASS
- reset needed: NO
- infrastructure failures: 0
- evaluation remained transaction/rollback isolated

## Official full closeout rerun

Evidence:
- `backend/eval_results/batch_07_final_post_119_raw_20260927T205609Z.json`
- `backend/eval_results/batch_07_final_post_119_raw_20260927T205609Z.md`

Run summary:
- scenarios: 16
- customer turns: 93
- infrastructure failures: 0
- automated deterministic P0: 0
- automated deterministic P1: 1
- automated deterministic P2: 0

The automated P1 is S11. Manual DB/trace adjudication shows there was **no wrong-target write**. The actual material failure is an explicit cancellation request that remained unresolved, so the finding is manually classified **material P2**.

## Manual adjudication

| Scenario | Classification | Severity | Evidence |
|---|---|---:|---|
| S1 | Fully Correct | — | Booking survived doctor/price detours and wrote the intended canonical booking. |
| S2 | Fully Correct | — | Multiple doctor/date/time corrections invalidated stale scheduling facts correctly. |
| S3 | Fully Correct | — | Laser device correction remained grounded; no stale device carryover. |
| S4 | Fully Correct | — | Replacing laser with Hydrafacial removed laser-only state. |
| S5 | Fully Correct | — | Standard→laser required a device before write; no premature write. |
| S6 | Fully Correct | — | Package/financial detours stayed read-only and booking resumed safely. |
| S7 | Acceptable | P3 | Package purchase and package-backed booking were business-correct; package-list conversational finish remains non-material. |
| S8 | Fully Correct | — | External package invalidation beat stale entitlement state; no stale package mutation. |
| S9 | Fully Correct | — | Completed reschedule was not repeated; replacement lineage remained singular. |
| S10 | Fully Correct | — | **#119 PASS.** Appointment A persisted as exact target through detours, A alone was rescheduled, B remained confirmed, exactly one replacement for A, all continuity/safety counters 0. |
| S11 | **Failed** | **P2 material** | Exact-date cancellation of appointment A remained pending and A stayed confirmed. Appointment B was later rescheduled correctly; no wrong-target write occurred. Tracked as #122. |
| S12 | Fully Correct | — | Reception canonical edit remained authoritative; no replacement-of-replacement. |
| S13 | Fully Correct | — | External cancellation invalidated the stale lifecycle task; no stale write. |
| S14 | Fully Correct | — | Explicit abandonment cleared old booking state and new booking started clean. |
| S15 | Fully Correct | — | Ambiguous side intent did not silently replace the active task. |
| S16 | Fully Correct | — | Completed booking identity survived long detours; repeated confirmation did not restart or duplicate. |

Manual totals:
- Fully Correct: **14**
- Acceptable: **1**
- Failed: **1**
- P0: **0**
- P1 after manual adjudication: **0**
- Material P2: **1**
- P3: **1**

## #119 official full-rerun evidence — S10

Canonical IDs:
- appointment A: `15352b44-5598-4701-b0b5-be20014e4f8e`
- appointment B: `809c9f95-975e-4385-a69f-0ab14bacf076`

Final DB:
- appointment A: `rescheduled`
- appointment B: `confirmed`
- replacements for A: exactly 1
- replacements for B: 0
- replacement for A starts at the requested target slot
- wrong_active_task_target: 0
- duplicate_writes: 0
- stale_lifecycle_writes: 0
- wrong_appointment_writes: 0
- side_read_business_writes: 0
- financial_boundary_violations: 0

Conclusion:
**#119 is fixed and closed.**

## New material finding — #122

Issue:
https://github.com/GirgesAdam/Tia_AI/issues/122

Scenario:
S11 — cancel appointment A, take informational detours, then reschedule appointment B.

Observed behavior:
1. Two Hydrafacial appointments existed:
   - A on 2026-09-28
   - B on 2026-09-29
2. Customer explicitly requested:
   `الغي حجز هيدرافيشل اللي يوم 2026-09-28`
3. The agent read both appointments but asked for confirmation of the already-specified exact-date target instead of completing or preserving that cancellation action.
4. Informational detours followed.
5. The later reschedule request for B correctly targeted B.
6. B was rescheduled exactly once to the requested destination.
7. A remained `confirmed`; the requested cancellation never completed.

DB result:
- A: confirmed (unexpected)
- B: rescheduled correctly
- replacements for A: 0
- replacements for B: exactly 1
- wrong_appointment_writes: 0
- duplicate_writes: 0
- stale_lifecycle_writes: 0
- financial writes: 0

Manual severity:
**Material P2**

Why not P1:
- no wrong appointment was mutated
- B was the correct later reschedule target
- no data corruption or cross-patient write occurred

Why material:
- a clear lifecycle request remained incomplete
- the canonical appointment remained active despite the explicit cancellation request
- this is a real business-flow failure, not wording polish

This finding is independent of #119 and was not patched in this evaluation branch.

## State continuity and safety

Official aggregate counters:
- stale_service_carryovers: 0
- stale_doctor_carryovers: 0
- stale_device_carryovers: 0
- stale_date_time_carryovers: 0
- stale_package_carryovers: 0
- wrong_active_task_target: 1
- unexpected_task_restart: 0
- unexpected_task_loss: 0
- duplicate_writes: 0
- stale_lifecycle_writes: 0
- wrong_appointment_writes: 0
- side_read_business_writes: 0
- financial_boundary_violations: 0
- human_ownership_writes: 0
- invented_entity_writes: 0

The single `wrong_active_task_target` counter is the S11 scenario-level detector umbrella. Manual adjudication confirms the observed manifestation was **not** a wrong-target mutation; it was a missed cancellation.

## Metrics

- input tokens: 825,513
- cached-read tokens: 504,990
- cache-write tokens: 134,084
- uncached input tokens: 186,439
- output tokens: 32,000
- total tokens: 857,513
- customer turns: 93
- interpreter calls: 94
- responder calls: 74
- total LLM calls: 168
- tokens/customer turn: 9,220.57
- LLM calls/customer turn: 1.806
- provider latency: 429,801 ms
- E2E turn latency: 815,022 ms
- retries: 1
- fallbacks: 0
- actual cost: $0.11930860
- no-cache equivalent: $0.20350260
- cache saving: $0.08419400
- cache saving: 41.37%

## Evaluation tooling validation

- unified agent-eval tooling: **44 passed**
- Batch 7 quick registry/import gate: **16/16**
- Ruff on eval tooling: PASS
- compileall: PASS
- git diff --check: PASS

An earlier local eval-tooling invocation omitted required test environment variables and failed during import/collection. No test assertions executed in that invocation. The corrected CI-style invocation passed 44/44.

## Final verdict

**BATCH 7: NOT CLOSED**

Post-#119 status:
- #115 family: GREEN
- #116 family: GREEN
- #117 family: GREEN
- #119: GREEN / CLOSED
- new #122: OPEN / material P2

Exit criteria:
- P0 = 0 ✅
- P1 = 0 after manual adjudication ✅
- Material P2 = 0 ❌ (currently 1)

Next step:
1. separate focused runtime task for #122,
2. deploy and verify that fix,
3. rerun the full Batch 7 Final Closeout on the post-fix main,
4. do not start Batch 8 until P0=0, P1=0, material P2=0.

PR #120 remains historical evaluation evidence, OPEN / NOT MERGED, and is not treated as the final pass.
