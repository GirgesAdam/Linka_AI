# Linka Agent — Real Customer Journey Sweep — 2026-10-03

## Scope

Evaluation-only sweep against the latest `origin/main`.

- Starting / tested main SHA: `3741697181a248b04fcdb2a3a5068a334f5f5837`
- Branch: `eval/real-customer-journey-sweep-20261003`
- Runtime: real Agent V2 interpreter + planner + verified reads + write executor + responder
- Environment: Railway `tia-agent-eval` Demo workspace
- External side effects: blocked by Demo runtime policy
- Business-data isolation: one outer database transaction; all fixture normalization and journey writes were rollback-only
- Product code changes: **none**
- Report/evidence only

The sweep exercised 14 customer journeys represented by 16 execution segments because A and G intentionally return to the same patient/conversation after a time gap.

Evaluation fixture names were normalized only inside the rollback transaction to match the requested customer language:

- Under Arm
- Full Body
- Bikini
- DEKA Again
- Candela Gentle
- doctors Mary / Youssef

Device-specific prices/durations used by the evaluation were rollback-only fixture values. No permanent Demo or Production catalog changes were made.

Raw local evidence was captured outside the repository:

- `C:\Users\USER\customer_journey_sweep_raw.json`
- `C:\Users\USER\customer_journey_compact.json`

## Executive result

The broad customer behavior is mostly healthy. Booking, device-specific pricing, no-availability recovery, corrections, side questions, existing-package booking, explicit non-package booking, same-day booking, clinic information, medical escalation, and social closing all completed without unsafe writes.

Two production-relevant lifecycle/continuity defects remain:

1. **P1 — an abandoned persisted reschedule can crash a later unrelated booking**
2. **P1 — after a cancellation has already completed, an immediate reversal can produce a false statement that the cancellation did not execute and then lock the conversation into handoff**

No P0 finding was observed.

There are also two non-blocking conversational-friction observations: device disambiguation in C is clumsy, and D requires repeating “first appointment” after pagination. Both remain grounded and do not cause an incorrect write, so they are not promoted to product-blocking findings in this sweep.

## Journey matrix

| Journey | Result | Evidence summary |
|---|---|---|
| A — booking, then second booking after 2h | PASS | A1 booked Under Arm / DEKA Again / Mary; A2 reused the same patient/history but correctly started a separate Full Body / Candela booking. No stale A1 dimensions leaked. |
| B — price comparison → choose device → book | PASS | Returned 650 EGP Candela vs 550 EGP DEKA, preserved DEKA choice, handled no availability tomorrow, then booked the nearest later slot. |
| C — broad availability → narrow date/time/doctor/device | PASS with friction | Final DB write is correct: Full Body, Youssef, Candela, Thu 8 Oct 19:00. An unnecessary generic ambiguity turn occurred before the final device choice. |
| D — no availability → broaden date → first slot | PASS with friction | Correctly reported no availability tomorrow, broadened to later dates, and ultimately booked the earliest shown slot. “اول ميعاد” paged once before selecting, requiring repetition. |
| E — corrections before/after booking | PASS | Date changed Thu→Tue, device DEKA retained, initial Mary booking was then safely rescheduled to Youssef at the same date/time. Canonical DB chain is correct. |
| F — booking interrupted by price/address side questions | PASS | Price and address reads did not destroy the booking task; “كمل الحجز” resumed Full Body / DEKA and booked successfully. |
| G — return to existing appointment, then next-day reschedule attempt | PASS individually; seeds F1 | G1 correctly read the next appointment. G2 preserved the exact appointment identity and reported no availability after 17:00 without an incorrect write, but left the reschedule task persisted. |
| H — cancel then immediately reverse | **FAIL — P1 / F2** | Cancellation write completed and DB status became `cancelled`. Next turn incorrectly said the cancellation “did not execute” and created a human handoff; the following turn repeated the same false ack without a canonical appointment read. |
| I — book from active package + ask remaining sessions | PASS | Package read showed 3 remaining; booking used the same package/device with `package_prepaid`, and package remaining decremented to 2. |
| J — active package exists but customer explicitly pays standalone | PASS | Booking remained `standard`, no `patient_package_id`, and the package remained at 3 sessions. |
| K — today’s clinic info → same-day availability → booking | PASS | Clinic info grounded from clinic read; same-day availability was checked and DEKA booking at 18:00 completed correctly. |
| L — laser booking then pregnancy question | PASS | No booking write after medical signal; conversation handed off to the medical team with high priority. |
| M — return 3 days later and start a different booking | **FAIL — P1 / F1** | First turn crashed before a customer reply with `V2StateConflictError: A persisted V2 task cannot change task type in place.` No new appointment write occurred. |
| N — short successful booking + conversational close | PASS | Under Arm / DEKA / Youssef booking completed; later “تسلم / تمام” stayed social-only and caused no duplicate writes. |

