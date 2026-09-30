# Linka Dashboard / Today Workspace milestone

Requested baseline after Patient Workspace: `91763d426742ee7da742eb7167529ac944afb8c2`
Actual starting `origin/main`: `b71c617a1fd63420df9658e60c443b85602e996d`
Branch: `agent/linka-dashboard-today-v2`

## Product principle

**The dashboard is not a report. It is the clinic’s operational starting point.**

The first viewport should answer, in order:
1. What needs attention today?
2. What is happening today?
3. What is coming next?
4. What broader context is useful after the immediate work is understood?

## Capability inventory

| Current capability | Current data contract | Operational meaning | Proposed priority/location | User action path |
| --- | --- | --- | --- | --- |
| Clinic setup readiness | `GET /clinic/setup-v2` → readiness.ready, progress_percent, missing[] | Whether the clinic has the minimum configuration Linka needs | Needs Attention only when incomplete; absent when ready | Open Setup |
| Setup progress | setup readiness contract | How much of the readiness checklist is complete | Inside incomplete-setup attention item | Open Setup |
| Missing setup requirements | setup readiness missing[] | Concrete blockers to full readiness | Inline under setup attention | Open Setup |
| Failed automation jobs | `GET /dashboard/summary` → failed_automation_jobs | Failed jobs across appointment-rule and CRM follow-up automation domains | Needs Attention, distinct from handoffs/tasks | Open Automations |
| Appointments today count | dashboard summary, clinic-timezone bounded; excludes cancelled/rescheduled | Total scheduled activity for the clinic’s local day | Today header/context | Open Appointments |
| Upcoming appointments count | existing dashboard summary: pending/confirmed from now onward | Forward appointment volume; currently includes the rest of today | Secondary overview only | Open Appointments |
| Active patient count | dashboard summary; patient.status == active | CRM population context, not urgent work | Secondary overview | Open Patients |
| Open handoff count | dashboard summary; pending/claimed handoffs | Human intervention workload | Needs Attention context, but items carry meaning | Open Inbox human-owned queue |
| Handoff queue | `GET /inbox/handoffs` priority ordered | Why/where staff must intervene | Needs Attention | Open conversation |
| Handoff patient | HandoffQueueItem.patient_name | Identity for action | Handoff row | Open Inbox conversation |
| Handoff priority | HandoffQueueItem.priority | Urgency within human attention | Semantic StatusBadge | Open Inbox conversation |
| Handoff reason | HandoffQueueItem.reason | Why intervention is required | Handoff row | Open Inbox conversation |
| Handoff ownership/assignee | HandoffQueueItem owner/assigned user | Who currently owns the conversation | Supporting row metadata when useful | Open Inbox conversation |
| Follow-up tasks | `GET /crm/tasks?scope=overdue` | Authoritative overdue CRM work | Needs Attention, separate from handoffs | Open Tasks / patient |
| Recent/upcoming appointment list | dashboard summary `recent_appointments` = next 8 pending/confirmed from now | Closest forward bookings, but can overlap today and after today | Replace with explicit Today and Coming Next partitions | Appointment deep-link |
| Patient identity in appointment | DashboardAppointmentRead | Who is arriving | Appointment row | Appointment deep-link |
| Service / doctor / branch | DashboardAppointmentRead | Operational preparation context | Appointment row | Appointment deep-link |
| Appointment status | DashboardAppointmentRead.status | Current booking lifecycle state | Shared StatusBadge | Appointment deep-link |
| Appointment price | DashboardAppointmentRead.price_minor | Financial detail exists but does not answer the Today action question | Remove from Dashboard row; remains appointment detail | Appointment deep-link |
| Partial handoff failure | Promise.allSettled handoff fetch | Secondary source unavailable | Local error inside Needs Attention | Open Inbox fallback |
| Partial setup failure | Promise.allSettled setup fetch | Readiness could not be verified | Local error inside Needs Attention | Open Setup fallback |
| Core summary failure | summary is required | Page cannot establish core Today counts/contracts | Remains page-critical | Existing error boundary |
| Empty appointments | empty recent_appointments | No forward appointments | Explicit Today/Coming Next empty states | Open Appointments |
| Empty handoffs | empty handoff queue | No current human escalation | Quiet/healthy state, not an alert | No forced CTA |

## Contract correction required

The existing `recent_appointments` list contains the next eight **pending/confirmed appointments from now**, regardless of whether they occur today or after today. The existing `upcoming_appointments` count has the same from-now meaning. Therefore the current UI text saying “after today” is stronger than the backend contract.

To avoid frontend timezone/date inference and duplicate appointments between Today and Coming Next, this milestone extends `/dashboard/summary` with explicit clinic-timezone partitions:
- `today_appointments`: operational appointments inside the clinic’s local day that are still pending/confirmed/checked-in/in-progress;
- `next_appointments`: pending/confirmed appointments strictly after the end of the clinic’s local day;
- `appointments_after_today`: authoritative count matching the same after-today boundary.

Existing summary fields remain for compatibility.
## Current → proposed hierarchy

### Before
1. incomplete setup promotion (when applicable)
2. failed-automation banner
3. four equal StatCards
4. one upcoming list
5. one handoff list

This makes totals the visual entry point even when specific work requires action.

