# Tia Agent Evaluation — Batch 06 Final Post-#107 Closeout Review

## Scope and frozen runtime

- Task branch: `eval/agent-batch-06-final-after-107`
- STARTING_MAIN_SHA: `6f651f11def77478ad6b24d5f59f9362f752aebd`
- BATCH6_FINAL_RUNTIME_SHA: `6f651f11def77478ad6b24d5f59f9362f752aebd`
- Production Railway SHA at verification: `6f651f11def77478ad6b24d5f59f9362f752aebd`
- Scenario version: `batch6-v1`
- Fixture version: `batch6-demo-fixtures-v1`
- Eval tooling source: current PR #108 tooling files only
- Historical PR #108 evidence was NOT imported
- Runtime changes: **NONE**
- Prompt/model/business-rule/schema/frontend changes: **NONE**

All 16 official Batch 6 scenarios were executed once against the same frozen runtime SHA after PR #109 / #107. This branch contains evaluation tooling and fresh evidence only.

## Preflight

- Demo workspace: `tia`
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
- Canonical preflight before run: PASS
- Reset needed before run: NO

## Manual adjudication

| Scenario | Classification | Severity | Manual evidence |
|---|---|---:|---|
| S1 | Fully Correct | — | Two sequential services created with one non-null `visit_group_id`; correct doctor/times; no extra write. |
| S2 | Fully Correct | — | External conflict invalidated the originally discussed sequence; confirmation performed a fresh availability read and produced zero partial writes. |
| S3 | Fully Correct | — | Missing device produced grounded clarification, zero writes, and exact bound price facts: Candela Gentle 650 EGP / Prime Lase 550 EGP. |
| S4 | Fully Correct | — | Shared anchor sequenced into 10:00 / 11:15 with one visit group. |
| S5 | Fully Correct | — | Entire existing group rescheduled; originals and replacements retained correct lifecycle/group linkage. |
| S6 | Fully Correct | — | Both members of the standard visit group were cancelled together. |
| S7 | Fully Correct | — | Package-covered component remained `package_prepaid`; the other component remained standard; package usage was scoped correctly. |
| S8 | Fully Correct | — | Historical-payment question produced a reception/payment handoff and zero grouped business writes. |
| S9 | Fully Correct | — | New 4-session Hydrafacial package and first booking were linked correctly with package usage. |
| S10 | Fully Correct | — | Package purchase + grouped A/B booking succeeded atomically; A package-backed, B standard. |
| S11 | Fully Correct | — | Laser component replacement produced Hydrafacial + deep cleansing only; no ghost laser component. |
| S12 | Acceptable | P3 | Device correction remained component-scoped: Hydrafacial device null, laser Prime Lase, one group. The final reply safely preserved the valid sequence rather than treating the ambiguous “10:00” phrase as authority to overlap components. |
| S13 | Fully Correct | — | Side price read used `service_catalog`, preserved both grouped components, and final write created both in one shared group with zero partial write. |
| S14 | Fully Correct | — | Explicit narrowing created only Hydrafacial; removed laser was absent and final acknowledgment explicitly said Hydrafacial only. |
| S15 | Acceptable | P3 | Resource-boundary flow made zero writes and kept the safe alternative sequence, but asked for another confirmation instead of immediately booking the accepted alternative. Flow remained safe and usable. |
| S16 | Fully Correct | — | Canonical state changed after initial read; confirmation performed fresh availability verification and made zero stale writes. |

Totals:
- Scenarios: 16
- Fully Correct: 14
- Acceptable: 2
- Failed: 0
- P0: 0
- P1: 0
- Material P2: 0
- P3: 2
- Scenario-level infrastructure failures: 0

## Closed-finding verification

- F1 canonical single-branch resolution: **PASS**
- #104 grouped continuity / partial-write guard: **PASS**
- S3 missing-device grounded clarification: **PASS**
- S12 component device scoping: **PASS**
- S14 explicit component removal: **PASS**
- #107 canonical device→price binding: **PASS**

### S3 / #107 detailed binding evidence

Official S3 customer-visible response:
- Candela Gentle = **650 EGP**
- Prime Lase = **550 EGP**

Official structured outcome contains explicit bound facts:

