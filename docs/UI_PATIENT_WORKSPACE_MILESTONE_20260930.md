# Linka Patient Workspace milestone

Baseline: `49d71072bdb0628fd8ca66dcdda412b25856048f`
Branch: `agent/linka-patient-workspace`

## Product principle

**One patient. One connected operational record.**

The workspace keeps current truth separate from historical events. The profile aggregation remains the source for CRM summary/timeline; package, pulse and payment truth stays on the existing booking/payment contracts. No frontend-derived balance or entitlement is introduced.

## Capability inventory

| Current capability | Current context | Business rule / dependency | Proposed UI location | User action path after redesign |
| --- | --- | --- | --- | --- |
| Name/phone search | Patients list | Backend `q` matches first/last name and phone | List controls | Search → open record |
| Status/source filtering | Backend list contract | Exact patient enums; no inferred activity status | List controls | Filter → open record |
| Mobile patient cards | Patients list | Same record identity as desktop | Mobile list | Scan identity/status/contact → open |
| Desktop patient table | Patients list | Same API data | Desktop table | Scan → open |
| Patient status/source/tags | Profile | Current patient record truth | Identity strip | Read current identity state |
| Last contact | List/profile | Stored patient field; not equivalent to active status | Identity/current context | Read timestamp |
| WhatsApp consent | Profile action | PATCH records/withdraws opt-in and timestamp/source | Patient data card | Review → record/withdraw |
| Appointment summary | Profile aggregation | Current CRM stats only | Operational summary | See next appointment → patient appointments |
| Conversations/handoffs | Profile aggregation/timeline | Conversation/handoff remain Inbox-owned domains | Operational summary + timeline | Open latest/deep-linked conversation |
| Notes | Profile + timeline | Note type/content/pinned state from CRM | Quick action + recent notes + timeline | Add note → visible chronologically |
| Follow-up tasks | Profile + Tasks | AI/human execution; due time; conversation association | Quick action + attention summary + timeline | Create → monitor in Tasks/timeline |
| WhatsApp follow-up constraint | Follow-up form | AI outbound outside 24h requires approved template | Inline warning | Review template path before scheduling |
| Packages | Patient package panel | Sessions independent from payment state | Treatment/entitlements area | Review → pay/purchase/cancel if allowed |
| Remaining/reserved/consumed sessions | Package contract | Backend-calculated current entitlement | Active package record | Read current package truth |
| Package payment/balance/refund | Package contract | Ledger-backed values; no frontend inference | Package financial detail | Record payment / admin cancellation |
| Package cancellation | Profile package panel | Admin only; deterministic quote required | Package disclosure | Review settlement → cancel/refund |
| Pulse balances | Pulse wallet contract | Device-specific; consumption after actual use | Treatment/entitlements area | Read per-device balance |
| Pulse packs/payments | Pulse contracts | Pack status and ledger values are backend truth | Pulse detail | Purchase / record additional payment |
| Payment/refund events | Unified timeline | Historical event, not current balance | Timeline | Inspect event → appointment when linked |
| Appointment events | Unified timeline | Creation and lifecycle status are historical | Timeline | Inspect → filtered appointments |
| Messages | Unified timeline | Sender/channel/delivery are historical message facts | Timeline | Inspect → Inbox |
| Handoffs | Unified timeline | Handoff event is history; active count is current summary | Timeline + attention summary | Inspect → Inbox |
| Actor identity/timestamp | Unified timeline | Backend actor metadata | Every timeline event | Understand who did what and when |
| Empty states | Lists/profile panels | No fabricated state | Contextual empty state | Understand next available action |

## Current → proposed workflow

Previously the profile opened with four equal-weight stat cards, then a timeline beside a long rail of identity, actions, packages, pulses and notes. That made historical volume compete with current operational state.

The redesigned hierarchy is:
1. identity and communication consent;
2. next scheduled action, attention state, and latest contact;
3. contextual primary actions (appointments/conversation);
4. current treatment entitlements and their financial truth;
5. notes/follow-up controls;
6. unified chronological history.

Desktop keeps the timeline as the broad history surface and current patient context as the supporting rail. On mobile/tablet the current patient state, packages/pulses and actions intentionally precede the long timeline instead of merely stacking the desktop order.
## Timeline model

The timeline remains one chronological source of truth. Events stay compact: icon, primary description, actor, timestamp, semantic state where applicable, then only the event-specific detail and contextual deep link.

Current state is deliberately not inferred from timeline history:
- historical package purchase does not imply an active package;
- a payment event does not imply current balance;
- a resolved handoff event does not imply an active handoff;
- an appointment status event does not replace the current appointment contract.

Pinned note metadata is surfaced as an operational cue without turning patient identity into a badge collection.

## Packages / pulses

Packages retain effective status, purchased/reserved/consumed/remaining sessions, paid/sale/balance/refund amounts, additional payment, purchase, and admin cancellation/refund behavior.

Pulses retain per-device balance, purchased/consumed/remaining counts, active pack count, pack financial state, purchase, and additional payment. The UI does not combine balances across devices.

