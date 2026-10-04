# Linka Task 1B — Resume TTL / Automation Context Isolation

Date: 2026-10-04  
Repository: `GirgesAdam/Tia_AI`  
Branch: `agent/fresh-task-lifecycle-20261003`  
PR: #199 (Draft)

## Baseline

Starting / current `origin/main` during implementation:

```
3741697181a248b04fcdb2a3a5068a334f5f5837
```

Task 1B started from PR head:

```
b7d08140b88a26db15eaf43e938c7306fe656711
```

No merge or deploy was performed.

## Problem

Task 1 already separated an explicit fresh customer goal from stale unfinished/completed task state.

Task 1B closes two adjacent lifecycle boundaries:

1. A genuinely unfinished booking must remain resumable during its configured flow lifetime instead of being treated as stale merely because time passed.
2. System automation messages such as appointment reminders and post-visit followups must be conversational context without becoming booking/write authority or contaminating an unrelated active task.

The existing canonical resume lifetime is:

```
settings.agent_flow_ttl_hours = 24
```

`ConversationFlowState.expires_at` remains the source of truth. No second timeout policy was introduced.

## Chosen design

### Resume boundary

Within the active flow TTL:

```
persisted active task
+ customer continuation/correction
-> continue same typed task
-> retain still-valid stable constraints
-> invalidate/rebuild dependent availability state
-> verified reads before any write
```

If a retained temporal constraint has become past by the time the customer resumes, Python drops only the no-longer-valid temporal field(s). Stable service/device/doctor facts are not discarded solely because time passed.

After `expires_at`:

```
expired flow
-> no active task authority
-> raw dialogue history cannot reconstruct task-local constraints
-> fresh book/reschedule state may use only latest explicit customer facts
   or explicitly supported verified/automation context
```

### Automation boundary

Automation outbound messages remain `sender_type=system` and are not promoted into normal patient/AI history.

A new server-owned `automation_context` is built only when the immediately preceding outbound is an automation message whose metadata contains:

```
source = automation_engine
automation_rule_key
appointment_id
```

The appointment id is never parsed from template prose. It is re-verified against workspace + patient ownership before semantic exposure.

The model receives only opaque semantic references:

```
appointment_ref
service_ref
device_ref
automation_rule_key
appointment_status
start_at
```

Automation context is explicitly not:

```
recent_verified_read
recent_verified_action
pending_choice
write authorization
```

Typed semantic relationships are:

```
none
acknowledge
appointment_action
next_session
```

Python then binds only the allowed server-verified facts.

### Conversation-order fix

System automation messages are intentionally omitted from raw native dialogue history. A live R7 regression showed that this can make a terse reply such as `تمام` appear to follow an older assistant clarification.

The fix does not expose template prose as authority. When immediate `automation_context` exists, the interpreter receives a server-generated chronology marker after native dialogue and before the latest customer message stating that the automation occurred in between.

This restores message order while preserving the authority boundary.

## Safety properties

- No keyword/regex intent routing was added.
- `save_active_task` task-type mutation protection remains unchanged.
- Automation template text is never parsed for appointment identity.
- Reminder acknowledgement cannot write.
- Reminder acknowledgement does not mutate an unrelated active task.
- Reminder acknowledgement does not renew `ConversationFlowState.expires_at`.
- Appointment lifecycle actions after a reminder target only the server-verified automation appointment.
- Post-visit “next session” may reuse only stable verified treatment facts (service/device).
- Post-visit continuation never reuses prior date/time/doctor/slot/options/write authorization.
- Expired task state cannot be reconstructed from raw conversation prose.

## Files changed

```
backend/app/agents/v2/responder.py
backend/app/agents/v2/semantic_context.py
backend/app/agents/v2/semantic_state_view.py
backend/app/agents/v2/turn_contract.py
backend/app/agents/v2/turn_interpreter.py
backend/app/services/agent_v2/active_task_progress.py
backend/app/services/agent_v2/live_chat.py
backend/app/services/agent_v2/orchestrator.py
backend/tests/test_v2_task1b_resume_automation.py
tools/agent_eval/run_fresh_task_lifecycle.py
```

## Live rollback-only regressions

All R1–R9 were executed through the real V2 chat runtime against the disposable/eval path inside an outer transaction and rollback.

### R1 — same-day resume (+2h)

Customer starts an Under Arm booking, selects DEKA, then returns two hours later:

```
Customer: طب فيه يوم السبت؟
Linka: لقيتلك المواعيد دي لخدمة Under Arm:
       DEKA Again مع يوسف سمير يوم 10 أكتوبر 2026...
       أنهي وقت أنسب لك؟
```

Result:

```
same booking task = YES
service preserved = YES
device preserved = YES
new date verified = YES
write = NO
```

### R2 — next-calendar-day resume (+18h, inside 24h TTL)

