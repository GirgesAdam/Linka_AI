# Linka Agent — Historical Weak-Point Sweep Evidence Report

**Evaluation date:** 2026-10-01
**Evaluation branch:** `eval/historical-weakpoint-sweep-20261001`
**Mode:** evidence-only; no product-code fixes were made during this sweep.

## 1. Baseline and production identity

| Item | Evidence |
| --- | --- |
| Starting `origin/main` SHA | `a819425767061b9c919a74b38efd81a9690f0b5e` |
| Final `origin/main` SHA | `a819425767061b9c919a74b38efd81a9690f0b5e` |
| Railway production deployed SHA | `a819425767061b9c919a74b38efd81a9690f0b5e` |
| Railway deployment status | `SUCCESS` |
| Deployment commit message | Merge PR #176 — naturalness / choice / availability presentation fixes |
| Runtime timezone | `Africa/Cairo` |
| Primary runtime model observed | `gpt-6-luna` |
| Fallback model configured | `gpt-5-mini` |
| Fallback-model calls observed | `0` |

The sweep therefore evaluated the same commit that was deployed to Railway production. `origin/main` did not move during the evaluation.

## 2. Scenario coverage and runtime metrics

Coverage:

- 40 historical weak-point scenarios.
- 10 rephrased / variant scenarios.
- 50 total scenarios.
- 132 customer turns.
- 216 LLM calls.
- 0 scenario execution errors.
- 0 provider hard failures.
- 0 customer-visible ISO timestamp leaks.
- Write scenarios were executed against real business code inside transaction + outer rollback isolation so no evaluation booking, cancellation, package, Pulse, payment, or handoff artifact was intentionally left committed.

Token/runtime evidence:

| Metric | Value |
| --- | ---: |
| Input tokens | 1,147,869 |
| Cached input tokens | 869,709 |
| Cache-write tokens | 32,523 |
| Uncached input tokens | 245,637 |
| Output tokens | 54,839 |
| Total tokens | 1,202,708 |
| Input cache rate | 75.77% |
| Same-model retry attempts | 68 |
| Fallback-model calls | 0 |
| Deterministic-fallback turns | 54 |
| Availability-composer validation fallback events | 8 |
| Provider metadata-missing calls | 0 |
| Mean turn latency | 9.69 s |
| Median turn latency | 9.25 s |
| P95 turn latency | 15.75 s |
| Max turn latency | 20.09 s |

The 54 deterministic turns include normal contract/rendering paths and are not themselves failures. The eight logged availability-composer validation fallbacks were `AvailabilityComposerValidationError` events; the deterministic verified-truth renderer recovered the customer response without invoking the configured fallback model.

## 3. Findings classification

| ID | Finding | Severity | Evidence status |
| --- | --- | --- | --- |
| F1 | Correcting a selected cancellation target can still cancel the stale previously-selected appointment | **P0** | Confirmed twice on independent fixtures |
| F2 | Immediate natural-language revocation after a completed booking can leave the unwanted booking active | **P1** | Confirmed |
| F3 | Appointment temporal scope can be dropped while refining only the time constraint | **P2** | Confirmed targeted adjudication |
| F4 | Challenging a verified appointment time can temporarily produce a false no-appointments response | **P2** | Confirmed targeted adjudication |
| F5 | Availability presentation cursor can suppress valid results after doctor/device narrowing | **P2** | Confirmed + variant |
| F6 | Some “show me more” phrasings reset/repeat page 1 rather than advancing the verified availability page | **P2** | Confirmed variant |
| F7 | A follow-up after cancellation-policy handoff can return an empty customer reply | **P2** | Confirmed |
| F8 | Mixed appointment + booked-service price request can leave the price unit unresolved despite verified service identity | **P2** | Confirmed |
| F9 | Arabizi/code-switched availability can unexpectedly render in English | P3 | Confirmed |
| F10 | Some deterministic clinic/service/package replies remain verbose or system-like | P3 | Confirmed |

