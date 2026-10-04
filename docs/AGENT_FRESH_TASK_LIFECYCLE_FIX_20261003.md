# Linka Agent — Fresh Task Lifecycle Fix — 2026-10-03

## Starting main SHA

`3741697181a248b04fcdb2a3a5068a334f5f5837`

Branch: `agent/fresh-task-lifecycle-20261003`

The branch was created in an isolated worktree from the fetched `origin/main`. PR #198 was used only as historical audit evidence/baseline replay and was not used as the implementation branch.

## المشكلة ببساطة

The current persistence invariant is correct:

`save_active_task(... expected=...)` refuses to silently mutate a persisted task from one task type to another.

The failure happened before persistence:

1. a customer started a reschedule;
2. the target appointment was canonically verified;
3. the requested replacement scope ended with verified no availability;
4. no reschedule write happened, correctly;
5. the incomplete `RescheduleTaskState` remained persisted;
6. three days later the same customer explicitly started a new Full Body booking;
7. runtime created a new `BookingTaskState` without first ending the old reschedule flow;
8. final persistence saw `expected=reschedule` + `final_task=booking` and raised:
   `V2StateConflictError: A persisted V2 task cannot change task type in place.`

The customer received no usable reply.

During implementation validation, an adjacent symptom of the same root cause was also caught: after the first fresh booking turn was clean, an old reschedule `after 17:00` constraint could be resurrected from older dialogue on the next date-only booking turn. Fresh-task isolation therefore had to cover both persisted execution state and semantic reuse of abandoned task-local constraints.

## Alternatives considered before implementation

### Approach A — replace whenever operation type changes

Rejected.

Operation-type inequality is not enough evidence that the customer abandoned the active task. A customer can ask pricing, clinic info, package info, or another read while a booking/reschedule remains active. It also cannot distinguish a booking correction from a genuinely separate second booking because both can be `book -> book`.


### Approach B — typed fresh-task boundary + explicit replacement transition

Chosen.

Two structured semantic signals now cover the two stale-authority shapes without giving Python access to raw customer wording:

- `active_task_relationship = continue | replace | unspecified` classifies a task-starting operation relative to an unfinished active task;
- `fresh_task=true` marks that the latest message itself explicitly starts a new/separate booking or reschedule, even when there is no active task because the previous booking already completed;
- `fresh_task_explicit_fields` lists only dimensions explicitly present in that latest customer message.

Python remains authoritative for lifecycle/state effects:

- non-task/side operations preserve the active task;
- `booking <-> reschedule` is a deterministic cross-task replacement boundary;
- same-type replacement uses structured `replace` / `fresh_task`;
- unfinished-task replacement is persisted as `cancel old active flow -> create new active flow`;
- a fresh task with no active task starts from only the latest message's explicit fields.

The semantic contract is message-local. Once the new task is exposed as `active_task`, later service/date/time/doctor/device answers are normal continuations. Absent task-local dimensions are not reconstructed from an abandoned task, recent verified read, completed action, assistant prose, or older dialogue.

No raw customer-text keyword/regex routing was added.

### Approach C — make persistence tolerate task-type replacement

Rejected.

That would weaken a useful concurrency/workflow ownership invariant. `save_active_task` remains unchanged and still rejects task-type mutation in place.

## الحل اللي اتعمل


### Typed semantic contract and completed-action isolation

`backend/app/agents/v2/turn_contract.py`

The task contract includes:

```
ActiveTaskRelationship = Literal["unspecified", "continue", "replace"]
fresh_task: bool
fresh_task_explicit_fields: list[FreshTaskField]
```

`active_task_relationship` is the semantic relationship used by booking/reschedule lifecycle logic. Python ignores that marker on non-task operations.

`fresh_task=true` is independent of active-task existence. It is used when the latest customer message explicitly opens a separate booking/reschedule after either an unfinished task, a completed booking / recent verified action, or older dialogue that still contains task-local details.

