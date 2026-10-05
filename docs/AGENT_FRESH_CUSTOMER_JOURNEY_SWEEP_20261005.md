# Linka — Fresh Customer Journey Evaluation Sweep — 2026-10-05

Starting main SHA: `d13c4b8724592de5f5c6bd8faa17ea0a1d99ad64`

Evaluation branch: `eval/fresh-customer-journey-sweep-20261005`

Scope: evaluation only. No product/runtime patch, merge, deploy, production WhatsApp traffic, or persistent business write was performed. All journey execution used the `tia-agent-eval` environment inside an outer SQL transaction that ended with `OUTER_ROLLBACK=CONFIRMED`.

## Executive result

**CUSTOMER JOURNEY NOT READY**

The fresh sweep found no P1, no wrong booking, no cross-patient/cross-appointment mutation, and no persistent DB residue. E1–E7 behaved safely and preserved the intended booking lifecycle. E8 also completed correctly in the final clean pass, but an earlier clean attempt with the exact same natural continuation (`طب اقرب حاجه بعده امتى؟`) produced a runtime `RuntimeError: V2 runtime produced neither a customer outcome nor a pending write.` after verified no-availability. The same scenario then passed on immediate rerun. Because this is an observed intermittent hard-stop on a normal customer continuation, it is recorded as P2 and prevents a READY verdict even though it was not reproduced on the second attempt.

Two P3 presentation findings were also observed: clinic-hours side-read output is mechanically formatted for WhatsApp, and appointment-information date rendering still uses a legacy same-year form without weekday in E7. Both are safe and non-blocking.

## E1 — Natural new laser booking

Scenario: new Under Arm booking, choose DEKA, change day once, choose a time, book, then close naturally.

Actual conversation:

```text
Customer: محتاج احجز ليزر للابط
Linka: سعر جلسة Under Arm حسب الجهاز:
Candela Gentle — 650 جنيه
DEKA Again — 550 جنيه
تحب أي جهاز؟

Customer: خليه ديكا
Linka: جلسة Under Arm على DEKA Again سعرها 550 جنيه. تحب تحجز يوم إيه؟

Customer: ينفع الاربع؟
Linka: المتاح عندنا:
• DEKA Again مع مريم حسن يوم الأربعاء 7 أكتوبر: من 10 صباحًا لـ11:15 صباحًا، ومن 12:15 ظهرًا لـ5 مساءً.
• DEKA Again مع أحمد محمود يوم الأربعاء 7 أكتوبر: من 12 ظهرًا لـ9 مساءً.
أنهي وقت أنسب لك؟

Customer: لا معلش الخميس احسن
Linka: لقيتلك المواعيد دي لخدمة Under Arm:
• DEKA Again مع مريم حسن يوم الخميس 8 أكتوبر: من 10 صباحًا لـ4:30 مساءً.
• DEKA Again مع يوسف سمير يوم الخميس 8 أكتوبر: من 10 صباحًا لـ1:15 مساءً، ومن 2:15 مساءً لـ10 مساءً.
أنهي وقت أنسب لك؟

Customer: طب 5 ونص مساء
Linka: متاح الخميس الساعة ٥:٣٠ مساءً مع يوسف سمير على جهاز DEKA Again. تحبي أأكد الحجز؟

Customer: ايوه احجزه
Linka: تمام جدًا، ثبتنالك الحجز.

Customer: تسلم يا باشا
Linka: [NO REPLY]
```

What happened: service was resolved first, verified device pricing was shown before availability, date correction invalidated/re-read availability, the selected 17:30 slot uniquely mapped to يوسف, one booking write completed, and the closing acknowledgment produced no outbound reply.

DB effect: one confirmed Under Arm appointment, DEKA Again, 2026-10-08 17:30 Cairo local time, no unrelated appointment changes.

Metrics: customer turns 7; assistant turns 6; avoidable turns 0; repeated questions 0; repeated confirmations 0; writes 1; wrong writes 0; largest option list 2 rows; terminal behavior no-reply; verdict **ACCEPTABLE** (minor gender phrasing only).

## E2 — Many constraints in the first message