## 4. Reproduction evidence

### F1 — wrong destructive cancellation target (P0)

Initial fixture A contained two confirmed Hydrafacial appointments:

- appointment A: Tuesday 2026-10-06 10:00, doctor Omar Khalil.
- appointment B: Thursday 2026-10-08 10:00, doctor Mariam Hassan.

Conversation:

1. Customer: `عايز ألغي ميعاد`
2. Linka presents both appointments.
3. Customer: `اللي يوم الثلاثاء 6/10`
4. Linka asks whether the Tuesday appointment should be cancelled.
5. Customer corrects: `لا قصدي اللي يوم الخميس 8/10`

Current-turn interpreter evidence correctly identified Thursday 2026-10-08 as the new `source_appointment`. Planner evidence nevertheless verified and wrote against appointment A. DB delta recorded appointment A as changed/cancelled while B remained unchanged.

First reproduction IDs:

- A = `601a5300-c63f-4d29-9086-812ae58a2c7d`
- B = `7903f14f-67f0-4564-a214-13cc158b8ec8`
- destructive write target = A

Independent rerun with fresh fixture IDs:

- A = `3af685f5-a6d2-4eaf-9e8a-626678d3d73e`
- B = `0d05b654-7039-49aa-8414-706a53c4b55b`
- destructive write target = A again

This establishes a reproducible wrong-target destructive write rather than evaluator noise or one stochastic model output.

### F2 — immediate booking revocation

Conversation:

1. Customer explicitly books Hydrafacial on 2026-10-08 at 10:00 with Mariam Hassan.
2. Booking write completes.
3. Customer: `لا متحجزش، بس وريني المتاح`

The second turn was interpreted only as informational availability. No cancellation operation was emitted, so the newly-created appointment remained confirmed inside the evaluation transaction. The subsequent availability read then reported 10:00 as unavailable because the just-created booking itself occupied the slot.

Targeted adjudication with the clearer wording `لا، الغِ الحجز ده، أنا كنت بس عايز أشوف المتاح` successfully cancelled the newly-created booking. The cancellation capability is therefore present; the gap is immediate correction/revocation interpretation.

### F3 — temporal scope loss on refinement

Targeted fixture contained:

- 2026-10-02 14:00 PRP appointment.
- 2026-10-09 14:00 PRP appointment.

Conversation:

1. `عندي حاجة الأسبوع ده بعد 5؟`
2. `طب قبل 5؟`

Turn 1 read parameters correctly included the current-week date range and `after 17:00`. Turn 2 retained only `before 17:00` and lost the week date range. The verified read therefore returned both 2 October and 9 October, crossing the original temporal scope.

### F4 — stale-time challenge

Fixture truth: next Hydrafacial appointment = 2026-10-02 14:00.

Conversation:

1. `ميعادي الجاي إمتى؟` → correctly returns 14:00.
2. `مش كان الساعة 18:00؟`

The second turn was interpreted as an appointment-list read filtered to exact 18:00. That verified read correctly found no row under the incorrect filter, but the customer-facing result became `مفيش مواعيد جاية في السجل المؤكد الحالي.` A later broad challenge (`متأكد؟`) returned the real 14:00 appointment again.

### F5 — availability narrowing versus presentation cursor

Doctor narrowing reproduction:

1. Customer asks Hydrafacial availability for 2 October.
2. Linka presents verified Ahmed + Youssef windows.
3. Customer asks `طب مع أحمد محمود بس؟`
4. Backend performs a fresh verified read scoped to Ahmed.
5. Presentation replies `مفيش فترات إضافية في نطاق البحث الحالي.`

Device narrowing reproduction:

1. Customer asks underarm laser availability for 3 October.
2. Linka presents verified Candela + Prime ranges.
3. Customer: `Prime`
4. Backend performs a fresh read with `device_key=prime_lase`.
5. Presentation replies `There are no additional verified ranges in the current search scope.`

The valid windows exist in the new narrower scope; the old shown-window cursor suppresses them.