`fresh_task_explicit_fields` contains only fields present in the latest customer message. It is not populated from active task state, recent verified actions, verified reads, assistant prose, or history.

`backend/app/agents/v2/turn_interpreter.py`

`isolate_fresh_task_context(...)` runs before verified-context merge. For a fresh task it:

- forces `continues_previous=False`;
- clears stale selection/source appointment/package usage;
- clears service/doctor/device/appointment/date/time unless that field is explicitly listed for the latest message.

The subsequent verified-read merge cannot re-inherit old temporal/entity context because the operation is no longer a continuation. The completed-booking verified-action merge does not inject booking constraints into a new booking.

The interpreter is also instructed to:

- mark true same-task corrections/continuations as `continue`;
- mark an explicit separate/additional unfinished-task goal as `replace`;
- set `fresh_task=true` for a genuinely new booking/reschedule even when there is no active task;
- never carry fresh/replace forward into later answers inside the newly active task.

### Replacement-turn response boundary

`backend/app/agents/v2/responder.py`

When the structured outcome contains `fresh_task_started=true`, old dialogue is excluded from the responder context for that turn. The responder is instructed to answer only from the fresh task's current facts / active-task summary and must not verbally resurrect the prior appointment/task.

This keeps `structured fresh/replace -> fresh customer-facing reply` consistent with the state transition.

### Deterministic Python lifecycle classification

`backend/app/services/agent_v2/active_task_progress.py`

Added `classify_active_task_lifecycle(...)`:

- side/informational operations: `preserve`;
- book vs persisted reschedule, or reschedule vs persisted booking: `replace`;
- same task type + semantic `replace`: `replace`;
- otherwise: `continue`.

`adapt_matching_active_task_step(...)` now marks a fresh transition as `state_action="replace_active"`.

### Clean new state

`backend/app/services/agent_v2/planner.py`

Added typed `replace_active` state action.

`backend/app/services/agent_v2/state_executor.py`

`replace_active` creates a new `BookingTaskState` or `RescheduleTaskState` from the current structured operation only.

It does **not** copy the old:

- target appointment ID;
- replacement date/time;
- doctor/device;
- selected slot;
- option snapshot;
- write authorization.

### Explicit old-flow close before persistence

`backend/app/services/agent_v2/orchestrator.py`

A replacement is recorded as an explicit lifecycle event. At final persistence:

1. `cancel_active_task(... expected=old persisted task)`;
2. `save_active_task(new task, expected=None)`.

Both happen in the caller-owned transaction.

`backend/app/services/agent_v2/state_persistence.py` was **not modified**.

## ليه الحل آمن

- The persistence task-type invariant is unchanged.
- The model only classifies semantic relationship; Python owns state mutation.
- No keyword/regex customer-text routing exists.
- Cross-type task replacement is deterministic and typed.
- Same booking corrections still update the existing task.
- Reschedule no-availability continuation still preserves the exact verified target.
- Fresh task creation resets task-local execution authority but keeps patient identity, workspace, preferred language, and durable conversation history.
- Writes in the live regression ran only against the Demo eval environment inside an outer transaction and were rolled back.
- The old appointment remains unchanged until an explicit valid reschedule write occurs.

## Actual full conversation — before fix

Executed against baseline product code from main SHA `3741697181a248b04fcdb2a3a5068a334f5f5837`, same patient and conversation, rollback-only Demo eval fixture.

Customer:
> ميعادي الجاي امتى؟

Linka:
> ميعادك الجاي:
> يوم 8 أكتوبر 2026 الساعة 10:00 صباحًا، مع مريم حسن، الخدمات: Under Arm على جهاز DEKA Again، الحالة: مؤكد.

Customer:
> عايز اغيره

Linka:
> تحب تغيّره ليوم إيه؟

Customer:
> خليه الخميس