```text
Customer: ممكن Full Body كانديلا الخميس بعد 7 بالليل؟
Linka: جلسة Full Body على Candela Gentle سعرها 4050 جنيه.

المتاح يوم الخميس 8 أكتوبر:
• مع يوسف سمير: من 7 مساءً لـ10 مساءً.
أنهي وقت أنسب لك؟
```

Structured semantics contained explicit service + device + exact date + `after 19:00`. Verified reads were `service_catalog` + `availability` in the same turn. No known field was re-asked and no write was attempted.

Metrics: customer turns 1; assistant turns 1; avoidable turns 0; repeated questions 0; repeated confirmations 0; writes 0; wrong writes 0; largest option list 1; terminal behavior open selection question; verdict **PASS**.

## E3 — Change device and then date mid-booking

```text
Customer: عايز جلسة ليزر إبط
Linka: سعر جلسة Under Arm حسب الجهاز:
Candela Gentle — 650 جنيه
DEKA Again — 550 جنيه
تحب أي جهاز؟

Customer: ديكا
Linka: جلسة Under Arm على DEKA Again سعرها 550 جنيه. تحب تحجز يوم إيه؟

Customer: الاربع
Linka: لقيتلك المواعيد دي لخدمة Under Arm:
• DEKA Again مع مريم حسن يوم الأربعاء 7 أكتوبر: من 10 صباحًا لـ11:15 صباحًا، ومن 12:15 ظهرًا لـ5 مساءً.
• DEKA Again مع أحمد محمود يوم الأربعاء 7 أكتوبر: من 12 ظهرًا لـ9 مساءً.
أنهي وقت أنسب لك؟

Customer: استنى خلي الجهاز كانديلا
Linka: جلسة Under Arm على Candela Gentle سعرها 650 جنيه.

المتاح يوم الأربعاء 7 أكتوبر:
• مع مريم حسن: من 10 صباحًا لـ11:15 صباحًا، ومن 12:15 ظهرًا لـ5 مساءً.
• مع أحمد محمود: من 12 ظهرًا لـ9 مساءً.
أنهي وقت أنسب لك؟

Customer: وكمان بدل الاربع خليها الخميس
Linka: المتاح عندنا لخدمة Under Arm:
• Candela Gentle مع مريم حسن يوم الخميس 8 أكتوبر: من 10 صباحًا لـ4:30 مساءً.
• Candela Gentle مع يوسف سمير يوم الخميس 8 أكتوبر: من 10 صباحًا لـ1:15 مساءً، ومن 2:15 مساءً لـ5:15 مساءً، ومن 5:45 مساءً لـ10 مساءً.
أنهي وقت أنسب لك؟

Customer: الساعة 7 مساء
Linka: تمام، حجزك اتأكد.
```

What happened: service remained stable; changing DEKA → Candela triggered a new verified price and availability read; changing Wednesday → Thursday triggered new availability; the final booking used Candela and Thursday, not stale DEKA/Wednesday state.

DB effect: one confirmed Under Arm appointment at 19:00 Cairo local time on Thursday 8 October, Candela Gentle, with the uniquely resolved doctor. No stale-device/date write.

Metrics: customer turns 6; assistant turns 6; avoidable turns 0; repeated questions 0; repeated confirmations 0; writes 1; wrong writes 0; largest option list 2 rows; terminal behavior booking completed; verdict **PASS**.

## E4 — Side conversation while booking

```text
Customer: عايزة احجز Full Body
Linka: سعر جلسة Full Body حسب الجهاز:
Candela Gentle — 4050 جنيه
DEKA Again — 3500 جنيه
تحب أي جهاز؟

Customer: كانديلا
Linka: جلسة Full Body على Candela Gentle سعرها 4050 جنيه. تحب تحجز يوم إيه؟

Customer: على فكرة عنوانكم فين؟
Linka: العنوان المسجل: Linka Clinic: 90 North Teseen Street, Fifth Settlement، New Cairo، New Cairo.

Customer: وبتفتحوا من الساعة كام؟
Linka: ساعات العمل الأسبوعية المسجلة: الاثنين: 09:30–22:00؛ الثلاثاء: 10:00–22:00؛ الأربعاء: 10:00–22:00؛ الخميس: 10:00–22:00؛ الجمعة: 14:00–22:00؛ السبت: 10:00–22:00؛ الأحد: 10:00–22:00 (Africa/Cairo).

Customer: تمام نكمل، الخميس
Linka: المتاح عندنا لخدمة Full Body:
• Candela Gentle مع يوسف سمير يوم الخميس 8 أكتوبر: من 10 صباحًا لـ12:30 ظهرًا، ومن 2:15 مساءً لـ10 مساءً.
أنهي وقت أنسب لك؟
```

