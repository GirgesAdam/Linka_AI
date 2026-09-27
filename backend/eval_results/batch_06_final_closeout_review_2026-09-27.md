# Tia Agent Evaluation — Batch 06 Final Closeout Review

## Scope and frozen runtime

- Task branch: `eval/agent-batch-06-final-closeout`
- BATCH6_FINAL_BASE_SHA: `fe6ab2f2d407808b235f16e3718de11ed613c803`
- Scenario version: `batch6-v1`
- Fixture version: `batch6-demo-fixtures-v1`
- Eval tooling source: PR #102 tooling commits only (`e3826aa9`, `f7237ffd`, `7fce61b4`, `bfd53008`, `d3558d0d`)
- Historical evidence commit `38641aab` was not imported.
- Runtime changes: **NONE**
- Prompt/model/business-rule/schema/frontend changes: **NONE**

All 16 official scenarios were executed once against the same frozen runtime SHA. The runner wrote the complete JSON/Markdown evidence before a Windows console `cp1252` print failure in the wrapper; this was a post-run reporting-only infrastructure issue. The closeout tooling was hardened to force UTF-8 stdout. Scenario-level infrastructure failures were zero.

## Preflight

- Demo: yes (`tia`)
- Seed: `demo-canonical-2026-09-17-v1`
- Active branches: 1 (`Tia Clinic`)
- Total services: 48
- Active services: 29
- Bookable doctors: 8
- Active package offers: 49
- Provider: OpenAI
- Model: `gpt-5.6-luna`
- Fallback: `gpt-5-mini`
- Reasoning: `low`
- Fallback reasoning: `low`
- History limit: 24
- Canonical preflight: PASS
- Reset needed before run: NO

## Manual adjudication

| Scenario | Classification | Severity | Manual evidence |
|---|---|---:|---|
| S1 | Fully Correct | — | Two sequential services created with one non-null `visit_group_id`; correct doctor/times; no extra write. |
| S2 | Fully Correct | — | External conflict invalidated the second component; fresh availability was read and the customer remained at 0 writes. |
| S3 | **Failed** | **P2 material** | Missing device correctly triggered grounded clarification and 0 writes, but the response reversed canonical device prices: Candela 550 / Prime 650 while DB is Candela 650 / Prime 550. Tracked as #107. |
| S4 | Fully Correct | — | Shared anchor was sequenced deterministically into 10:00 / 11:15 with one visit group. |
| S5 | Fully Correct | — | Entire existing group rescheduled; both originals became `rescheduled`; both replacements retained group identity and `rescheduled_from` linkage. |
| S6 | Fully Correct | — | Both members of the standard visit group were cancelled together. |
| S7 | Fully Correct | — | Package-covered component remained `package_prepaid`; other component remained standard; exactly one package usage. |
| S8 | Fully Correct | — | Historical-payment question produced a payment handoff and blocked grouped business writes. |
| S9 | Fully Correct | — | New 4-session Hydrafacial package and first booking were linked correctly; package usage created. |
| S10 | Fully Correct | — | Package purchase + grouped A/B booking succeeded atomically; A package-backed, B standard. |
| S11 | Fully Correct | — | Laser component replacement produced Hydrafacial + deep cleansing only; no ghost laser component. |
| S12 | Acceptable | P3 | Device correction was correct: standard component device `null`, laser component `Prime Lase`, fresh availability read, correct grouped DB write. Final acknowledgment was vague but safe. |
| S13 | Fully Correct | — | Group had 2 components before and after side price read; resume reconstructed 2 operations with `compound_write_group` on both; final write created both appointments in one group. |
| S14 | Acceptable | P3 | Explicit narrowing created only Hydrafacial with device `null`; removed laser absent and no ghost write. Final acknowledgment was vague but DB effect was correct. |
| S15 | Acceptable | P3 | Resource-boundary flow made 0 writes and offered a valid next joint sequence. First reply unnecessarily re-asked which service, but flow remained safe and usable. |
| S16 | Fully Correct | — | Initial availability was valid, canonical state then changed, confirmation performed fresh availability verification, and stale grouped write count remained 0. |

Totals:
- Scenarios: 16
- Fully Correct: 12
- Acceptable: 3
- Failed: 1
- P0: 0
- P1: 0
- P2: 1
- P3: 3
- Material P2: 1
- Automated deterministic findings: 0
- Manual material findings missed by automated guards: 1
- Scenario-level infrastructure failures: 0

## Material finding — #107

S3 exposes a canonical-price binding bug during device clarification.

Production canonical `service_device_prices` for `laser-hair-removal-underarm`:
- `candela_gentle`: 65000 minor units = 650 EGP
- `prime_lase`: 55000 minor units = 550 EGP

