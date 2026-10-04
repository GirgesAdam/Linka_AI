# Agent Booking Journey Contract — 2026-10-04

## Scope and safety boundary

This closeout covers PR #201 only. The original reviewed head was `7267c754a22c75ba85501f6519a3ac26a0047cfc` against `main` `b9ded0797e02e025c5ce320926b9ab2affd276a3`.

The final runtime implementation head for the last customer-facing presentation fix is:

`6d2ea1d7663a68ab8811af6d8687bae281fad4e7`

No merge, deploy, or Ready-for-Review transition was performed while producing this evidence. PR #201 remains Draft.

The accepted booking direction remains:

`laser: service -> device + verified commercial truth -> date/time -> doctor only when needed -> verified write`

For every normal paid standalone service, laser or non-laser, customer-visible verified commercial truth must precede a booking write. Existing-package use is a distinct commercial path: package coverage/session truth is shown instead of a misleading cash price.

## BEFORE evidence — Shared CI Run 1903

This section is historical before-fix evidence, not current status.

Exact reviewed PR head: `7267c754a22c75ba85501f6519a3ac26a0047cfc`.

- frontend: SUCCESS
- backend: FAILURE
- backend tests: `7 failed, 2133 passed, 4 skipped`
- agent-eval tooling: PASS (`39 passed`)
- Ruff: PASS
- compileall: PASS
- Alembic single head: PASS (`0089_dynamic_laser_device_references`)
- clean PostgreSQL migration: PASS

The seven backend failures were the four stale numeric-date assertions, two compound atomicity crashes caused by missing `response_disposition`, and the grouped-booking continuity regression.

## Intermediate green closeout — head a15bb1a

Head `a15bb1a150a5fcd3b009864f3624979a81201827` closed the earlier review blockers.

Shared CI Run 1904: **SUCCESS**

- frontend: SUCCESS
- backend: SUCCESS
- full backend: `2153 passed, 4 skipped`
- agent-eval tooling: `39 passed`
- Ruff: PASS
- compileall: PASS
- Alembic single head: PASS
- clean PostgreSQL migration: PASS

At this point the only remaining product gap was customer-facing presentation for `service + device + date` when verified availability had already been read but the reply still said only that availability existed.

## Final presentation fix — date supplied without exact time

The price/device deterministic response path now reuses the verified availability presentation primitives instead of discarding already-read windows.

Contract after the final fix:

- `service + device + date` -> verified price + one WhatsApp-sized page of verified availability windows + one selection question.
- The selected device is not repeated on every availability row because it was already established in the price sentence.
- Dates use the same weekday-aware deterministic customer formatter.
- Times and doctors come only from verified `availability_windows`; no LLM-generated slot list is used.
- `service + device + date` with no availability -> verified price + exact checked date + natural other-day offer.
- `service + device + date + exact time` keeps the existing exact-time availability/unavailability behavior unchanged.

## Final focused deterministic validation

Affected price/availability + booking lifecycle controls:

`152 passed`

Included:

- `test_v2_price_device_composer.py`
- `test_v2_availability_composer.py`
- `test_booking_journey_contract.py`
- `test_v2_fresh_task_lifecycle.py`
- `test_v2_task1b_resume_automation.py`
- `test_v2_active_task_progress.py`
- `test_v2_planner.py`

Affected Ruff check: PASS.

Affected compileall: PASS.

The previously completed broader regression gate remains `371 passed` across focused booking/package/device/grouped + Historical F1–F6 / Task 1 / Task 1B controls. No TTL, no-reply, automation, grouped-booking, or commercial-ownership code was changed by this final patch.

## Actual runtime controls — C1–C3

The controls below ran against the existing `tia-agent-eval` environment through the local current-working-tree runtime. Railway was used only to supply the evaluation environment variables; there was no deployment.

All three controls produced:

- verified reads: `service_catalog + availability`
- `write_attempted = false`
- errors: none

### C1 — date with multiple verified options

Customer:

> عايز Under Arm على DEKA يوم الخميس

Actual Linka reply:

> جلسة Under Arm على DEKA Again سعرها 550 جنيه.
>
> المتاح يوم الخميس 8 أكتوبر:
> • مع مريم حسن: من 10 صباحًا لـ4:30 مساءً.
> • مع يوسف سمير: من 10 صباحًا لـ1:15 مساءً، ومن 2:15 مساءً لـ10 مساءً.
> أنهي وقت أنسب لك؟

Result: **PASS**. Verified availability is shown in the same reply; the customer does not need a second turn to ask what times are available.

### C2 — date with no availability

Customer:

> عايز Under Arm على DEKA يوم الأحد

Actual Linka reply:

> جلسة Under Arm على DEKA Again سعرها 550 جنيه.
>
> مفيش مواعيد متاحة يوم الأحد 4 أكتوبر. أقدر أدورلك في يوم تاني لو تحب.

Result: **PASS**. No slots were invented, and the checked weekday/date is explicit.

### C3 — date + exact time regression control

Customer:

> عايز Under Arm على DEKA يوم الأحد الساعة 5

Actual Linka reply:

> جلسة Under Arm على DEKA Again سعرها 550 جنيه. الوقت المطلوب مش متاح.

Result: **PASS**. Existing J2 exact-time behavior is unchanged.

## Previously closed journey invariants

The J1–J14 runtime sweep on the preceding green implementation established the following invariants, all untouched by the final presentation-only patch:

- J1: simple laser booking writes once; ordinary closing can end with no reply.
- J2: service/device/date/time together reads price + availability in the same turn.
- J3: single compatible device auto-resolves without an unnecessary device question.
- J6: side price/address questions preserve the active booking task.
- J7/J8: booking resume remains valid through the booking-specific 7-day window with verified temporal re-read.
- J9: expired booking state does not leak into a fresh booking.
- J10: a completed booking does not contaminate a new booking shortly afterward.
- J11: ordinary terminal `شكرا` may produce no outbound reply.
- J12: automation acknowledgement cannot be deterministically terminal-silenced.
- J13: no availability can continue forward through a fresh verified availability read.
- J14: doctor choice is requested only when verified slot ambiguity actually requires it.

## Shared CI final implementation status

Final runtime implementation head:

`6d2ea1d7663a68ab8811af6d8687bae281fad4e7`

Shared CI Run 1905 (`37243896341`): **SUCCESS**.

- frontend: **SUCCESS**
- backend: **SUCCESS**
- full backend: `2156 passed, 4 skipped, 63 warnings`
- agent-eval tooling: `39 passed`
- Ruff: PASS
- compileall: PASS
- Alembic single head: PASS
- clean PostgreSQL migration: PASS

The evidence-only documentation commit that records this result must receive its own exact-head Shared CI success before PR #201 is returned for review.