### F6 — availability “more” continuation

The canonical Arabic path `طب في غير دول؟` → `هاتلي كمان` paginated correctly through later windows. A mixed-language variant `غير كده؟` → `more` did not preserve continuation semantics; it repeated/reset the first verified page.

### F7 — empty reply after handoff

Near-term cancellation correctly created a handoff because cancellation policy required staff review. On the next customer turn `حتى لو أنا موافق؟`, handoff remained pending but `final_reply` was empty and no LLM call was made for that turn.

### F8 — mixed appointment/service completeness

Customer: `ميعادي الجاي إمتى ومع مين؟ والخدمة اللي حاجزها سعرها كام؟`

The appointment contract verified and rendered the next Hydrafacial appointment, but the mixed path returned `محتاج معلومة إضافية عشان أكمل.` instead of using the already-verified booked-service identity to read its price in the same turn.

## 5. DB before/after evidence for write scenarios

All rows below were observed before outer rollback. “After” describes the transaction-local state/delta.

| Scenario | Write | DB before | DB after / verified delta |
| --- | --- | --- | --- |
| S14 | booking | No target appointment for selected slot in fixture patient | One appointment created for selected Hydrafacial slot |
| S15 | booking after service correction | No target PRP booking | One PRP appointment created for chosen doctor/time |
| S16 | booking after doctor correction | Requested first doctor unavailable | One Hydrafacial appointment created with corrected doctor |
| S17 | laser booking after device correction | No target appointment | One underarm-laser appointment created with Prime Lase |
| S18 | reschedule | One confirmed source PRP appointment | Source appointment changed to rescheduled lifecycle state + replacement appointment created |
| S19 | reschedule + doctor change | One confirmed source PRP appointment | Source changed + replacement appointment created with requested doctor |
| S21 | reschedule + service change | One confirmed Hydrafacial source | Source became rescheduled; replacement confirmed appointment created as PRP Skin |
| S22 | cancellation | Two confirmed candidate Hydrafacial appointments | **Wrong candidate A changed/cancelled; intended corrected candidate B unchanged** |
| S23 | policy handoff | Confirmed near-term appointment | Appointment not destructively changed; handoff created |
| S24 | package-backed booking | Active PRP package with 3 sessions remaining | Booking/package usage created; subsequent verified balance = 2 remaining |
| S26 | laser booking without Pulse consumption | Existing Candela Pulse balance = 1000 | Appointment created; verified Pulse balance remained 1000 |
| S27 | Pulse-pack purchase | Existing patient Pulse state | 1000-Pulse pack added; no payment transaction was implied by the purchase write |
| S39 | direct booking then weak revocation wording | No target booking | Booking created and remained confirmed after `لا متحجزش، بس وريني المتاح` |
| V14 | booking after side question | No selected Hydrafacial booking | One selected-slot appointment created |
| V18 | reschedule variant | One confirmed source PRP appointment | Source changed + replacement appointment created at requested date/time |
| Adjudication A4 | explicit post-booking undo | No target booking, then one newly-created confirmed booking | Same new booking changed to `cancelled` after explicit `الغِ الحجز ده` |
| Adjudication A5 | service-change reschedule | Confirmed Hydrafacial source | Source = `rescheduled`; replacement = confirmed PRP Skin appointment |

No evaluation payment write occurred from `اعتبرني دفعتهم كارت`; that request produced a payment handoff. No cross-patient appointment disclosure/write occurred in the privacy scenario.

## 6. Root-cause locations

### F1 lifecycle identity precedence

Primary location:

- `backend/app/services/agent_v2/planner.py`
- helper: `_source_appointment_parameters()`

Observed precedence:

1. valid `pending_choice`
2. `purpose == "appointment_target"`
3. matching lifecycle action
4. singleton pending option
5. return its `appointment_id`

That happens before current-turn `operation.source_appointment` constraints are resolved. As a result, an explicit latest correction can be understood correctly by the interpreter but still lose to the stale singleton target in planning.

