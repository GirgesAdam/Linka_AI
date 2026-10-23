# Tia Agent Evaluation — Batch 07 Post-#119 Follow-up Adjudication

## Scope

- Runtime / production SHA: `e8923a9c01213b15f115869267c34760d16f8fbe`
- Scenario version: `batch7-v1`
- Stabilized fixture version: `batch7-demo-fixtures-v2`
- Runtime changes in this evaluation branch: **NONE**
- Prompt/model/provider/reasoning/schema/frontend changes: **NONE**

This follow-up supersedes the interpretation of the earlier post-#119 closeout evidence where S11's nearest source appointment could drift into the configured cancellation notice window as wall-clock time advanced.

## #119 status

#119 is fixed and closed by PR #121.

Production lineage:
- focused fix commit: `1570a27b0e8373067ddfa1e7e62e7d689f9c43c5`
- merged main / production SHA: `e8923a9c01213b15f115869267c34760d16f8fbe`

Verified behavior:
- persisted reschedule target identity remains authoritative
- selected slot supplies destination scheduling fields only
- slot identity cannot silently override the persisted target
- missing/conflicting target remains fail-closed
- S10 post-fix full closeout: PASS
- wrong-target writes: 0
- duplicate lifecycle writes: 0

## Evaluation-tooling stabilization

### S11 cancellation-window fixture

Canonical cancellation notice is 720 minutes (12 hours).

The original S11 fixture selected the nearest future slot. As the evaluation crossed midnight, the selected appointment could fall inside the 12-hour notice window and correctly require staff review, which no longer exercised the intended "cancel A, then later reschedule B" path.

Fixture v2 chooses S11's source appointment on a later calendar day, safely outside that moving notice boundary.

Validation:
- dedicated fixture unit test: PASS
- stabilized S11 targeted repetitions: 3/3 business-correct
- each repetition: A cancelled, B rescheduled exactly once, no wrong-target/duplicate/stale/financial writes

### S13 detector

The original detector assigned:
`active_after_cancel = evidence[-1].active_task_after`

But the canonical cancellation occurs before turn 4. Runtime correctly invalidated the stale reschedule task on turn 4:
- `active_task_invalidated=true`
- `canonical_target_non_actionable=true`
- `active_task_cancelled=true`
- active task immediately after turn 4: null
- source appointment remained cancelled
- replacements for source: 0

Turn 5 may legitimately begin a new booking task. The old detector incorrectly counted that unrelated new task as the stale reschedule task.

The detector now checks the task immediately after the canonical-cancellation follow-up and only flags a reschedule task that still targets the cancelled source appointment.

This is evaluation-tooling-only; no runtime patch was made.

## Full post-#119 run with fixture v2

Run metadata:
- runtime/base SHA: `e8923a9c01213b15f115869267c34760d16f8fbe`
- fixture: `batch7-demo-fixtures-v2`
- scenarios: 16
- customer turns: 93
- infrastructure failures: 0

Raw run generated at:
`2026-09-27T22:07:28.831954Z`

Automated result before S13 detector adjudication:
- P0: 0
- P1: 0
- P2: 1 (S13 detector false positive)

Manual S13 adjudication:
- stale reschedule task after canonical invalidation: **NO**
- stale lifecycle write: 0
- wrong appointment write: 0
- source replacement rows: 0
- later new booking task is unrelated to the invalidated reschedule task

Therefore the S13 automated P2 is an evaluator false positive, not a runtime material finding.

All other scenarios in that run, including S10/#119 and S11, had no automated material issue.

## Outstanding material finding — #122

Issue #122 remains open:
**exact-date cancellation can remain pending and leave appointment active**

After moving S11 outside the 12-hour cancellation window, one valid targeted run still reproduced this family:
- explicit exact-date cancellation of A did not bind/complete the unique canonical target
- A remained confirmed
- later B reschedule targeted B correctly
- no wrong-target or duplicate write occurred

Subsequent validation on the same runtime/fixture:
- S11 repetitions: 3/3 PASS
- full fixture-v2 run: S11 PASS

This means #122 is **intermittent**, not disproven.

Because a material business-flow failure was reproduced on a valid stabilized fixture, it remains a material P2 until separately fixed and revalidated.

No #122 runtime patch was made in this evaluation branch.

## State / safety from the fixture-v2 full run

- stale_service_carryovers: 0
- stale_doctor_carryovers: 0
- stale_device_carryovers: 0
- stale_date_time_carryovers: 0
- stale_package_carryovers: 0
- unexpected_task_restart: 0
- unexpected_task_loss: 0
- duplicate_writes: 0
- stale_lifecycle_writes: 0
- wrong_appointment_writes: 0
- side_read_business_writes: 0
- financial_boundary_violations: 0
- human_ownership_writes: 0
- invented_entity_writes: 0

The raw `wrong_active_task_target=1` came only from the superseded S13 detector described above; manual trace proves the stale reschedule task was cleared at the correct turn.

## Metrics — fixture-v2 full run

- input tokens: 821,761
- cached-read tokens: 504,990
- cache-write tokens: 136,415
- uncached input tokens: 180,356
- output tokens: 31,930
- total tokens: 853,691
- customer turns: 93
- interpreter calls: 93
- responder calls: 74
- total LLM calls: 167
- tokens/customer turn: 9,179.47
- LLM calls/customer turn: 1.796
- provider latency: 437,988 ms
- E2E turn latency: 744,171 ms
- retries: 0
- fallbacks: 0
- actual cost: $0.11859075
- no-cache equivalent: $0.20266820
- cache saving: $0.08407745 / 41.49%

## Validation

Focused #119 runtime validation (PR #121):
- targeted regression: 143 passed
- full backend clean PostgreSQL: 1650 passed, 4 skipped
- clean migrations: PASS
- CI backend: SUCCESS
- CI frontend/shared: SUCCESS

Post-#119 eval tooling:
- unified eval tooling before detector update: 45 passed
- Batch 7 quick gate: 16/16
- S11 stabilized repetitions: 3/3 PASS
- Ruff/compileall/diff-check: PASS before detector-only follow-up
- CI on this updated eval branch is the final source of truth for the detector/tooling update

## Production

Production remains unchanged by this evaluation work:
- Railway runtime SHA: `e8923a9c01213b15f115869267c34760d16f8fbe`
- `/api/v1/health/live`: 200
- `/api/v1/health/ready`: 200 / DB connected
- WhatsApp transport: tick 200, inbound_failed=0, send_failed=0
- Vercel deployment: not required

## Final verdict

**BATCH 7 = NOT CLOSED**

Status:
- #115 family: GREEN
- #116 family: GREEN
- #117 family: GREEN
- #119: GREEN / CLOSED
- S13 closeout P2: evaluator false positive
- #122: OPEN / intermittent material P2

Exit criteria:
- P0 = 0 ✅
- P1 = 0 ✅
- Material P2 = 0 ❌ — #122 remains

Next step:
1. focused runtime review/fix for #122 only,
2. deploy exact merged SHA,
3. rerun full Batch 7 Final Closeout,
4. do not start Batch 8 until P0=0, P1=0, material P2=0.
