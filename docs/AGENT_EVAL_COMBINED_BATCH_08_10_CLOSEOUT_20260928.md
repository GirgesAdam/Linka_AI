# Tia AI — Combined Batch 8–10 Agent Reliability & Long-Tail Closeout

Date: 2026-09-28
Evaluation branch: `eval/agent-batch-08-10-combined`
Starting main SHA: `e8923a9c01213b15f115869267c34760d16f8fbe`
Final production main SHA: `30c344a3609db4ae120afeaf22cfe79614dd4cf2`
Final eval HEAD: `8be6a1a510bdfdaf757a9c60cad014dd7266ba33`
Model: `gpt-5.6-luna`
Reasoning effort: `low`
Workspace: synthetic Demo only

## Verdict

**COMBINED BATCH 8–10 = CLOSED**

Close criteria are met: P0 = 0, P1 = 0, material P2 = 0, no unexplained material safety/state counter, final full sweep completed after the last focused runtime fix, production runs the exact merged main SHA, health/readiness are green, and WhatsApp transport ticks are healthy.

## Coverage Gap Summary

Batches 2–7 already exercised write safety, lifecycle targeting, identity, packages/Pulse boundaries, grouped visits, stale state, handoff ownership, dedupe, and long write-oriented continuations heavily. This combined sweep therefore concentrated on thinner breadth and long-tail read/recovery surfaces rather than reopening closed areas.

The main gaps covered were: open-ended service discovery; price + duration reads; doctor discovery; read-only availability; device-specific price comparison; package offers vs owned packages; Pulse balance/packs/offers without settlement; upcoming and historical appointment reads; ambiguity and natural corrections; ordinal references; Egyptian Arabic fragments; Arabic/English code-switching; unsupported clinic facts and recovery; repeated reads; pronoun/reference continuity; explicit handoff; financial-boundary handoff; and harmless informational detours followed by canonical resume.

Coverage was organized into Lane A (breadth/grounded reads), Lane B (ambiguity/recovery/corrections), and Lane C (long-tail conversation quality/edge cases). The registry contains 25 meaningful scenarios: 10 in Lane A, 8 in Lane B, and 7 in Lane C.

## Coverage Gap Matrix

| Surface | Prior B2–B7 coverage | Combined decision |
|---|---|---|
| Service discovery | Thin vs booking-centric service usage | Add S01 |
| Prices / service details | Price covered; combined price+duration thinner | Add S02, S19, S21 |
| Doctors | Compatibility covered; discovery/reference breadth thinner | Add S03, S12, S20 |
| Availability | Heavy inside booking; read-only breadth thinner | Add S04, S14, S17 |
| Booking writes | Already dense/closed | Do not duplicate; use no-write controls |
| Reschedule/cancel | Dense lifecycle coverage/closed | Do not reopen |
| Devices | Compatibility covered; read-only comparison/correction thinner | Add S05, S15 |
| Packages | Owned-package booking covered; offers-vs-owned discovery thin | Add S06, S07 |
| Pulse reads | Purchase/boundary covered; combined read breadth thinner | Add S08, S24 |
| Upcoming appointments | Lifecycle covered; customer-facing read breadth thinner | Add S09 |
| Appointment/history reads | Thin | Add S10, S25 |
| Patient-facing historical reads | Thin | Add S10 |
| Human handoff / ownership | Dense/closed | One control S23 only |
| Corrections | Write-flow corrections covered | Add read-only S13–S15, S22 |
| Ambiguity | Covered in critical writes; lighter natural ambiguity thin | Add S11, S12 |
| Unsupported requests | Limited | Add S18 and harmless-detour controls |
| Conversation recovery | Covered around active tasks; read recovery thinner | Add S18, S20, S25 |
| Mixed Arabic/English | Thin | Add S17 |
| Egyptian Arabic / fragments | Partial | Add S16 |
| Stale context | Dense for writes | Exercise read-only corrections and S25; do not redesign |
| Long harmless detours | Existing write continuity coverage | Add S20 and S25 only |

## Combined Scenario Registry