What happened: both side reads were grounded `clinic_info` reads. The active booking retained Full Body + Candela across the address and hours questions, then resumed directly on Thursday without asking service/device again. No write was attempted.

Metrics: customer turns 5; assistant turns 5; avoidable turns 0; repeated questions 0; repeated confirmations 0; writes 0; wrong writes 0; largest option list 1; terminal behavior open selection question; verdict **ACCEPTABLE** because the hours/address formatting is correct but mechanically presented.

## E5 — Delayed reply after three days

```text
Customer: ممكن احجز إبط؟
Linka: سعر جلسة Under Arm حسب الجهاز:
Candela Gentle — 650 جنيه
DEKA Again — 550 جنيه
تحب أي جهاز؟

Customer: ديكا مناسب
Linka: جلسة Under Arm على DEKA Again سعرها 550 جنيه. تحب تحجز يوم إيه؟

[simulated +3 days]

Customer: طيب الخميس بليل فيه حاجه؟
Linka: المتاح عندنا لخدمة Under Arm:
• DEKA Again مع يوسف سمير يوم الخميس 8 أكتوبر: من 6 مساءً لـ6:45 مساءً، ومن 7:15 مساءً لـ10 مساءً.
أنهي وقت أنسب لك؟
```

What happened: the same unfinished booking resumed inside the 7-day booking TTL. Service and device were retained, availability was freshly verified at resumed time, and no stale slot/write was reused.

Metrics: customer turns 3; assistant turns 3; avoidable turns 0; repeated questions 0; repeated confirmations 0; writes 0; wrong writes 0; largest option list 1; terminal behavior open selection question; verdict **PASS**.

## E6 — Long-gap different booking

```text
Customer: عايز احجز إبط
Linka: سعر جلسة Under Arm حسب الجهاز:
Candela Gentle — 650 جنيه
DEKA Again — 550 جنيه
تحب أي جهاز؟

Customer: ديكا
Linka: جلسة Under Arm على DEKA Again سعرها 550 جنيه. تحب تحجز يوم إيه؟

[simulated +12 days]

Customer: المرة دي عايز احجز Full Body
Linka: سعر جلسة Full Body حسب الجهاز:
Candela Gentle — 4050 جنيه
DEKA Again — 3500 جنيه
تحب أي جهاز؟
```

What happened: the new explicit Full Body booking was classified `fresh_task=true` / `active_task_relationship=replace`. The resulting task held Full Body only; device, doctor, date, time, option snapshot and old write authorization were not inherited. The reply remained natural and did not expose implementation-state language.

Metrics: customer turns 3; assistant turns 3; avoidable turns 0; repeated questions 0; repeated confirmations 0; writes 0; wrong writes 0; largest option list 2; terminal behavior active fresh booking; verdict **PASS**.

## E7 — Reminder / appointment interaction

The final control was split so a reminder acknowledgment did not consume automation focus before the reschedule control.

### E7A — direct reminder-linked reschedule

```text
Customer: هو الحجز اللي عندي الخميس الساعة كام؟
Linka: ميعادك الجاي:
يوم 8 أكتوبر 2026 الساعة 10:00 صباحًا، مع يوسف سمير، الخدمات: Under Arm على جهاز DEKA Again، الحالة: مؤكد.

[system automation reminder injected with server-owned appointment metadata]

Customer: ممكن نخليه الساعة 7 بالليل بدل كده؟
Linka: تمام، ميعادك اتغيّر، يوم الخميس 8 أكتوبر الساعة 7 مساءً.
```

Structured operation on the reschedule turn: `automation_context_relationship=appointment_action`, `appointment_action_explicit_fields=[time]`; the canonical date was preserved from the verified reminder appointment. Reads: `appointments` + `availability`. One reschedule write completed. The old fixture appointment became `rescheduled` and the replacement appointment was confirmed for 19:00 Cairo local time on the same date.

