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

### Approach B — typed task relationship + explicit replacement transition

Chosen.

A typed semantic field now classifies only the relationship of a task-starting operation to the supplied active task:

- `unspecified` — no active-task lifecycle claim;
- `continue` — continue/correct the same unfinished task;
- `replace` — explicitly start a separate/unrelated booking or reschedule and abandon the unfinished task.

Python remains authoritative for lifecycle effects:

- non-task/side operations preserve the active task;
- `booking <-> reschedule` is a deterministic cross-task replacement boundary;
- same-type replacement happens only when the structured semantic relation is `replace`;
- replacement is persisted as:
  `cancel old active flow -> create new active flow`.

The semantic contract is message-local: `replace` may only describe what the latest customer message itself does. Once the new task is exposed as `active_task`, later date/time/doctor/device/service answers are continuations. The current active task is authoritative for task-local constraints; absent constraints are not reconstructed from an older abandoned task.

No raw customer-text keyword/regex routing was added.

### Approach C — make persistence tolerate task-type replacement

Rejected.

That would weaken a useful concurrency/workflow ownership invariant. `save_active_task` remains unchanged and still rejects task-type mutation in place.

## الحل اللي اتعمل

### Typed semantic contract

`backend/app/agents/v2/turn_contract.py`

Added:

```
ActiveTaskRelationship = Literal["unspecified", "continue", "replace"]
```

and `TurnOperation.active_task_relationship`, valid only for `book` / `reschedule` when non-default.

`backend/app/agents/v2/turn_interpreter.py`

The structured interpreter is instructed to:

- mark true same-task corrections/continuations as `continue`;
- mark an explicit separate/additional task as `replace`;
- never carry a prior replace decision into a later constraint-only reply;
- treat the supplied active task as authoritative for task-local constraints;
- never resurrect an absent old task date/time/doctor/device/slot from abandoned dialogue.

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

Executed from the implementation branch using the committed live regression runner, same patient/history shape, same +3 day gap, rollback-only Demo eval fixture.

Customer:
> ميعادي الجاي امتى؟

Linka:
> ميعادك الجاي:
> يوم 8 أكتوبر 2026 الساعة 10:00 صباحًا، مع مريم حسن، الخدمات: Under Arm على جهاز DEKA Again، الحالة: مؤكد.

Customer:
> عايز اغيره

Linka:
> تحب تغيّر ميعادك لأي تاريخ؟

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

### 3 days later

Customer:
> عايز احجز Full Body كمان

Linka:
> محتاج أعرف التاريخ اللي تحب تحجز فيه Full Body.

The persisted task immediately after this turn is a clean booking task:

- service = Full Body;
- doctor = null;
- device = null;
- date = null;
- time = null;
- old appointment target = absent;
- option snapshot = null;
- selected slot = null;
- write authorization source = the new booking turn, not the old reschedule turn.

Customer:
> الخميس

Linka:
> المتاح عندنا لخدمة Full Body:
> Candela Gentle مع يوسف سمير يوم 8 أكتوبر 2026: من 12 ظهرًا لـ12:30 ظهرًا، ومن 2:15 مساءً لـ8 مساءً.
> DEKA Again مع يوسف سمير يوم 8 أكتوبر 2026: من 12 ظهرًا لـ12:30 ظهرًا، ومن 2:15 مساءً لـ8 مساءً.
> أنهي وقت أنسب لك؟

The new booking still has `time = null` here; the old `after 17:00` reschedule constraint was not resurrected.

Customer:
> كانديلا

Linka:
> المتاح عندنا لخدمة Full Body:
> Candela Gentle مع يوسف سمير يوم 8 أكتوبر 2026: من 12 ظهرًا لـ12:30 ظهرًا، ومن 2:15 مساءً لـ8 مساءً.
> أنهي وقت أنسب لك؟

Customer:
> الساعة 7

Linka:
> تمام جدًا، ثبتنالك الحجز.

Final DB snapshot inside the rollback transaction:

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

`partial Under Arm booking -> explicit Full Body second booking`

Structured interpretation emitted `book + active_task_relationship=replace`.

Result:

- old Under Arm booking task closed/replaced;
- new Full Body booking task started;
- doctor/device/date/time = null;
- old option snapshot absent;
- fresh write authorization source;
- no stale Under Arm task-local constraints leaked.

## Metrics

| Metric | Before | After |
|---|---:|---:|
| runtime error | YES | NO |
| new booking completed | NO | YES |
| stale task leakage / blocking | YES / blocked | NO |
| customer turns in T1 | 5 | 8 |
| assistant replies in T1 | 4 | 8 |
| business writes in T1 | 0 | 1 |
| old appointment changed | NO | NO |
| new appointments created inside eval tx | 0 | 1 |
| persisted fresh task after new-goal turn | none; runtime crashed | clean BookingTaskState |

The higher after-turn count is expected because the customer can now proceed through the new booking instead of crashing at the first fresh-task turn.

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
- `backend/app/services/agent_v2/active_task_progress.py`
- `backend/app/services/agent_v2/planner.py`
- `backend/app/services/agent_v2/state_executor.py`
- `backend/app/services/agent_v2/orchestrator.py`

Tests/evidence:

- `backend/tests/test_v2_fresh_task_lifecycle.py`
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
  tests/test_v2_turn_interpreter.py

66 passed
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

### Live T1–T4 regression

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
- T4 same-type explicit fresh booking replacement.

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

_Pending exact-head PR CI run._