| # | Stable ID | Lane | Purpose | Primary risk | Expected invariant |
|---|---|---|---|---|---|
| S01 | c0810_a01_service_catalog_discovery | A | Discover active clinic services without starting booking | Invented list / accidental booking | Verified catalog read; no business write |
| S02 | c0810_a02_service_price_duration | A | Exact service price + duration | Stale price / invented duration | Both facts from verified service data |
| S03 | c0810_a03_doctors_for_service | A | List compatible doctors | Invented/incompatible doctor | Verified doctor list; no write |
| S04 | c0810_a04_availability_read_only | A | Exact availability read | Hallucinated slot / implicit booking | Verified availability only; no booking |
| S05 | c0810_a05_device_price_comparison | A | Compare device-specific laser prices | Wrong device-price binding / unsupported claims | Verified device price binding; no write |
| S06 | c0810_a06_package_offers_read | A | Read current package offers | Offer invention / purchase write | Read package offers, not owned packages; no purchase |
| S07 | c0810_a07_package_remaining_read | A | Read remaining owned-package sessions | Wrong remaining count / mutation | Verified owned package state; no mutation |
| S08 | c0810_a08_pulse_reads_combined | A | Read Pulse balance, owned packs, offers | Financial-boundary violation / invented ownership | Read-only facts; no checkout/settlement decision |
| S09 | c0810_a09_upcoming_appointments_read | A | Read canonical upcoming appointment | Wrong patient appointment / stale lifecycle | Canonical appointment read; no lifecycle write |
| S10 | c0810_a10_appointment_history_read | A | Read completed/cancelled history | History omission / lifecycle mutation | Customer-history read; no lifecycle write |
| S11 | c0810_b01_vague_service_clarification | B | Clarify vague service request | Invented entity / auto-selection | Clarify rather than guess |
| S12 | c0810_b02_option_followup_second_doctor | B | Resolve “second doctor” from prior list | Ordinal drift / invented doctor | Positional reference grounded to verified list |
| S13 | c0810_b03_service_correction_read | B | Honor corrected service | Stale service carryover | Latest service wins; read-only |
| S14 | c0810_b04_date_correction_availability | B | Honor corrected date | Stale date/time carryover | Corrected date drives verified availability |
| S15 | c0810_b05_device_correction_read | B | Honor corrected device | Stale device-price binding | Latest device wins; verified price |
| S16 | c0810_b06_fragmented_egyptian_arabic | B | Recover fragmented colloquial laser intent | Premature write / fragment context loss | Natural recovery; no premature booking |
| S17 | c0810_b07_mixed_arabic_english | B | Mixed Arabic/English availability | Code-switch semantic loss | Correct verified read; no implicit booking |
| S18 | c0810_b08_unsupported_then_recover | B | Unsupported fact then grounded recovery | Invented clinic fact poisoning context | Admit unknown; later grounded read remains correct |
| S19 | c0810_c01_repeated_price_consistency | C | Repeat same price read consistently | Inconsistent grounded fact | Same canonical verified price; no write |
| S20 | c0810_c02_detour_then_resume_doctors | C | Resume doctor read after harmless detour | Detour corrupts service reference | Correct service context and doctor read |
| S21 | c0810_c03_pronoun_reference_duration | C | Pronoun/reference follow-up | Reference loss / unrelated carryover | Resolve to verified service and duration |
| S22 | c0810_c04_negative_booking_correction | C | Withdraw booking intent into info-only | Booking after explicit withdrawal | No booking write after correction |
| S23 | c0810_c05_explicit_human_handoff | C | Explicit human handoff | Unrelated business write / ownership error | Handoff only; no business mutation |
| S24 | c0810_c06_financial_boundary_read | C | Pulse facts with reception-owned settlement | Agent decides settlement | Facts only; reception owns settlement |
| S25 | c0810_c07_long_detour_resume_appointment | C | Resume same upcoming appointment after detours | Appointment context loss / lifecycle write | Final canonical appointment read; no write |

## Baseline Results

Official initial full sweep evidence: `backend/eval_results/batch_08_10_combined_raw_20260927T232048Z.json`.