Linka:
> المتاح عندنا لخدمة Under Arm:
> DEKA Again مع مريم حسن يوم 8 أكتوبر 2026: الساعة 10 صباحًا.
> أنهي وقت أنسب لك؟

Customer:
> بعد 5

Linka:
> مفيش مواعيد متاحة يوم 8 أكتوبر 2026 لخدمة Under Arm. أقدر أدورلك في نطاق تاني لو تحب.

At this point:

- verified no availability after 17:00;
- no reschedule write;
- old appointment remained `confirmed`;
- persisted task remained `reschedule / collecting` with target appointment + Oct 8 + after 17:00.

### 3 days later

Customer:
> عايز احجز Full Body كمان

Linka:
> **no customer reply**

Runtime:
```
V2StateConflictError: A persisted V2 task cannot change task type in place.
```

No new appointment was created.


## Actual executed conversation — after fix

Executed from the current implementation branch, same patient/history shape, same +3 day gap, rollback-only Demo eval fixture.

Customer:
> ميعادي الجاي امتى؟

Linka:
> ميعادك الجاي:
> يوم 8 أكتوبر 2026 الساعة 10:00 صباحًا، مع مريم حسن، الخدمات: Under Arm على جهاز DEKA Again، الحالة: مؤكد.

Customer:
> عايز اغيره

Linka:
> تحب تغيّره لإمتى؟

Customer:
> خليه الخميس

Linka:
> المتاح عندنا لخدمة Under Arm:
> DEKA Again مع مريم حسن يوم 8 أكتوبر 2026: من 10 صباحًا لـ4:30 مساءً.
> أنهي وقت أنسب لك؟

Customer:
> بعد 5

Linka:
> مفيش مواعيد متاحة يوم 8 أكتوبر 2026 لخدمة Under Arm. أقدر أدورلك في نطاق تاني لو تحب.

At this point:

- verified no availability after 17:00;
- no reschedule write;
- old appointment remained `confirmed`;
- persisted task remained the incomplete reschedule.

### 3 days later

Customer:
> عايز أبدأ حجز جديد منفصل

Structured interpretation:

```
type = book
active_task_relationship = replace
fresh_task = true
fresh_task_explicit_fields = []
```

Linka:
> تمام، هنبدأ حجز جديد منفصل. تحب تحجز أنهي خدمة؟

The fresh booking task immediately after this turn contains:

- service = null;
- doctor = null;
- device = null;
- date = null;
- time = null;
- old appointment target = absent;
- option snapshot = null;
- selected slot = null;
- write authorization = a fresh source turn.

Customer:
> Full Body

Linka:
> تمام، Full Body. تحب الموعد يكون إمتى؟

State before any new date/time/device/doctor:

- service = Full Body;
- doctor = null;
- device = null;
- date = null;
- time = null;
- old appointment target = absent;
- old selected slot = absent;
- old option snapshot = absent.

Customer:
> الخميس

Linka:
> المتاح عندنا لخدمة Full Body:
> Candela Gentle مع يوسف سمير يوم 8 أكتوبر 2026: من 12 ظهرًا لـ12:30 ظهرًا، ومن 2:15 مساءً لـ8 مساءً.
> DEKA Again مع يوسف سمير يوم 8 أكتوبر 2026: من 12 ظهرًا لـ12:30 ظهرًا، ومن 2:15 مساءً لـ8 مساءً.
> أنهي وقت أنسب لك؟

Customer:
> كانديلا

Linka:
> لقيتلك المواعيد دي لخدمة Full Body:
> Candela Gentle مع يوسف سمير يوم 8 أكتوبر 2026: من 12 ظهرًا لـ12:30 ظهرًا، ومن 2:15 مساءً لـ8 مساءً.
> أنهي وقت أنسب لك؟

Customer:
> الساعة 7

Linka:
> تمام جدًا، ثبتنالك الحجز.

Final T1 DB snapshot inside the rollback transaction:

- old Under Arm appointment: still `confirmed`, same ID/time/doctor/device;
- new appointment: `confirmed`;
- service: Full Body;
- doctor: يوسف سمير;
- device: Candela Gentle;
- local time: 19:00 on 8 Oct 2026;
- only one business write occurred in T1: the new booking.

## Adjacent controls

### T2 — real reschedule continuation

Sequence:

`reschedule -> no availability after 5 -> طب قبل 5؟`

Result:

- no runtime error;
- same `RescheduleTaskState`;
- exact same target appointment ID preserved;
- time changed from `after 17:00` to `before 17:00`;
- task remained active and presented valid earlier availability.

### T3 — booking correction remains same task

Sequence:

`Under Arm booking -> Thursday -> لا خليها يوم التلات بدل الخميس`

Result:

- same booking task;
- same write-authorization source turn;
- date changed;
- no replacement transition.


### T4 — explicit fresh booking while booking task exists

Sequence:

`partial Under Arm booking -> "عايز أبدأ حجز جديد منفصل" -> Full Body`

Actual replacement turn:

Customer:
> عايز أبدأ حجز جديد منفصل

Structured interpretation:

```
type = book
active_task_relationship = replace
fresh_task = true
fresh_task_explicit_fields = []
```

Linka:
> تمام، تحب تحجز أنهي خدمة؟

The response no longer proposes adding the new service to the old appointment or preserving old task constraints.

Immediately after replacement:

- service = null;
- doctor = null;
- device = null;
- date = null;
- time = null;
- option snapshot = null;
- selected slot = null;
- fresh write authorization source.

After the next customer message `Full Body`, only the new service is present; the old date/time/device/doctor remain absent.

### T5 — completed booking context -> time gap -> genuinely fresh booking

This regression covers the production failure shape where there is no unfinished active task.

Session 1 — successful booking:

Customer:
> عايز احجز Under Arm

Linka:
> تحب الحجز يكون يوم إيه؟

Customer:
> الخميس

Linka:
> أقرب المواعيد المتاحة لخدمة Under Arm:
> Candela Gentle مع مريم حسن: من 10:30 صباحًا لـ4:30 مساءً يوم 8 أكتوبر 2026.
> DEKA Again مع مريم حسن: من 10:30 صباحًا لـ4:30 مساءً يوم 8 أكتوبر 2026.
> Candela Gentle مع يوسف سمير: من 12 ظهرًا لـ1:15 مساءً يوم 8 أكتوبر 2026.
> DEKA Again مع يوسف سمير: من 12 ظهرًا لـ1:15 مساءً يوم 8 أكتوبر 2026.
> أنهي وقت أنسب لك؟

Customer:
> ديكا

Linka:
> لقيتلك المواعيد دي لخدمة Under Arm:
> DEKA Again مع مريم حسن: من 10:30 صباحًا لـ4:30 مساءً يوم 8 أكتوبر 2026.
> DEKA Again مع يوسف سمير: من 12 ظهرًا لـ1:15 مساءً يوم 8 أكتوبر 2026.
> DEKA Again مع يوسف سمير: من 2:15 مساءً لـ6:45 مساءً يوم 8 أكتوبر 2026.
> DEKA Again مع يوسف سمير: الساعة 8 مساءً يوم 8 أكتوبر 2026.
> أنهي وقت أنسب لك؟

Customer:
> مريم

Linka:
> لقيتلك المواعيد دي لخدمة Under Arm:
> DEKA Again مع مريم حسن يوم 8 أكتوبر 2026: من 10:30 صباحًا لـ4:30 مساءً.
> أنهي وقت أنسب لك؟

Customer:
> الساعة 4

Linka:
> تمام، حجزك اتأكد.

The resulting `recent_verified_action` contains canonical old booking authority:

- operation_type = book;
- old appointment ID = present;
- old service ID = Under Arm;
- old doctor ID = Mary;
- old device key = DEKA Again;
- old start time = 16:00 local.

### +2 hours, same patient and same conversation