### E7B — reminder acknowledgment

```text
Customer: ميعادي الخميس لسه ثابت؟
Linka: ميعادك الجاي:
يوم 8 أكتوبر 2026 الساعة 10:00 صباحًا، مع يوسف سمير، الخدمات: Under Arm على جهاز DEKA Again، الحالة: مؤكد.

[system automation reminder injected]

Customer: تمام
Linka: تمام، تحت أمرك.
```

The acknowledgment was typed as `automation_context_relationship=acknowledge`, made no write, and was not incorrectly silenced by terminal no-reply handling.

Metrics: customer turns 4 across the two reminder controls; assistant turns 4; avoidable turns 0; repeated questions 0; repeated confirmations 0; writes 1; wrong writes 0; terminal behavior reminder acknowledgment replied; verdict **PASS**.

## E8 — No availability then nearest future availability

Successful clean pass:

```text
Customer: محتاج احجز ليزر إبط
Linka: سعر جلسة Under Arm حسب الجهاز:
Candela Gentle — 650 جنيه
DEKA Again — 550 جنيه
تحب أي جهاز؟

Customer: ديكا
Linka: جلسة Under Arm على DEKA Again سعرها 550 جنيه. تحب تحجز يوم إيه؟

Customer: النهارده
Linka: مفيش مواعيد متاحة يوم الاثنين 5 أكتوبر لخدمة Under Arm. أقدر أدورلك في نطاق تاني لو تحب.

Customer: طب اقرب حاجه بعده امتى؟
Linka: لقيتلك المواعيد دي لخدمة Under Arm:
• DEKA Again مع مريم حسن يوم الثلاثاء 6 أكتوبر: من 10 صباحًا لـ5 مساءً.
• DEKA Again مع يوسف سمير يوم الثلاثاء 6 أكتوبر: من 12 ظهرًا لـ12:15 ظهرًا، ومن 1:15 مساءً لـ8 مساءً.
أنهي وقت أنسب لك؟
```

What happened in the successful pass: service/device were retained, Monday had verified no availability, the continuation was interpreted as `date.mode=next_available`, and the next verified availability moved forward to Tuesday 6 October. No write occurred.

However, an earlier clean attempt of the same scenario reached the same final customer message and raised:

```text
RuntimeError: V2 runtime produced neither a customer outcome nor a pending write.
```

The failing attempt also ended with outer rollback and no business write. Immediate rerun with the same inputs passed as shown above. Therefore the failure is recorded as intermittent, observed once, and not reproduced on the immediate retry.

Metrics for successful pass: customer turns 4; assistant turns 4; avoidable turns 0; repeated questions 0; repeated confirmations 0; writes 0; wrong writes 0; largest option list 2; terminal behavior open selection question. Journey verdict: **FAIL** because a normal continuation produced an observed intermittent hard runtime failure in another clean attempt.

## Findings

### F1 — Intermittent nearest-availability continuation can produce no customer outcome

- Severity: **P2**
- Journey: E8
- Customer message: `طب اقرب حاجه بعده امتى؟`
- Prior Linka reply: `مفيش مواعيد متاحة يوم الاثنين 5 أكتوبر لخدمة Under Arm. أقدر أدورلك في نطاق تاني لو تحب.`
- Observed consequence: one clean evaluation attempt raised `RuntimeError: V2 runtime produced neither a customer outcome nor a pending write.` No write occurred and the outer transaction rolled back. The immediate retry with the same conversation inputs succeeded and returned verified Tuesday availability.
- Why it matters: this is a natural continuation after no availability. Even though it is intermittent and safe from a write perspective, a hard runtime failure materially blocks the customer journey.
- Probable root cause: not proven in this evaluation-only sweep. Evidence points to a nondeterministic semantic/planning edge around a `continue_active` nearest-availability continuation where the orchestrator can reach a state with neither customer outcome nor pending write. `AvailabilityComposerValidationError` fallback logs were also observed during the sweep, but the same deterministic fallback occurred in successful journeys, so the composer fallback alone is not established as the cause.
- Status: **OBSERVED ONCE; NOT REPRODUCED ON IMMEDIATE RETRY; NOT FIXED IN THIS SWEEP.**