## Visual language

Legacy teal interaction accents in the Patient area move to Linka derived interaction tokens. Green/amber/red remain semantic. Shared StatusBadge is used for patient, appointment and task state where the shared status contract applies.

## Known contract boundaries

The profile aggregation does not expose a single authoritative cross-domain patient financial total. The UI therefore does not invent one. Package and pulse financial state are presented in their domain contracts; payment/refund events remain historical timeline evidence.

The timeline endpoint is capped and the UI requests 75 events. No client-side claim is made that this is an unbounded ledger.
## Verification target

Responsive review:
- 1440×900
- 768×1024
- 390×844

State coverage:
- no appointments / upcoming appointment / long history;
- active and partially used package / outstanding package payment / cancellation-refund path;
- device pulse balance / pulse pack payment / no entitlements;
- WhatsApp opt-in / opt-out;
- open and overdue follow-up / AI and human follow-up;
- active handoff;
- message/payment/refund timeline events;
- long Arabic and mixed-language content / long patient names / empty states.

Engineering gates:
- Patient Workspace contract tests
- relevant CRM/timeline/package/pulse/payment backend tests
- frontend lint
- frontend typecheck
- production build
- full CI before merge

## PR #153 review fixes

The pre-merge review tightened operational truth without changing backend business semantics:

- **Next action ordering:** `next_task_at` and `next_appointment_at` are compared directly; the earlier timestamp owns the card and its contextual CTA. An overdue/open task can therefore appear ahead of a later appointment.
- **Contextual booking:** the profile's primary booking action opens patient-filtered Appointments with the existing standard `ManualAppointmentForm` pre-bound to the patient. It reuses existing availability, package and pulse contracts; no booking endpoint or write semantics changed.
- **Attention disclosure:** overdue tasks and active handoffs keep separate labels when both are present; their sum remains the displayed attention count.
- **Patient-specific follow-up context:** Tasks now accepts the backend's existing `patient_id` filter so next-task and overdue-task links stay scoped to the patient.
- **Filtered empty state:** name/phone search, status and source all count as active filters. A zero-result filter state now says no matching results and offers a clear “show all patients” action.
- **Linka cleanup:** the remaining Patient-area teal border was replaced with the neutral shared border token; semantic green/amber/red remain semantic.
- **Pinned notes:** `is_pinned` is presented neutrally as “مثبتة”. The pre-existing profile write behavior remains unchanged; the UI no longer equates the persistence flag with a new “important for the team” product meaning.
- **Mobile touch targets:** Patient Workspace collapsible summaries and profile/package settlement actions were raised to the shared 40px interaction height where they are direct controls.

## Actual runtime / visual verification

Because the local authenticated Supabase session was not available in this worktree, verification used a **temporary, non-shipping synthetic preview harness** that rendered the production Patient components/styles and the existing contextual booking form. The preview route, temporary public-path allowance, Playwright script and screenshots were removed before the final commit; no auth bypass or test route ships with the milestone.
Playwright drove installed Chrome at the requested viewports:

- **1440×900**
- **768×1024**
- **390×844**

The synthetic state matrix covered:
- earlier overdue follow-up with a later appointment, and a separate appointment-only state;
- overdue tasks + active handoff together;
- long patient identity and mixed Arabic/English labels;
- active/partially consumed package with outstanding balance and payment form;
- multiple device-specific pulse balances and pulse-pack payment;
- no package/pulse entitlement;
- WhatsApp opt-in and opt-out presentation;
- long chronological timeline and long Arabic/mixed-language note;
- empty patient activity/current-action state;
- filtered Patient List with zero results;
- the real existing-patient `ManualAppointmentForm` at desktop/tablet/mobile sizes.
Automated DOM checks on every scenario/viewpoint reported:
- HTTP 200 for every preview state;
- no product-content horizontal overflow (`scrollWidth == viewport width`);
- no form/button/summary control below 40px after the touch-target pass.

Manual screenshot inspection confirmed PageHeader action wrapping, current-state card stacking, package/pulse density, booking/profile form layout, timeline readability, long-text wrapping and filtered/empty states at all three breakpoints.

### Verification limitation

The visual pass validates layout/runtime rendering with synthetic data, not an authenticated local backend session. Business behavior remains separately guarded by contract/relevant backend tests and the full GitHub CI suite. No production capability is represented in the preview that is not backed by an existing application contract.

## Review-fix validation results

Local frontend validation after the PR review fixes:
- `npm run lint`: 0 errors; 2 pre-existing warnings remain in `automations/page.tsx`.
- `npx next typegen`: passed.
- `npm run typecheck`: passed.
- `npm run build`: passed on Next.js 16.3.4.

Relevant backend / UI contract validation:
- 72 tests passed, 1 existing date-boundary package test deselected locally.
- The deselected case uses local `date.today()` against UTC runtime time and is unreliable during the Cairo/UTC date rollover; no package logic was modified by this milestone.
- Full GitHub CI remains the authoritative all-suite gate before merge.

The temporary preview harness and public-path exception were removed before staging the final review-fix commit.