```json
"laser_device_options": [
  {
    "device_name": "Candela Gentle",
    "price": "650.00 EGP"
  },
  {
    "device_name": "Prime Lase",
    "price": "550.00 EGP"
  }
]
```

The S3 outcome does not rely on detached device and price lists. No write was attempted.

### #104 / S13 continuity evidence

- Turn 1 grouped components preserved: 2
- Side price read: `service_catalog`
- Side read business writes: 0
- Final created appointments: 2
- Shared non-null `visit_group_id`: yes
- Laser device: Prime Lase
- Standard component device: null
- Partial grouped writes: 0

### S12 scoping evidence

- Hydrafacial `laser_device_key = null`
- Laser component `laser_device_key = prime_lase`
- Laser component `laser_device_name = Prime Lase`
- Both components share one visit group
- Wrong component doctor/device counter: 0

### S14 explicit removal evidence

- Final created appointments: 1
- Retained service: Hydrafacial
- Removed laser component: absent
- Retained component laser device: null
- Ghost component writes: 0

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

The official run created temporary data inside scenario transactions:
- Appointments created inside scenarios: 18
- Patient packages created inside scenarios: 2
- Package usages created inside scenarios: 3
- Handoffs created inside scenarios: 1
- Pulse packs: 0
- Payments: 0
- Pulse usages: 0
- Pulse settlements: 0

Post-run persistence checks:
- Appointments persisted: 0
- Patient packages persisted: 0
- Package usages persisted: 0
- Handoffs persisted: 0
- Pulse packs persisted: 0
- Payments persisted: 0
- Pulse usages persisted: 0
- Pulse settlements persisted: 0
- Temporary patients in official-run creation window: 0
- Post-run canonical Demo preflight: PASS
- Reset needed: NO

## Metrics

- Customer turns: 24
- Interpreter calls: 24
- Responder calls: 23
- Total LLM calls: 47
- Input tokens: 201,745
- Cached-read tokens: 124,890
- Cache-write tokens: 50,697
- Uncached input tokens: 26,158
- Output tokens: 13,954
- Total tokens: 215,699
- Provider latency: 163,155 ms
- Aggregate turn / E2E latency: 372,737 ms
- Retries: 0
- Fallback calls: 0
- Actual cost: $0.03714845
- No-cache equivalent: $0.05709380
- Cache saving: $0.01994535
- Cache saving: 34.93%

## Regression and validation

- Batch 6 tooling tests: **4 passed**
- Unified agent-eval tooling tests: **43 passed**
- Batch 6 quick registry/import gate: **16 / 16**
- Critical F1/#104/#107/compound/state/device/cancellation/Batch3/Batch4 regression union: **224 passed**
- Full backend on clean PostgreSQL: **1621 passed, 4 skipped**
- Clean PostgreSQL Alembic upgrade from zero: PASS
- Ruff — eval tooling: PASS
- Ruff — backend app/tests/alembic: PASS
- compileall: PASS
- `git diff --check`: PASS
- Secret scan: CLEAN
- Alembic single head: `0086_all_service_packages`
- Runtime diff in this evaluation branch: 0

## Production verification

No deployment was performed for this evaluation.

- Production Railway SHA: `6f651f11def77478ad6b24d5f59f9362f752aebd`
- Evaluated runtime SHA: `6f651f11def77478ad6b24d5f59f9362f752aebd`
- Same SHA: **YES**
- `tia-api`: SUCCESS
- `tia-whatsapp-staging`: SUCCESS
- `/api/v1/health/live`: 200 / alive
- `/api/v1/health/ready`: 200 / ready / database connected
- WhatsApp transport: tick 200
- `inbound_failed = 0`
- `send_failed = 0`

## Final verdict

**BATCH 6: CLOSED**

All 16 scenarios were reviewed on one frozen post-#107 runtime. P0 = 0, P1 = 0, material P2 = 0, all atomicity counters = 0, critical safety violations = 0, all previously closed findings remain green, isolation is clean, regressions are green, and production matches the evaluated SHA.

The remaining S12/S15 findings are accepted P3 conversational conservatism and do not justify reopening Batch 6 or patching runtime behavior in this evaluation branch.