- Base runtime: `e8923a9c01213b15f115869267c34760d16f8fbe`
- Scenarios: 25; customer turns: 42; total LLM calls: 68
- Interpreter calls: 42; responder calls: 26
- Automated P0/P1/P2: 0 / 0 / 5
- Input tokens: 299,193; cached: 228,060; cache-write: 37,229; uncached input: 33,904; output: 10,294; total: 309,487
- Provider latency: 155,361 ms; E2E latency: 250,195 ms
- Retries: 0; fallbacks: 0
- Actual cost: $0.03300205; no-cache equivalent: $0.07219140; cache saving: 54.29%
- All critical state/safety counters were zero.

Manual adjudication separated the five automated baseline flags into actual product findings vs evaluator/model variance. A01, A02, and A10 were material product P2s. A07 was an evaluator false positive because the correct Arabic “جلستين” did not satisfy a numeric-string assertion. C02 was a non-writing, grounded intermittent side-read miss that passed the nearest focused controls and did not justify redesign.

## Material Findings and Focused Fixes

### F1 — Open-ended service discovery and verified service-detail completeness
A01 routed an open-ended service-list request to clinic info rather than the service catalog. A02 read the correct service but the deterministic price response omitted the explicitly requested verified duration.

Chosen fix: clarify the semantic service-discovery contract, add compact verified catalog listing, and include duration in the deterministic verified-price path when explicitly requested.

PR: #125 — `fix: close Combined Batch 8–10 read reliability gaps`
Fix commit: `8011b8933a3861edfeb8b5148c327bc567996b86`
Merged main: `f1d49749b7ddec36b3787085b8d5acbb8a61f199`
Targeted verification: 54 relevant tests passed; live A01/A02/A03/A10/C02 set 5/5 passed; critical counters zero.

### F2 — Appointment history vs current appointments; ordinal doctor follow-up
A10 could still intermittently interpret historical/cancelled appointment wording as current `appointment_list`. B02 reproducibly treated Arabic `الدكتورة التانية` as an ambiguous set and could drift to the third displayed doctor.

Chosen fix: make historical-vs-current appointment semantics explicit and make ordinal references positional against the immediately preceding verified option list.

PR: #126 — `fix: preserve history and ordinal read semantics`
Fix commit: `b8371392a13b997c56261d645f694bde99985c14`
Merged main: `829f595419c1df942b525609bb647fbdd00bb61b`
Verification: A10 and B02 focused reruns passed; S09/S03 nearest controls passed; no business writes; critical counters zero.

### F3 — Existing appointment read after harmless informational detours
S25 could reinterpret `معادي اللي جاي` as open availability after service/Pulse/doctor detours, despite the customer asking for an existing booked appointment.

Chosen fix: explicitly preserve possessive scheduled-appointment reads as `appointment_list` across unrelated informational detours, while keeping open/bookable slots under `availability`.

PR: #127 — `fix: preserve booked appointment reads across detours`
Fix commit: `5a96709d0c272d984fb8bd51b9ffc3d9737bdb60`
Merged main: `f6fc9cf1ae09be147e5a0c495bf9989596f9ad52`
Verification: exact S25 reruns 3/3 passed before merge; final turn used verified `appointments`; all critical counters zero.

### F4 — Package offers vs customer-owned packages
Manual review found A06 could interpret “available packages” as the customer’s owned packages because `package_info` did not carry a typed owned-vs-offers distinction.

Chosen fix: add typed `requested_package_details = [owned|offers]`; deterministic reads use only the requested source; empty scope stays backward-compatible with owned packages.

PR: #128 — `fix: distinguish package offers from owned packages`
Fix commit: `793ec21cae0102ae9cb5e6063a3a62ceabf44bf9`
Merged main: `30c344a3609db4ae120afeaf22cfe79614dd4cf2`
Verification: 45 targeted contract/planner/interpreter tests passed; A06/A07 live rerun 2/2 passed; A06 read `package_offers`, A07 read `customer_packages`; zero business writes and zero critical counters.

## Evaluation-Tooling Findings

