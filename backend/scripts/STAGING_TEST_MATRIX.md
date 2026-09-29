# Linka Full Staging Test Matrix — v0.13.0

This suite is designed for **staging only**. The seed script refuses to run when
`ENVIRONMENT=production`.

## Seeded data

The seed creates real PostgreSQL staging records for:

- 2 active branches
- 2 active doctors
- 3 active services
- doctor ↔ branch assignments
- doctor ↔ service assignments
- branch + doctor working hours
- booking settings
- active / inactive / blocked patients
- tags + tag assignments + notes
- leads: new / qualified / booked / lost
- conversations: open / pending / closed
- appointments:
  - pending
  - confirmed
  - checked_in
  - in_progress
  - completed
  - cancelled
  - no_show
  - rescheduled source + replacement
  - cancellation-policy scenario
  - lifecycle transition scenario
  - idempotency scenario
- handoffs:
  - pending medical
  - claimed urgent complaint
  - resolved customer request
- mock WhatsApp channel:
  - active connection
  - paused connection
  - channel identities
  - inbound processed + failed events
  - outbox queued + failed + read
  - sent / delivered / read delivery events
- automations:
  - real default rules
  - active staging automation worker
  - jobs in processing / dispatched / failed / skipped / cancelled states

## Automated regression coverage

### Auth / platform
- Supabase sign-in
- health live / ready
- `/auth/me`
- workspace membership
- dashboard summary
- onboarding/setup readiness

### Clinic configuration
- branches
- services
- doctors
- booking settings
- readiness = 100%

### CRM
- active / inactive / blocked filtering
- patient search
- duplicate phone conflict
- invalid phone validation
- notes
- tags
- leads by status
- conversations by status

### Booking
- every appointment state can be queried
- completed appointment cannot be confirmed
- completed appointment cannot be cancelled
- cancellation notice policy blocks normal cancellation
- Admin override works
- confirmed → checked_in → in_progress → completed
- idempotency key returns the existing appointment
- double booking is rejected
- real availability lookup
- real reschedule creates a replacement
- old appointment becomes `rescheduled`
- status history exists

### Human handoff
- pending medical queue
- claimed urgent complaint queue
- resolved history
- staff reply requires/creates an external dispatch
- resolve

### Channel Layer / WhatsApp contract
- active mock connection
- invalid adapter token
- paused adapter token
- inbound event acceptance
- inbound idempotency
- inbound → AI processing
- outbound claim
- provider send result
- delivered callback
- duplicate callback
- read callback
- secret-in-config rejection
- duplicate channel-account conflict
- early delivery callback reconciliation

No real Meta credentials are used.

### Automation Engine
- rule list
- job states
- invalid worker token
- planner + due-job claiming
- successful automation dispatch
- no-route skip
- cancelled-appointment cancellation

### AI Agent
- service facts
- unknown-service anti-hallucination
- blocked patient
- multi-turn real booking through the Agent
- persistence of Agent-created booking in PostgreSQL
- medical safety handoff
- AI pause during active handoff
- AI resume after resolve

### Frontend
- Next.js server reachability

## Intentionally manual/external

These cannot be truthfully proven by a local staging mock:

1. **Meta public-network delivery**
   - webhook subscription in Meta
   - real phone number
   - real template approval
   - Graph API acceptance
   - real recipient delivery/read

2. **n8n deployed execution**
   - imported workflows
   - actual n8n credentials
   - scheduler activation
   - production/staging callback URLs

3. **Member RBAC with a real Supabase session**
   - requires a second Supabase Auth user with Member role

Those are reported as `WARN`, not `FAIL`.