## Finding F1 — P1: stale reschedule task blocks a later unrelated booking

### Reproduction

Same patient and conversation:

1. G1 reads the customer’s next confirmed Under Arm / DEKA appointment.
2. On the next day, G2 starts a reschedule.
3. Customer chooses Thursday, then asks for “after 5”.
4. Verified availability returns zero matching options.
5. The runtime correctly does **not** write a reschedule, but persists the reschedule task in `collecting` state.
6. Three days later, M says: `عايز احجز Full Body كمان`.
7. The runtime raises:

`V2StateConflictError: A persisted V2 task cannot change task type in place.`

No customer reply is produced.

### Deterministic evidence

At the end of G2 the persisted state is still:

- `task_type = reschedule`
- target = the verified original appointment
- replacement date = 2026-10-08
- replacement time = after 17:00
- no matching availability

The later M turn is a new `book` operation. It is not a continuation of that failed reschedule.

### Root cause

The persistence layer is correctly defensive: it refuses in-place task-type mutation.

`backend/app/services/agent_v2/state_persistence.py`

`save_active_task(...)` explicitly raises when the final task type differs from the expected persisted task type:

```python
if active_task.task_type != expected.active_task.task_type:
    raise V2StateConflictError("A persisted V2 task cannot change task type in place.")
```

The missing behavior is one layer earlier.

`backend/app/services/agent_v2/active_task_progress.py`

- `adapt_matching_active_task_step(...)` has explicit “book while booking is active = patch the current booking” behavior.
- A persisted **reschedule** plus a new unrelated **book** operation falls through unchanged.
- `persist_initial_task_intent(...)` then turns the new booking clarification into `state_action="start_booking"`.

`backend/app/services/agent_v2/orchestrator.py`

- the orchestrator ends with `_persist_final_task(initial=persisted, final_task=current_task)`
- because the old reschedule was never explicitly cancelled/replaced, `save_active_task(... expected=old_reschedule)` receives a new booking task and throws the conflict.

So the failure is not a concurrency race and not provider noise. It is a deterministic state-transition gap for **new task type after an abandoned/blocked persisted task**.

### Customer impact

A returning customer can be unable to book anything new merely because an earlier reschedule attempt ended with no availability. The failure is a runtime exception with no usable customer response.

### Fix direction

Do not weaken the persistence invariant.

Instead, before starting a task of a different type, the orchestrator/state layer should deterministically decide whether the current explicit customer intent abandons the old task. For a clearly unrelated new task:

`persisted old task -> explicit cancel/close old flow -> create new task`

This must be based on structured intent/state, not keyword routing.

Required regression:

- same patient + same conversation
- persisted reschedule ends in verified no-availability
- time gap
- explicit new booking for another service
- old reschedule flow is closed/cancelled safely
- new booking starts normally
- no `V2StateConflictError`

## Finding F2 — P1: post-cancel reversal contradicts canonical DB state

### Reproduction

H:

1. Customer: `عايز الغي معادي الجاي`
2. Verified appointment read finds one canonical appointment.
3. Cancellation write completes successfully.
4. DB status changes from `confirmed` to `cancelled`.
5. Customer immediately says: `استنى خلاص سيبه زي ما هو`.
6. Structured interpreter emits `human_support`.
7. Planner creates a generic `customer_request` handoff.
8. Customer-facing reply says, in substance, that the cancellation **did not execute** and that the appointment will be left as-is.
9. The following question `هو معادي لسه موجود صح؟` receives the same reused handoff acknowledgement, with no appointment read.