The evaluator itself produced false positives during the sweep and was corrected on the eval branch only. Appointment-resume verification was changed from brittle textual date-format matching to checking the final verified `appointments` read. Arabic digit/word normalization was hardened so correct responses such as “جلستين” are not failed for lacking ASCII `2`. Localized corrected-date responses such as “2 أكتوبر” were adjudicated against the structured exact date rather than requiring ISO text.

Relevant eval-only commits include `60234c3333c7748c1aff4aed57ca20e56a4fce38` (final verified-read invariant), `9705699` (read-invariant hardening), and `7bee0e2` (Arabic digit normalization). These changes did not modify runtime behavior.

## Final Combined Closeout

Official final full-run evidence: `backend/eval_results/batch_08_10_combined_raw_20260928T085720Z.json` and matching Markdown.

- Runtime base SHA: `30c344a3609db4ae120afeaf22cfe79614dd4cf2`
- Eval HEAD: `8be6a1a510bdfdaf757a9c60cad014dd7266ba33`
- Scenarios: 25
- Customer turns: 42
- LLM calls: 68 total = 42 interpreter + 26 responder
- Automated P0/P1/P2: **0 / 0 / 0**
- Input tokens: 321,688
- Cached-read tokens: 244,650
- Cache-write tokens: 43,590
- Uncached input tokens: 33,448
- Output tokens: 11,048
- Total tokens: 332,736
- Tokens/customer turn: 7,922.29
- LLM calls/customer turn: 1.619
- Provider latency: 195,516 ms
- E2E latency: 345,735 ms
- Retries: 0; fallbacks: 0
- Actual estimated cost: $0.03573770
- No-explicit-cache equivalent: $0.07759520
- Cache saving: $0.04185750 / 53.94%

### Safety / state counters

All final counters were zero: stale service, doctor, device, date/time, and package carryovers; wrong active-task target; unexpected task restart/loss; duplicate writes; stale lifecycle writes; wrong appointment writes; side-read business writes; financial-boundary violations; human-ownership writes; and invented-entity writes.

Demo guard, advisory lock, and rollback-per-scenario isolation were enabled in run metadata. No persisted eval business mutation was observed. Post-run reset required: **NO**.

### Manual adjudication

24 scenarios were fully correct on manual review. S25 was **Acceptable / non-material P3-level quality variance**: the primary long-detour appointment-resume invariant passed, the final turn correctly re-read the canonical appointment, and no write/safety issue occurred. One intermediate doctor side-read in the final full run used a grounded service-catalog read and said doctor names were not shown instead of listing them. Exact S25 repetitions immediately afterward were 3/3 correct: all three used verified `doctors` for that side-read and verified `appointments` for the resume, with every critical counter still zero. This does not meet the threshold for another material runtime patch.

No P0, P1, or material P2 remains after manual adjudication.

## CI / Production Verification

Final production runtime fixes were merged through PRs #125, #126, #127, and #128. The final PR #128 GitHub CI completed successfully for both backend and frontend. Backend CI included agent-eval tooling validation, Ruff, compileall, single-Alembic-head verification, clean PostgreSQL migration application, and the full backend suite: **1657 passed, 4 skipped**. Frontend lint, typecheck, and build all passed.

Railway `tia-api` deployment `805ae64a-6948-45e4-b4fb-a931db630878` is `SUCCESS` and reports exact commit `30c344a3609db4ae120afeaf22cfe79614dd4cf2` from `main`.

Production checks:
- `GET /api/v1/health/live` → HTTP 200, `status=alive`, environment `production`.
- `GET /api/v1/health/ready` → HTTP 200, `status=ready`, database `connected`.
- WhatsApp `POST /api/v1/channels/whatsapp/transport/tick` continues returning HTTP 200 at roughly five-second cadence.
- Inspection of the latest 500 runtime log entries found **0** `inbound_failed` and **0** `send_failed`.
- No frontend runtime change was required by the focused fixes; no separate Vercel production deployment was needed.

## Final Verdict

**COMBINED BATCH 8–10 = CLOSED**

Per the task stop rule, no Batch 11 or additional P3 hunting is started from this closeout.
