# Tia Agent Evaluation — Batch 07 Final Closeout Review

## Scope and frozen runtime

- Final closeout branch: `eval/agent-batch-07-final-closeout`
- BATCH7_FINAL_RUNTIME_SHA: `10dd366d99894c509bd566d1ad33321058b311ab`
- Production runtime SHA at closeout: `10dd366d99894c509bd566d1ad33321058b311ab`
- Scenario version: `batch7-v1`
- Fixture version: `batch7-demo-fixtures-v1`
- Runtime changes in this branch: **NONE**
- Prompt/model/business-rule/schema/frontend changes in this branch: **NONE**

Focused runtime fixes completed before this closeout:
- Fix 1 — PR #115, merged production lineage `d28a864dfc63e95dd860a9ce008ebd566f54cfcb`
- Fix 2 — PR #116, merged production lineage `595983e5b74ae6bdf892ad62c6d55c17a83ba14a`
- Fix 3 — PR #117, merged production lineage `10dd366d99894c509bd566d1ad33321058b311ab`

## Preflight

- Canonical Demo preflight before official run: PASS
- Reset needed: NO
- Seed: `demo-canonical-2026-09-17-v1`
- Active branch count: 1
- Services total: 48
- Services active: 29
- Bookable doctors: 8
- Package offers: 49

## Stability gate before full closeout

Repeated on the final runtime SHA:
- S9: 3/3 PASS
- S12: 3/3 PASS
- S13: 3/3 PASS
- S16: 3/3 PASS

Across all 12 stability runs:
- wrong_active_task_target = 0
- unexpected_task_restart = 0
- unexpected_task_loss = 0
- duplicate_writes = 0
- stale_lifecycle_writes = 0
- wrong_appointment_writes = 0
- side_read_business_writes = 0
- financial_boundary_violations = 0
- human_ownership_writes = 0
- invented_entity_writes = 0

## Official full closeout execution

- Scenarios: 16
- Customer turns: 93
- Infrastructure failures: 0
- Automated deterministic P0: 0
- Automated deterministic P1: 1
- Automated deterministic P2: 0
- Automated P1 manifestation: S10
- All aggregate state-continuity/safety counters: 0

The automated S10 issue title says the persisted target switched to another similar appointment. Manual trace/DB adjudication shows that title overstates the observed manifestation: the target did **not** switch and no wrong appointment was mutated. The actual failure is a fail-closed loss of the persisted target appointment ID at the final slot-selection write boundary. It is manually classified as **material P2** rather than P1.

## Manual adjudication

| Scenario | Classification | Severity | Manual evidence |
|---|---|---:|---|
| S1 | Fully Correct | — | Booking survived doctor and price side reads; exactly one canonical Prime Lase booking. |
| S2 | Fully Correct | — | Repeated doctor/date/time corrections invalidated stale scheduling facts; final booking used the latest doctor/date/time only. |
| S3 | Fully Correct | — | Candela→Prime correction survived side reads; final write and price were Prime-only and grounded. |
| S4 | Fully Correct | — | Replacing laser with Hydrafacial cleared laser-only state; final write had no device leakage. |
| S5 | Fully Correct | — | Standard→laser introduced the device requirement; no write occurred before Prime Lase was grounded. |
| S6 | Fully Correct | — | Package/financial side reads remained read-only; financial interruption did not create business writes; booking resumed safely. |
| S7 | Acceptable | P3 | Package purchase and package-backed booking were correct. Initial package-list response was unnecessarily unhelpful but did not block the flow. |
| S8 | Fully Correct | — | External package invalidation won over stale entitlement state; no stale package usage/write. |
| S9 | Fully Correct | — | Completed reschedule was not re-executed after acknowledgment; replacement lineage remained singular. |
| S10 | **Failed** | **P2 material** | Exact appointment A target persisted through detours, but final `select_active` reschedule write dropped `appointment_id`, failed closed, and unnecessarily asked for the booking number. Tracked as #119. |
| S11 | Acceptable | P3 | Cancel A then reschedule B produced the correct DB effects. Final “كملي” acknowledgment was conversationally rough but caused no duplicate or wrong-target write. |
| S12 | Fully Correct | — | Canonical Reception edit was respected; follow-up confirmation did not create a replacement-of-replacement. |
| S13 | Fully Correct | — | External cancellation invalidated the active reschedule task; no stale lifecycle write occurred. |
| S14 | Fully Correct | — | Explicit abandonment cleared booking A; new laser booking B started clean. |
| S15 | Fully Correct | — | Ambiguous side intent did not replace the active Hydrafacial booking; original task resumed correctly. |
| S16 | Fully Correct | — | Completed booking identity survived long informational detours; repeated confirmation revalidated the canonical appointment and did not restart or duplicate. |

Totals:
- Fully Correct: 13
- Acceptable: 2
- Failed: 1
- P0: 0
- P1 after manual adjudication: 0
- Material P2: 1
- P3: 2

## Material finding — #119

Issue: https://github.com/GirgesAdam/Tia_AI/issues/119

Scenario S10 creates two similar canonical Hydrafacial appointments. Appointment A is selected for reschedule. The persisted active task correctly retains A's exact `appointment_id` across:
- the initial appointment read,
- doctor side read,
- date correction,
- price side read.