The existing Under Arm / DEKA / Saturday task is still active.

```
Customer: طب بعد الساعة 6؟
Linka: verified availability after 18:00
```

Result:

```
same task = YES
service preserved = YES
device preserved = YES
date preserved = YES
time updated to after 18:00 = YES
availability re-verified = YES
write = NO
```

### R3 — expired flow, generic booking request

An unfinished booking flow is expired through the canonical `expires_at` path, then the customer says:

```
Customer: عايز احجز
```

Result:

```
old flow status = expired
old flow is_active = false

inherited service = NO
inherited device = NO
inherited doctor = NO
inherited date = NO
inherited time = NO
inherited selected slot = NO
inherited option snapshot = NO
write = NO
```

### R4 — expired flow, service explicitly restated

After the same expiry boundary:

```
Customer: عايز أكمل حجز Under Arm
Linka: تحب تحجز Under Arm في أي تاريخ؟
```

New task state:

```
service = Under Arm   # explicit latest-turn fact
device = null
doctor = null
date = null
time = null
selected_slot = null
option_snapshot = null
old authorization reused = NO
write = NO
```

### R5 — completed booking + appointment reminder + acknowledgement

After a completed booking, an automation reminder is inserted with real automation metadata.

```
Customer: تمام
Linka: تمام.
```

Structured operation:

```
type = social
automation_context_relationship = acknowledge
execution = read-only
```

Context evidence:

```
recent_verified_read = null
recent_verified_action = null
pending_choice = null
automation_context = verified server metadata
write = NO
active task = none
```

### R6 — appointment reminder + reschedule action

The reminder references one verified appointment.

```
Customer: ممكن أخليه الساعة 6؟
Linka: تقصد يوم إيه؟

Customer: نفس اليوم
Linka: تمام جدًا، عدلنالك الميعاد، يوم 8 أكتوبر 2026 الساعة 6 مساءً.
```

Structured first action:

```
type = reschedule
automation_context_relationship = appointment_action
```

Result:

```
target = exact appointment_id from verified automation metadata
verified reschedule path = YES
write = one normal reschedule write
template prose used as identity = NO
```

### R7 — unrelated active booking + reminder + terse acknowledgement

Before the chronology fix, `تمام` could be interpreted as continuing an older assistant clarification because the system reminder was intentionally absent from native history.

After the fix:

```
Customer: عايز احجز Full Body
Linka: تحب تحجز الـ Full Body يوم إيه؟

Customer: ديكا
Linka: <date clarification>

[system appointment reminder for a separate Under Arm appointment]

Customer: تمام
Linka: تمام

Customer: طب نكمل الحجز
Linka: أكيد، تحب تحجز يوم إيه؟
```

Acknowledgement operation:

```
type = social
automation_context_relationship = acknowledge
write = NO
```

Active booking fields before and after acknowledgement are identical:

```
service = Full Body
device = prime_lase
doctor = null
date = null
time = null
selected_slot = null
option_snapshot = null
write_authorization = same source
```

Flow lifecycle evidence before vs after acknowledgement:

```
flow id = same
status = collecting_requirements
version = 2 -> 2
expires_at = unchanged
last_turn_at = unchanged
```

So the reminder/acknowledgement neither contaminates the active task nor extends its TTL.

### R8 — post-visit followup + acknowledgement

```
Customer: تمام الحمد لله
Linka: الحمد لله، دايمًا يا رب!
```

Structured:

```
type = social
automation_context_relationship = acknowledge
active task = none
write = NO
```

### R9 — post-visit followup + next-session booking

```
Customer: عايز احجز الجلسة الجاية
Linka: تحب تحجزها في أي تاريخ؟
```

Structured:

```
type = book
fresh_task = true
automation_context_relationship = next_session
```

State immediately after that request:

```
service = verified previous treatment service (Under Arm)
device = verified previous treatment device (prime_lase)
doctor = null
date = null
time = null
selected_slot = null
option_snapshot = null
write_authorization = fresh booking authorization
write = NO
```

The customer then supplies fresh date/doctor/time constraints and the second booking completes through normal verified availability/write flow.

## Test results

Focused Task 1 / Task 1B lifecycle, semantics, persistence and responder coverage:

```
95 passed
```

Historical F1–F6 guardrails:

```
76 passed
```

Agent-eval tooling tests with CI settings:

```
39 passed
```

Full live R1–R9 runner:

```
exit code = 0
all embedded lifecycle assertions = PASS
```

Static and migration gates:

```
Ruff = PASS
compileall = PASS
git diff --check = PASS
Alembic single head = 0089_dynamic_laser_device_references (head)
```

## Shared CI

Pending exact-head Shared CI after the Task 1B commit is pushed. The PR body will record the terminal workflow result for the exact final head without creating another commit solely to describe its own CI.
