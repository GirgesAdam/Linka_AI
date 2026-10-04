# Agent Booking Journey Contract — 2026-10-04

## Scope and safety boundary

This closeout covers PR #201 only. The reviewed implementation head was `7267c754a22c75ba85501f6519a3ac26a0047cfc` against `main` `b9ded0797e02e025c5ce320926b9ab2affd276a3`. No merge, deployment, or Ready-for-Review transition was performed while producing this evidence.

The accepted booking direction remains:

`laser: service -> device + verified price -> date/time -> doctor only when needed -> verified write`

For every normal paid standalone service, laser or non-laser, a customer-visible verified commercial basis must precede a booking write. Existing-package use is a distinct commercial path: package coverage/session truth is shown instead of a misleading cash price.

## Before evidence — Shared CI Run 1903

Exact reviewed PR head: `7267c754a22c75ba85501f6519a3ac26a0047cfc`.

- frontend: SUCCESS
- backend: FAILURE
- backend tests: `7 failed, 2133 passed, 4 skipped`
- agent-eval tooling: PASS (`39 passed`)
- Ruff: PASS
- compileall: PASS
- Alembic single head: PASS (`0089_dynamic_laser_device_references`)
- clean PostgreSQL migration: PASS

The seven backend failures were exactly the four stale `DD/MM/YYYY` assertions, two compound atomicity crashes from a missing `response_disposition`, and the grouped-booking continuity regression.

## Fixes applied after review

1. Customer-facing dates keep the weekday-aware contract (`الثلاثاء 25 أغسطس`, `الأربعاء 9 سبتمبر`, `الخميس 20 أغسطس`); stale tests were updated instead of restoring numeric dates.
2. `_terminal_no_reply_allowed` now fails closed when `response_disposition` is missing/unknown. Any present `automation_context`, including an empty context object supplied by the automation path, also blocks terminal silence.
3. The single-service commercial/device gate is bypassed by grouped/compound write groups, preserving grouped persistence, side reads, reload/resume, write grouping, and transaction atomicity.
4. A first booking message that already contains service/device/date or service/device/date/time performs `service_catalog + availability` in the same turn. It never asks to continue on an unverified “appointment”.
5. Commercial presentation state is keyed by `service + optional device + commercial path`, not device alone. Standalone non-laser bookings therefore receive the same price-before-write invariant as laser bookings.
6. `use_existing` is a separate `package` commercial path. The runtime verifies owned package coverage and remaining sessions before recording the commercial basis; no standalone cash price is presented as package truth.
7. Booking slot selection also checks the commercial-basis invariant before write. Dependency changes clear the commercial presentation key.

## Deterministic regression evidence

Final combined focused booking/package/device/grouped + historical/Task controls: `371 passed`.

A narrower commercial/state/renderer verification pass also completed at `110 passed`; Ruff over `app tests alembic`: PASS; compileall over `app alembic tests`: PASS; Alembic reports the single head `0089_dynamic_laser_device_references`.

Agent-eval tooling tests under CI-like environment: `39 passed`.

The grouped continuity regression now explicitly asserts that both resumed steps retain `present_availability`, both retain write intents, and both retain the same compound write group. Compound atomicity tests pass without adding `response_disposition` to their legacy test doubles, proving the helper itself is robust.

## Actual runtime methodology

J1–J14 evidence below came from the current working-tree code through `tools/agent_eval/run_fresh_task_lifecycle.py`, using `railway run --service tia-agent-eval` only to supply the existing evaluation environment. This did **not** deploy code or mutate production deployment state. Each runtime result was captured as JSON; the replies below are actual runtime replies, not handwritten expectations.

### Key before/after evidence

**J2 — multi-constraint first message**

Customer: `عايز Under Arm على DEKA يوم الأحد الساعة 5`

After fix, the same turn performed verified reads `service_catalog + availability` and replied:

> جلسة Under Arm على DEKA Again سعرها 550 جنيه. الوقت المطلوب مش متاح.

No write was attempted. The old avoidable `show price -> ask to continue -> only then discover availability` turn is gone.

**Task 1B automation no-reply guard**

R5 injects an appointment-reminder automation with content `فكرك بميعادك النهاردة.` after a completed booking. Customer then says `تمام`. Actual reply was `تمام 😊`, not silence. This is the required fail-closed behavior when automation context exists.

**J11 — ordinary terminal closing**

After a normal price read, customer `شكرا` produced `reply=None`, no read, and no write. Ordinary terminal silence therefore still works outside automation context.

## J1–J14 runtime metrics