Customer:
> عايز احجز جلسة جديدة

Linka:
> أكيد، تحب تحجز جلسة إيه؟

Before any new service/date/time/device/doctor:

```
task_type = booking
service = null
doctor = null
device = null
date = null
time = null
option_snapshot = null
selected_slot = null
appointment target = absent
write authorization source = new fresh-goal turn
```

The old completed booking authorization source is different from the fresh task's authorization source.

Customer:
> Full Body

Linka:
> تحب تحجز جلسة الـFull Body في أي تاريخ؟

State before any new date/time/device/doctor:

```
service = Full Body
doctor = null
device = null
date = null
time = null
option_snapshot = null
selected_slot = null
```

DB evidence at this exact point:

```
appointments for this patient = 1
booking write on fresh goal/service turns = NO
```

So none of the completed booking's old date/time/device/doctor/appointment/slot/authorization was inherited.

The new journey then continues normally:

Customer:
> الخميس

Linka:
> أقرب المواعيد المتاحة لخدمة Full Body:
> Candela Gentle مع يوسف سمير يوم 8 أكتوبر 2026: من 12 ظهرًا لـ12:30 ظهرًا، ومن 2:15 مساءً لـ6 مساءً.
> DEKA Again مع يوسف سمير يوم 8 أكتوبر 2026: من 12 ظهرًا لـ12:30 ظهرًا، ومن 2:15 مساءً لـ3 مساءً.
> أنهي وقت أنسب لك؟

Customer:
> كانديلا

Linka:
> المتاح عندنا لخدمة Full Body:
> Candela Gentle مع يوسف سمير يوم 8 أكتوبر 2026: من 12 ظهرًا لـ12:30 ظهرًا، ومن 2:15 مساءً لـ6 مساءً، والساعة 8 مساءً.
> أنهي وقت أنسب لك؟

Customer:
> احجز الساعة 5

Linka:
> تم تأكيد الحجز.

Final T5 evidence:

- runtime errors = none;
- first completed appointment remains present;
- no premature second write occurred before new date/time/device selection;
- final patient appointment count = 2;
- business writes = 2 total: first completed booking + second verified fresh booking.

The full T1–T5 runner completed with exit code 0 and all internal lifecycle assertions passed.


## Metrics

### Original stale-active-task regression (T1)

| Metric | Before | After |
|---|---:|---:|
| runtime error | YES | NO |
| fresh booking completed | NO | YES |
| stale task leakage / blocking | YES / blocked | NO |
| premature/wrong write | NO | NO |
| old appointment changed by abandoned reschedule | NO | NO |
| persisted fresh task after new-goal turn | none; runtime crashed | clean BookingTaskState |

The customer/assistant turn counts are not directly comparable because the pre-fix journey terminates at the runtime exception while the post-fix journey continues through a complete new booking.

### Completed-old-booking regression (T5)

| Check | Result |
|---|---|
| old completed booking context exists | YES |
| fresh goal produces booking write | NO |
| after Full Body, new date inherited | NO |
| old time inherited | NO |
| old device inherited | NO |
| old doctor inherited | NO |
| old appointment target inherited | NO |
| old selected slot / option snapshot inherited | NO |
| old write authorization reused | NO |
| appointment count before new constraints are supplied | 1 |
| fresh second booking eventually completes | YES |
| final appointment count | 2 |

## DB isolation evidence

All live journey writes used the Demo eval service under one outer SQLAlchemy transaction.

After the final run completed and rolled back, querying the seeded/new appointment IDs returned:

```
ROLLBACK_EVIDENCE_APPOINTMENTS_PRESENT 0
```

No evaluation appointment residue remained.

## Files changed

Product/runtime:

- `backend/app/agents/v2/turn_contract.py`
- `backend/app/agents/v2/turn_interpreter.py`
- `backend/app/agents/v2/responder.py`
- `backend/app/services/agent_v2/active_task_progress.py`
- `backend/app/services/agent_v2/planner.py`
- `backend/app/services/agent_v2/state_executor.py`
- `backend/app/services/agent_v2/orchestrator.py`