Supporting lifecycle state locations:

- `backend/app/services/agent_v2/orchestrator.py`
  - `_appointment_choice_snapshot()`
  - `_selected_appointment_snapshot()`
  - `_pending_choice_from_context()`
- `backend/app/agents/v2/turn_interpreter.py`
  - lifecycle/pending-choice semantic instructions
- `backend/app/services/agent_v2/planner.py`
  - `_plan_select_active()`

### F3 temporal refinement

The failure occurs before the read executor: current-turn understanding/planning drops the prior verified date scope while changing only the time predicate. The backend read then faithfully executes the too-broad parameters.

### F5 availability presentation scope

Relevant locations:

- `backend/app/services/agent_v2/live_chat.py`
  - persistence of `availability_presented_window_keys`
- `backend/app/services/agent_v2/orchestrator.py`
  - `_availability_presentation_continuation()`
  - `_availability_shown_window_keys()`
- `backend/app/agents/v2/availability_composer.py`
  - `_paged_contract()`
- `backend/app/agents/v2/availability_pagination.py`
  - `select_availability_window_page()`

The shown-window cursor is carried on continuation without a strong scope identity proving that doctor/device/date/time constraints are still the same presentation scope.

## 7. Fallback statistics

Across the 50-scenario sweep:

- deterministic-fallback turns: 54
- logged availability-composer validation fallback events: 8
- configured fallback-model calls: 0
- same-model retry attempts: 68

The eight logged events were availability composer strict-validation failures and were recovered by deterministic verified-data rendering. They occurred in S16, S33, S37, S38 (two turns), V05, V08, and V38.

No observed fallback event changed canonical availability truth into a fabricated slot.

## 8. Regression and CI gates

Focused regression suite executed from the evaluated baseline:

`266 passed`

Static gates:

- Ruff: PASS
- `python -m compileall`: PASS
- `git diff --check`: PASS
- Alembic: single head
- Alembic head: `0086_all_service_packages`

CI evidence from the merged code lineage used by production:

- CI workflow conclusion: success
- Backend job: success
- Frontend job: success
- Clean migration path exercised through `0086_all_service_packages`
- Full backend CI result: `1990 passed, 4 skipped`

## 9. Production health evidence

After the sweep:

`GET /api/v1/health/live`

- HTTP 200
- `status=alive`
- `app=Linka`
- `environment=production`

`GET /api/v1/health/ready`

- HTTP 200
- `status=ready`
- `database=connected`

WhatsApp transport/waker evidence during the same closeout window repeatedly showed:

- tick HTTP 200
- `connections_checked=1`
- `connections_ready=1`
- `provider_refreshes=0`
- `inbound_failed=0`
- `send_failed=0`

## 10. Historical fixes that held

The sweep did not reopen the following as product failures: PRP skin/hair ambiguity, change-of-mind service selection, service price/duration grounding, doctor compatibility, normal availability ranges, preserved real gaps, standard availability pagination, direct this-week/next-week appointment reads, explicit date ranges, booking resume after side questions, service/doctor/device changes before booking, standard reschedule, reschedule + doctor change, conflict rejection with nearest alternative, reschedule service change, package balances and package-backed booking, exact package decrement, exhausted package handling, Pulse balance and no automatic Pulse consumption, Pulse purchase versus payment separation, device-specific Pulse overage, urgent medical handoff, payment handoff, cross-patient privacy, duplicate-name patient isolation, human date/time formatting, and explicit post-booking cancellation.

## 11. Final verdict

**Historical Weak-Point / Production Readiness Sweep: NOT CLEAN**

Blocking reason:

> **F1 / P0:** a current explicit cancellation-target correction can be overridden by a stale singleton `pending_choice`, producing a verified but wrong destructive cancellation write.

Priority order identified by the sweep was F1 first, then F2, temporal continuity/stale challenge, availability scope/cursor, empty handoff continuation, and mixed completeness. Per evaluation scope, no product fix was made on this branch.