On turn 5 (“خليه الساعة 10:00”), the interpreter emits a structured `select_active` continuation and the active task still contains appointment A.

The first incorrect layer is:
`backend/app/services/agent_v2/planner.py::_plan_select_active`

For `purpose == "reschedule_slot"`, it creates the write intent using:
`parameters=dict(selected.payload)`

The selected slot payload contains the replacement slot/service/doctor/time facts but not the lifecycle target `appointment_id`. Consequently, write execution correctly fails closed with:
`Verified write is missing appointment_id.`

Observed DB result in the official closeout:
- appointment A: still confirmed
- appointment B: still confirmed
- replacements for A: 0
- replacements for B: 0
- wrong appointment writes: 0

Therefore this is a **material P2 blocked-flow failure**, not a wrong-target P1 write.

Targeted S10 repetitions on the same frozen runtime after the finding:
- 3/3 PASS
- each correctly rescheduled A
- B remained confirmed
- exactly one replacement for A
- no duplicate/wrong-target write

The finding is therefore intermittent, consistent with semantic-path variance into the vulnerable `select_active` branch.

No runtime fix was made in this closeout branch.

## Closed-family verification

### Fix 1 — completed reschedule dedupe
- S9 official closeout: PASS
- S12 official closeout: PASS
- S9 stability: 3/3 PASS
- S12 stability: 3/3 PASS
- duplicate_writes: 0
- stale_lifecycle_writes: 0

### Fix 2 — canonical lifecycle invalidation
- S13 official closeout: PASS
- S13 stability: 3/3 PASS
- wrong_active_task_target: 0
- stale lifecycle writes: 0

### Fix 3 — completed booking identity across detours
- S16 official closeout: PASS
- S16 stability: 3/3 PASS
- unexpected_task_restart: 0
- duplicate_writes: 0

## State continuity and safety

Aggregate official closeout counters:
- stale_service_carryovers: 0
- stale_doctor_carryovers: 0
- stale_device_carryovers: 0
- stale_date_time_carryovers: 0
- stale_package_carryovers: 0
- wrong_active_task_target: 0
- unexpected_task_restart: 0
- unexpected_task_loss: 0
- duplicate_writes: 0
- stale_lifecycle_writes: 0
- wrong_appointment_writes: 0
- side_read_business_writes: 0
- financial_boundary_violations: 0
- human_ownership_writes: 0
- invented_entity_writes: 0
## Isolation

- Post-run canonical Demo preflight: PASS
- Reset needed: NO
- No scenario infrastructure failures
- Evaluation remained transaction/rollback isolated
- No intentional production persistence from Batch 7 evaluation

## Metrics

Official final closeout:
- Customer turns: 93
- Interpreter calls: 95
- Responder calls: 74
- Total LLM calls: 169
- Input tokens: 841,903
- Cached-read tokens: 515,850
- Cache-write tokens: 130,383
- Uncached input tokens: 195,670
- Output tokens: 32,779
- Total tokens: 874,682
- Tokens/customer turn: 9,405.18
- LLM calls/customer turn: 1.817
- Provider latency: 453,298 ms
- E2E latency: 759,372 ms
- Retries: 2
- Fallbacks: 0
- Actual cost: $0.12138155
- No-cache equivalent: $0.20771540
- Cache saving: $0.08633385
- Cache saving: 41.56%

## Regression and validation

- Clean PostgreSQL migration from zero: PASS
- Full backend on clean PostgreSQL: **1646 passed, 4 skipped**
- Unified agent-eval tooling: **44 passed**
- Batch 7 quick registry/import gate: **16 / 16**
- Ruff on eval tooling: PASS
- compileall: PASS
- `git diff --check`: PASS
- Alembic single head: `0086_all_service_packages`
- Runtime diff in closeout branch: 0

One earlier local full-backend invocation pointed PostgreSQL-specific tests at the wrong local port (5432) and produced connection-auth failures. This was an invocation/configuration error, not a code failure. The corrected clean-PostgreSQL run against the intended disposable database passed 1646/4.

## Production verification

At closeout:
- production/main SHA: `10dd366d99894c509bd566d1ad33321058b311ab`
- Railway `tia-api`: SUCCESS on the same SHA
- Railway `tia-whatsapp-staging`: SUCCESS on the same SHA
- `/api/v1/health/live`: 200 / alive
- `/api/v1/health/ready`: 200 / ready / database connected
- WhatsApp transport ticks: 200
- inbound_failed: 0
- send_failed: 0
- Vercel deployment: not required; no frontend changes

## Final verdict

**BATCH 7: NOT CLOSED**

The three originally identified root-cause families are fixed and stable:
- Fix 1: GREEN
- Fix 2: GREEN
- Fix 3: GREEN

However, one new material P2 remains:
- #119 — reschedule slot selection can drop the persisted target `appointment_id` and block completion.

Because material P2 > 0, the Batch 7 Definition of Done is not satisfied.

Next step:
1. separate focused runtime task for #119,
2. deploy and verify that focused fix,
3. rerun Batch 7 final closeout,
4. do not start Batch 8 until P0=0, P1=0, material P2=0.