Tests/evidence:

- `backend/tests/test_v2_fresh_task_lifecycle.py`
- `backend/tests/test_v2_responder.py`
- `backend/tests/test_v2_state_persistence.py` (explicit fail-closed invariant regression)
- `tools/agent_eval/run_fresh_task_lifecycle.py`
- `docs/AGENT_FRESH_TASK_LIFECYCLE_FIX_20261003.md`

Not changed:

- `backend/app/services/agent_v2/state_persistence.py`
- cancel-reversal behavior
- device ordering
- price disclosure
- temporal broadening
- terminal acknowledgement wording

## Tests executed

### Focused lifecycle / persistence / semantic coverage

```
pytest -q \
  tests/test_v2_fresh_task_lifecycle.py \
  tests/test_v2_active_task_progress.py \
  tests/test_v2_lifecycle_task_invalidation.py \
  tests/test_v2_state_persistence.py \
  tests/test_v2_turn_contract.py \
  tests/test_v2_turn_interpreter.py \
  tests/test_v2_responder.py

91 passed
```

### Historical F1–F6 guardrails

```
pytest -q \
  tests/test_v2_batch3_focused_fixes.py \
  tests/test_v2_turn_interpreter.py \
  tests/test_v2_completed_action_receipt.py \
  tests/test_v2_booking_presentation_and_cancellation_policy.py \
  tests/test_v2_appointment_temporal_scope.py \
  tests/test_v2_appointment_fact_challenge.py \
  tests/test_v2_availability_scope.py \
  tests/test_v2_availability_show_more.py \
  tests/test_v2_read_continuity_semantics.py

76 passed
```

This covers the requested historical guardrails:

- F1 cancellation target;
- F2 immediate booking revocation semantics;
- F3 temporal continuation;
- F4 appointment fact challenge;
- F5 availability canonical scope;
- F6 pagination/show-more.

### Agent-eval tooling tests

With the same required dummy settings as Shared CI:

```
pytest -q tools/agent_eval/tests
39 passed
```

### Live T1–T5 regression

`tools/agent_eval/run_fresh_task_lifecycle.py` was executed against `tia-agent-eval`.

Its assertions cover:

- T1 full same-patient/same-conversation journey with +3 day gap;
- old appointment unchanged;
- verified no-availability before the fresh goal;
- no `V2StateConflictError`;
- clean fresh booking task;
- no stale target/date/time/device/doctor/slot/auth leakage;
- successful new booking;
- T2 continuation;
- T3 booking correction;
- T4 same-type explicit fresh booking replacement;
- T5 completed booking -> +2h -> fresh booking with real recent_verified_action;
- T5 no write after the fresh-goal or service-only turns;
- T5 no old date/time/device/doctor/appointment/slot/authorization inheritance;
- T5 successful second booking only after new verified constraints.

All assertions passed.

### Static / migration gates

```
ruff check tools/agent_eval
PASS

ruff check app tests alembic
PASS

python -m compileall -q tools/agent_eval
PASS

python -m compileall -q app alembic tests
PASS

git diff --check
PASS

python -m alembic heads
0089_dynamic_laser_device_references (head)
```

## Shared CI

Code head validated:

`e48de111b14c3ae5b1a109334d9f1e8e60687515`

GitHub Actions:

```
CI Run 1879 = SUCCESS

backend = SUCCESS
frontend = SUCCESS
```

Backend job passed:

- agent-eval tooling validation;
- Ruff;
- compileall;
- Alembic single-head verification;
- clean PostgreSQL migration;
- full backend test suite.

Frontend job passed:

- production dependency audit;
- lint;
- Next typegen;
- typecheck;
- production build.

A final documentation-only commit records this CI result. The PR body records the exact final-head CI run after that documentation commit.