### Deterministic evidence

The first turn outcome contains:

- `status = completed`
- `response_goal = cancellation_completed`
- `action_result.ok = true`
- `action_result.status = cancelled`

The final DB snapshot shows the same appointment changed:

`confirmed -> cancelled`

Therefore the later statement that the cancellation did not execute is false.

### Root cause

There are two linked gaps.

**1. Semantic/lifecycle reversal gap**

`backend/app/agents/v2/turn_contract.py` exposes lifecycle operations such as `cancel_appointment`, `reschedule`, and generic `human_support`, but there is no dedicated deterministic “undo completed cancellation” operation.

For the reversal utterance the interpreter selected `human_support` rather than grounding the response in the immediately preceding verified cancellation result.

`backend/app/services/agent_v2/planner.py`

A `human_support` operation becomes a generic handoff with only:

- category
- priority
- preserve-active-task flag

It contains no verified cancellation truth that constrains the customer-facing response.

**2. Handoff acknowledgement reuse amplifies the first false response**

`backend/app/services/agent_v2/live_chat.py`

Pending AI handoff continuation can reuse the previous handoff acknowledgement via `_previous_handoff_ack_reply(...)`. Once the first handoff response is wrong, later customer turns can repeat the same wrong status without re-reading the appointment.

### Customer impact

This is a trust/lifecycle correctness problem. The system performs a destructive lifecycle write, then tells the customer that it did not happen. The handoff also prevents the subsequent “is it still there?” question from being canonically answered.

### Fix direction

The safe behavior after a completed cancellation reversal is not to pretend the write never happened.

A robust path is:

1. use recent verified action context to recognize that cancellation already completed;
2. re-read the canonical appointment when the customer challenges/reverses the result;
3. truthfully state that the appointment is currently cancelled;
4. if restoring the old slot is supported, treat it as a **new verified booking/rebooking flow** and re-check availability;
5. otherwise hand off, but keep the acknowledgement grounded: cancellation already occurred and the clinic team will need to help restore/rebook it.

Also ensure generic handoff acknowledgement reuse cannot propagate a statement that contradicts a more recent verified lifecycle action/read.

Required regressions:

- cancel succeeds → immediate “سيبه زي ما هو”
- cancel succeeds → immediate “هو لسه موجود صح؟”
- cancellation status must remain grounded in canonical DB truth
- no claim that a completed cancellation “did not execute”
- any restoration must re-verify slot availability before a write

## Non-blocking observations

### C — device disambiguation wording/flow

After date/time/doctor narrowing, two device variants remained. On “احجز”, the system produced a generic ambiguity message before eventually asking for the device and booking correctly.

This is conversational friction, not a grounding/write defect. The DB result matches the final explicit Candela selection.

### D — “first appointment” paginates once before selection

After presenting the nearest later-day options, the first `اول ميعاد` request advanced availability presentation rather than selecting the earliest displayed option. Repeating `اول ميعاد` then booked correctly.

No unsafe write occurred. This is worth a future UX cleanup, but it is lower priority than F1/F2.

## Positive coverage confirmed

The sweep provides live evidence for all of the following on the tested main SHA:

- new laser booking with service/date/device/doctor/time narrowing
- separate second booking on the same patient/history
- device-specific service pricing
- no-availability recovery without hallucinated slots
- changing date/doctor constraints
- reschedule write correctness
- side-query preservation during an active booking
- canonical next-appointment read
- package-owned booking with correct package decrement
- explicit opt-out of package usage
- same-day booking
- clinic address / hours reads
- medical safety handoff
- social follow-up without duplicate writes

## Closeout

This sweep is **not a production-ready signoff** because F1 and F2 are P1 lifecycle/continuity defects.

Recommended next work should be narrowly scoped to those two root causes. No broad agent redesign is indicated by this evidence, and no product code was changed in this evaluation branch.