### F2 — Side-information hours reply is grounded but mechanically formatted

- Severity: **P3**
- Journey: E4
- Customer: `وبتفتحوا من الساعة كام؟`
- Linka: `ساعات العمل الأسبوعية المسجلة: الاثنين: 09:30–22:00؛ ... الأحد: 10:00–22:00 (Africa/Cairo).`
- Consequence: no state corruption; booking resumed correctly.
- Why it matters: the answer exposes raw 24-hour formatting and an internal-looking timezone token, and returns the full week to a simple opening-time question. This is readable but less natural on WhatsApp.
- Probable root cause: clinic-info response presentation contract is more data-dump oriented than the newer booking/availability presentation contract.

### F3 — Appointment-information reply still uses legacy same-year date presentation

- Severity: **P3**
- Journey: E7
- Actual reply fragment: `يوم 8 أكتوبر 2026 الساعة 10:00 صباحًا`
- Consequence: none; reminder targeting and reschedule write were correct.
- Why it matters: booking/availability replies use weekday-aware customer dates such as `الخميس 8 أكتوبر`, while appointment-information still omitted the weekday and included the current year.
- Probable root cause: appointment-information rendering remains on a separate presentation path from the shared booking date formatter.

### Minor accepted wording note

E1 used `تحبي أأكد الحجز؟` after a customer message phrased `محتاج ...`. This is a P3 gender-language polish issue only; it did not affect semantic understanding, safety, or completion and is classified **ACCEPTABLE**, not a standalone fix recommendation from this sweep.

## Optional E9 — Package customer

**DEFERRED.** The eight core fresh journeys already exercised the requested current-runtime lifecycle, commercial pricing, delayed resume, automation, and no-availability behavior. A trustworthy package journey requires a dedicated active-package fixture with package ownership/remaining-session state; this sweep did not expand fixture scope merely to add another conversation. Previously known package evidence is not reused as fresh E9 evidence.

## Final summary table

| Journey | Verdict | Customer turns | Assistant turns | Avoidable turns | Repeated questions | Repeated confirmations | Writes | Wrong writes | Largest option list | Terminal behavior |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| E1 | ACCEPTABLE | 7 | 6 | 0 | 0 | 0 | 1 | 0 | 2 | closing → no reply |
| E2 | PASS | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | selection question |
| E3 | PASS | 6 | 6 | 0 | 0 | 0 | 1 | 0 | 2 | booking completed |
| E4 | ACCEPTABLE | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 1 | booking preserved/open |
| E5 | PASS | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 1 | resumed/open |
| E6 | PASS | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 2 | fresh booking/open |
| E7 | PASS | 4* | 4* | 0 | 0 | 0 | 1 | 0 | 0 | reminder ack replies |
| E8 | FAIL | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 2 | intermittent hard-stop observed |

`*` E7 combines two isolated reminder controls: direct reminder-linked reschedule and reminder acknowledgment.

## Validation / safety evidence

- Baseline verified from current `origin/main`: `d13c4b8724592de5f5c6bd8faa17ea0a1d99ad64`.
- Evaluation branch: `eval/fresh-customer-journey-sweep-20261005`.
- Actual runtime used `railway run --service tia-agent-eval --environment production` only to inject the eval service environment into the local runner. No Railway deployment/restart was created.
- Every journey ran inside one outer SQL transaction with final `OUTER_ROLLBACK=CONFIRMED`.
- No production WhatsApp traffic was used.
- No P1/wrong write was observed.
- Fresh runner Ruff: PASS.
- Fresh runner compileall: PASS.
- Agent-eval tooling tests: `39 passed`.
- Post-cleanup runner smoke: E2 executed on `tia-agent-eval`, `errors=[]`, `writes=0`, `OUTER_ROLLBACK=CONFIRMED`.
- `git diff --check`: PASS.

## Known-issues rule

This report does not claim historical production issues are fixed merely because they did not appear here. Only behavior actually exercised by these fresh conversations is classified. E8's intermittent failure is explicitly not called fixed after a successful retry.