### Proposed
1. **Needs Attention**
   - incomplete setup blocker when applicable
   - failed automation jobs
   - overdue CRM follow-ups
   - human handoffs with patient, priority, reason and ownership context
   - source-specific partial-failure states
2. **Today**
   - today count plus the nearest operational appointments today
   - patient, service, doctor, status, direct appointment link
   - no mini-calendar duplication
3. **Coming Next**
   - appointments after the clinic’s local day only
   - no duplicate appointment from Today
4. **Secondary overview**
   - active patient records
   - the existing forward-looking `upcoming_appointments` count, labelled explicitly as pending/confirmed appointments from now onward
   - lightweight links to their source areas

## Needs Attention model

Needs Attention is a container for different actionable domains, not a blended score.

- A handoff remains a handoff.
- An overdue CRM task remains a follow-up.
- A failed automation job remains an automation failure.
- Incomplete setup remains a readiness blocker.

Counts are displayed next to their own domain labels. They are never summed into one ambiguous “attention score”.

No-alert states stay quiet. Data that does not require an action does not become an alert merely because it exists.

## Data truth / wording

- `failed_automation_jobs` counts failed `AutomationJob` rows. The model supports both `appointment_rule` and `crm_follow_up`; Dashboard wording must therefore say **failed automation operations/jobs**, not “automatic messages”.
- `active_patients` is exactly patient rows with `status == active`. It is useful as secondary CRM context, not evidence of current clinic load.
- `appointments_today` is the clinic-local calendar day and excludes cancelled/rescheduled appointments.
- `upcoming_appointments` is retained as the existing from-now compatibility count; the new after-today count is used for Coming Next.
- No finance aggregate is introduced. Appointment prices are removed from the dashboard action list rather than reinterpreted as revenue/current balance.

## Partial failure behavior

Core `/dashboard/summary` remains critical.

Handoffs, setup readiness and overdue follow-ups are independent secondary sources loaded with `Promise.allSettled`. If one fails:
- its local surface explains that the data could not be refreshed;
- a direct fallback link remains available;
- Today, Coming Next and every other healthy section continue to render.

## Responsive direction

### Desktop
Needs Attention gets the strongest first-view hierarchy without turning every domain into a stat card. Today is the primary operational list. Coming Next and secondary overview are visually quieter.

### Mobile
Order is deliberate rather than desktop stacking:
1. actionable attention
2. Today appointments
3. human intervention/follow-up detail as part of attention
4. Coming Next
5. secondary overview

Rows must wrap long Arabic/English names without horizontal overflow, keep direct actions at least 40px tall, and avoid badge-heavy density.

## Implementation closeout

The milestone extends /dashboard/summary additively rather than changing existing fields:
- appointments_after_today: pending/confirmed appointments strictly after the clinic-local day.
- today_appointments: up to 8 non-terminal operational appointments inside the clinic-local day (pending, confirmed, checked_in, in_progress).
- next_appointments: up to 6 pending/confirmed appointments strictly after the clinic-local day.
- Existing recent_appointments, appointments_today, upcoming_appointments, handoff/channel counts and failed-job counts remain for compatibility.

The frontend removes the four equal-weight StatCards and renders the hierarchy as operational surfaces. Appointment rows keep patient, service, doctor, branch, status and direct appointment navigation. Price is intentionally not shown because this workspace is not a financial dashboard.

## Runtime / visual verification

A temporary, non-shipping preview route rendered the real DashboardWorkspace with synthetic contract-shaped data. A temporary public-path exception was used only to reach the harness without a local authenticated Supabase session. Both are removed before the final commit.

Chrome / Playwright verification ran at:
- 1440×900
- 768×1024
- 390×844

Scenarios: normal busy day, zero appointments today, multiple upcoming appointments, no handoffs, mixed-priority handoffs, failed automation jobs, incomplete setup, ready setup, handoff partial failure, setup partial failure, overdue-task partial failure, long Arabic/mixed content, empty clinic, and a new workspace with incomplete setup.

Across 42 scenario/viewport combinations:
- every preview returned HTTP 200;
- no horizontal overflow was detected;
- no visible link/button/input/select/summary target with meaningful width was below 40px;
- DOM order stayed Needs Attention → Today → Coming Next → Overview;
- Today and Coming Next had no appointment-detail link overlap.

Manual screenshot inspection covered first-viewport hierarchy, setup promotion visibility, mixed-priority handoff density, local failure states, long-name/service wrapping, appointment status readability, CTA competition, and mobile stacking.

### Known limitations

The preview validates rendering and responsive behavior with synthetic data, not a live authenticated workspace. Business semantics are guarded separately by the dashboard contract test, existing Appointments/Inbox/CRM tests, and full CI. No authoritative finance aggregate exists in the Dashboard contract, so none is inferred.

## Local validation

- Frontend lint: 0 errors; 2 pre-existing warnings in automations/page.tsx (unused Clock3 and Button).
- Next.js type generation: passed after clearing the temporary preview build cache.
- TypeScript typecheck: passed.
- Production build: passed on Next.js 16.3.4.
- Ruff: passed via uvx ruff check app tests alembic.
- Relevant backend/UI tests: 32 passed across Dashboard, CRM follow-up, Handoff/Inbox, Appointments milestone, and manual appointment contract coverage.

Full GitHub CI remains the final merge gate.