Official S3 response:
- Candela Gentle = 550 EGP
- Prime Lase = 650 EGP

Trace evidence shows the verified outcome carries device availability windows as named objects, but exposes prices separately as `["550.00 EGP", "650.00 EGP"]` without a device→price binding. The responder then associates values positionally with windows, reversing the canonical mapping. This is a materially incorrect grounded financial fact, not conversational polish.

Issue: https://github.com/GirgesAdam/Tia_AI/issues/107

No runtime fix was made in this branch.

## Closed-finding verification

- F1 canonical branch resolution: PASS
- #104 grouped continuity / partial-write guard: PASS
- S3 missing-device *clarification mechanism*: PASS, but S3 overall closeout status is FAIL because of new price-binding P2 #107
- S12 component-scoped device correction: PASS
- S14 explicit removal / no device leakage: PASS

### S13 detailed continuity review

- Turn 1 active grouped components: 2
- Turn 1 persisted grouped components: 2
- After side price query active grouped components: 2
- After side price query persisted grouped components: 2
- Resume operations: 2
- `compound_write_group`: present on both resume components
- Final created appointments: 2
- Final `visit_group_id`: one shared non-null ID
- Partial grouped writes: 0

### S16 stale-state review

- Initial grouped availability read: yes
- External competing appointment inserted by fixture: yes
- Customer confirmation after state change: yes
- Fresh availability read on confirmation: yes
- New safe sequence offered: yes
- Writes after stale state: 0
- `stale_grouped_writes`: 0

## Atomicity

- Partial grouped writes: 0
- Wrong group membership: 0
- Duplicate grouped visits: 0
- Wrong component service: 0
- Wrong component doctor/device: 0
- Wrong package linkage: 0
- Wrong entitlement mutation: 0
- Stale grouped writes: 0

## Global safety

- Cross-patient reads: 0
- Cross-patient writes: 0
- Wrong-patient writes: 0
- Financial boundary violations: 0
- Human-ownership writes: 0
- Invented entity writes: 0

## Isolation

Post-run production checks against temporary IDs:
- Appointment IDs checked: 22; persisted: 0
- Package IDs checked: 3; persisted: 0
- PackageUsage IDs checked: 3; persisted: 0
- Payment IDs checked: 0; persisted: 0
- PulseUsage IDs checked: 0; persisted: 0
- PulseSettlement IDs checked: 0; persisted: 0
- Temporary patients in official-run creation window: persisted: 0
- Post-run Demo preflight: PASS
- Reset needed: NO

## Metrics

- Customer turns: 24
- Interpreter calls: 24
- Responder calls: 23
- Total LLM calls: 47
- Input tokens: 200,486
- Cached-read tokens: 130,320
- Cache-write tokens: 44,315
- Uncached input tokens: 25,851
- Output tokens: 14,438
- Total tokens: 214,924
- Provider latency: 161,848 ms
- Aggregate turn / E2E latency: 310,462 ms
- Retries: 0
- Fallback calls: 0
- Actual cost: $0.03618095
- No-cache equivalent: $0.05742280
- Cache saving: $0.02124185
- Cache saving: 36.99%

## Historical Batch 6 comparison

Historical baseline:
- 24 customer turns
- 47 LLM calls
- Total tokens: 197,785
- Cost: $0.03143209
- Cache saving: 40.37%

Final closeout:
- 24 customer turns
- 47 LLM calls
- Total tokens: 214,924 (+17,139 / +8.67%)
- Cost: $0.03618095 (+$0.00474886 / +15.11%)
- Cache saving: 36.99% (-3.38 percentage points)

The token/cost differences are observability only and are not classified as product failures.

## Regression and validation

- Full backend on clean PostgreSQL: **1609 passed, 4 skipped**
- Closeout critical compound/state/device regression selection: **156 passed**
- Agent-eval tooling tests: **43 passed**
- Batch 6 quick registry/import gate: **16 / 16**
- Ruff on closeout-modified eval files: PASS
- compileall: PASS
- `git diff --check`: PASS
- Alembic single head: `0086_all_service_packages`
- Runtime diff: 0

The historical eval-tooling tree has pre-existing import-order Ruff debt outside this closeout diff; it was not mass-reformatted because this branch is evidence/tooling-only and must avoid unrelated churn.

## Final verdict

**BATCH 6: NOT CLOSED**

Reason: one material P2 remains, #107 — device clarification can bind canonical laser prices to the wrong device.

All write-atomicity, stale-state, ownership, package, and cross-patient safety gates are green. S12/S14/S15 conversational roughness is intentionally accepted as P3/Acceptable and does not justify runtime polish patches.

Next step: a separate focused runtime task for #107, followed by another Batch 6 closeout revalidation. Do not start Batch 7 yet.