| Journey | Customer turns | Assistant turns | Avoidable turns | Repeated questions | Repeated confirmations | Largest option list | Commercial truth before booking | Device before broad availability | Date formatting | Doctor question necessity | Writes | Write correctness | Terminal behavior | Result |
|---|---:|---:|---:|---:|---:|---:|---|---|---|---|---:|---|---|---|
| J1 simple laser + closing | 6 | 5 visible + 1 silence | 0 | 0 | 0 | 2 devices / 2 doctor windows | Yes, DEKA 550 shown before write | Yes | `الخميس 8 أكتوبر` | Exact 4pm had doctor ambiguity; doctor selection was necessary | 1 | Correct booking only | Final `تمام شكرا` -> no reply | ACCEPTABLE |
| J2 service/device/date/time together | 1 | 1 | 0 | 0 | 0 | 0 | Yes, 550 shown | Yes | date was verified in same turn | No doctor question because requested time was unavailable | 0 | No write, correctly | Reply explains unavailability | PASS |
| J3 single device | 1 | 1 | 0 | 0 | 0 | 0 | Yes, single DEKA auto-resolved at 550 | Yes, auto-resolved | N/A in turn | Not asked | 0 | No write | Normal reply | PASS |
| J4 device correction | 3 | 3 | 0 | 0 | 0 | 2 devices | Yes; corrected DEKA 550 -> Candela 650 | Yes | N/A | Not asked | 0 | No write | Normal reply | PASS |
| J5 date correction | 4 | 4 | 0 | 0 | 0 | 2 devices / 2 windows | Yes | Yes | `الخميس 8 أكتوبر`, then `السبت 10 أكتوبر` | Only availability-derived doctors shown | 0 | No write | Normal reply | PASS |
| J6 side pricing/address questions | 5 | 5 | 0 | 0 | 0 | 2 devices | Yes, Full Body / DEKA 3500 preserved | Yes | Date still missing, correctly requested after side reads | Not asked early | 0 | No write | Side reads preserve booking task | PASS |
| J7 +2 days resume | 3 | 3 | 0 | 0 | 0 | 2 devices | Yes | Yes | `السبت 10 أكتوبر` | No unnecessary doctor question | 0 | No write | Active booking resumed | PASS |
| J8 near 7-day expiry | 3 | 3 | 0 | 0 | 0 | 2 devices | Yes | Yes | `السبت 10 أكتوبر` | No unnecessary doctor question | 0 | No write | Resume still valid near TTL | PASS |
| J9 expired -> fresh booking | 3 | 3 | 0 | 0 | 0 | 2 devices | Yes for old task and fresh Full Body task | Yes | N/A | No stale doctor inheritance | 0 | No write | New task starts clean after expiry | PASS |
| J10 completed -> new booking | 6 | 6 | 0 | 0 | 0 | 2 devices / 2 windows | Yes before first write; fresh Full Body commercial truth shown after completion | Yes | `الخميس 8 أكتوبر` | 4pm ambiguity required doctor disambiguation | 1 | Correct first booking; later request starts fresh | Booking completion reply, then fresh task | PASS |
| J11 terminal no-reply | 2 | 1 visible + 1 silence | 0 | 0 | 0 | 2 devices | Price truth only; no booking write | N/A | N/A | N/A | 0 | No write | `شكرا` -> no reply | PASS |
| J12 Task 1B automation controls | R1–R9 aggregate | Actual runtime aggregate | 0 on required automation invariant | 0 material | 0 material | scenario-dependent | Commercial gates preserved in booking controls | Yes where laser booking applies | weekday-aware in booking/reschedule paths | Only when semantic/runtime ambiguity requires it | R5=0; R6=1 reschedule; R9=1 booking | Correct targeted writes | R5 reminder `تمام` -> `تمام 😊`, never terminal silence | PASS |
| J13 no availability -> forward continuation | 4 | 4 | 0 | 0 | 0 | 4 forward windows | Yes, DEKA 550 shown first | Yes | `الأحد 4 أكتوبر` then `الاثنين 5 أكتوبر` | No premature doctor question | 0 | No write | No-availability followed by verified forward search | PASS |
| J14 doctor ambiguity only when necessary | 4 | 4 | 0 | 0 | 0 | 2 devices / 2 windows | Yes, DEKA 550 shown first | Yes | `الخميس 8 أكتوبر` | Exact 5pm resolved without asking customer for doctor | 1 | Correct booking | Booking completed | PASS |

## Selected actual runtime transcripts

### J1

1. `عايز احجز Under Arm` -> device prices: Candela 650 / DEKA 550.
2. `DEKA` -> DEKA 550, asks for date.
3. `الخميس` -> verified Thursday availability with weekday date.
4. `الساعة 4 مساء` -> asks confirmation in the ambiguous doctor context.
5. `مريم` -> `تمام، حجزك اتأكد.`; one booking write.
6. `تمام شكرا` -> no reply.

### J6

Side price and clinic-address reads do not destroy the active booking. After `بالمناسبة الجلسة بكام؟` and `والعنوان فين؟`, `تمام كمل الحجز` correctly returns to the missing booking field: `تحب تحجز يوم إيه؟`.

### J10

A completed Under Arm booking produced exactly one write. Two hours later `عايز احجز Full Body كمان` started a clean new booking and presented Full Body device prices, with no stale service/device/date inheritance.

### J13

`بكرة` returned no availability for `الأحد 4 أكتوبر`; `طب أقرب يوم؟` then performed a fresh verified availability read and returned Monday `5 أكتوبر` windows. No invented availability and no stale presentation cursor were observed.

### J14

After verified Thursday windows, customer requested `الساعة 5`; runtime completed the booking directly without forcing a doctor question, proving doctor choice is requested only when the verified slot remains ambiguous.

## Final acceptance gate status

The exact final-head Shared CI result is intentionally filled only after the final commit is pushed and GitHub checks that exact SHA. Until that succeeds, PR #201 remains Draft / CHANGES REQUIRED and must not be merged or deployed.
