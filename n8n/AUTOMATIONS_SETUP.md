# Linka Automations Engine — n8n Setup

## Architecture

PostgreSQL/Linka remains the source of truth. n8n is a shared platform scheduler only:
it wakes Linka on schedule. It does **not** hold a clinic's Meta access token, WhatsApp
credential, WABA ID, phone-number credential, booking state, reminder eligibility,
idempotency, retries, clinic-sync cursors, mapping, or financial decisions.

For WhatsApp, Meta calls Linka's webhook directly and Linka sends to Meta Graph API
directly with the encrypted credential belonging to the correct clinic connection.
Onboarding a new clinic does not require a new n8n workflow or WhatsApp credential.

## Current patient automations

The product intentionally exposes a small set of predefined automations instead
of a workflow builder:

- `appointment_reminder_6h` — appointment reminder. The historical key is kept
  for database compatibility, but the admin controls the timing.
- `post_visit_followup` — optional post-visit message that checks in, offers help
  or the next booking, and asks for feedback in one message.
- `cancellation_recovery` — optional recovery after a cancelled/no-show appointment.
- `lead_not_booked_followup` — optional follow-up for an interested lead that has
  not completed a booking yet.

There is no separate automatic booking-confirmation rule. A successful booking is
confirmed immediately by the customer AI in the booking conversation after the
deterministic booking operation succeeds. This avoids a duplicate delayed system
message for the same action.

Only the appointment reminder is enabled by default. Other optional features can
be enabled or disabled independently by the admin.

The admin can configure reminder/follow-up timing in minutes, hours, or days from
the Automations page. Linka stores the resulting `offset_minutes` on the rule and
replans pending jobs deterministically. There is no product-level seven-day timing
cap; planning query windows expand to cover the configured offset.

Legacy 24-hour, 2-hour, standalone no-show, and booking-confirmation rules may
still exist in old audit/history data, but they are not part of the current product
default rule set.

## Automation worker authentication

Create an Automation Worker from:

```text
POST /api/v1/automations/workers
```

The API returns a `worker_token` once. Store it inside an n8n Header Auth
credential:

```text
Header: X-Automation-Token
Value: <worker_token>
```

Never put the raw token into Git, workflow JSON, screenshots, or chat logs.

## Scheduler workflow

Import:

```text
n8n/workflows/tia_automation_scheduler.json
```

Set `TIA_API_BASE_URL` in the n8n environment and use the Automation Worker
header credential for the Linka HTTP nodes.

The workflow runs once per minute. It does not sleep until individual appointment
times. Linka plans idempotent jobs in PostgreSQL and returns only jobs that are due.
The same scheduler also wakes the clinic-sync runtime; the backend owns that sync
state and its retry/backoff behavior.

## WhatsApp direct onboarding

Embedded Signup is not part of the current product path. Each clinic owns its Meta
App, WABA, phone number, and System User credential. The clinic admin completes a
guided setup inside the **Automation** page.

The admin provides once:

1. Meta App ID;
2. Meta App Secret;
3. WhatsApp Business Account ID (WABA ID);
4. Phone Number ID;
5. System User Access Token with `whatsapp_business_management` and
   `whatsapp_business_messaging`.

Every input has a direct Meta link in the UI. Linka validates that the token belongs
to the entered app, checks the required scopes, verifies that the phone belongs to
the WABA, and stores the access token and App Secret encrypted per clinic.

Linka then generates a connection-scoped Callback URL and Verify Token. The admin
pastes those two values in the clinic Meta App's WhatsApp Configuration page,
clicks **Verify and Save**, and subscribes the `messages` webhook field. After that,
Linka subscribes the app to the WABA and owns provider/template health monitoring.

The callback shape is:

```text
GET/POST /api/v1/channels/whatsapp/webhook/{connection_id}
```

POST webhook bodies are validated with Meta's `X-Hub-Signature-256` HMAC using
the encrypted App Secret for that exact clinic connection.

Per-clinic credentials live in `channel_provider_credentials`. Raw access tokens
and App Secrets are never returned to the dashboard after they are saved.

The Linka platform itself only needs shared runtime configuration such as:

```text
META_GRAPH_API_VERSION
CHANNEL_CREDENTIAL_ENCRYPTION_KEY
CHANNEL_TRANSPORT_WORKER_TOKEN
```

The clinic's App ID/App Secret/WABA/Phone ID/System User token are **not** shared
platform credentials.

## WhatsApp templates

Linka owns the standard template catalog. The admin does not type template names and
does not need to decide which templates to create.

As soon as direct Meta credentials are accepted for a clinic, Linka checks the
clinic WABA and automatically creates **all** missing standard templates —
independent of which optional Automation toggles are enabled. The Automation page
shows every template and its live Meta status, for example `approved`, `pending`,
`rejected`, or a provisioning error. Linka keeps refreshing statuses automatically.

Current standard template names:

- `tia_reminder_01` — configurable appointment reminder;
- `tia_post_visit_01` — post-visit follow-up;
- `tia_cancellation_recovery_ar` — cancellation/no-show recovery;
- `tia_ai_followup_ar` — lead/AI CRM proactive follow-up outside the 24-hour window.

The WhatsApp transport sends the exact number of positional body parameters
required by the selected template. Current variable contracts are:

- appointment reminder — **3 parameters**: customer name, service, appointment time;
- post-visit follow-up — **3 parameters**: customer name, service, session date;
- cancellation recovery — **4 parameters**: customer name, service,
  cancelled/no-show appointment date, appointment time;
- AI/lead follow-up — **5 parameters**: customer name, follow-up goal, local date,
  local time, clinic name.

The reminder copy must stay timing-neutral because the admin controls when it is
sent. Do not hardcode "6 hours" or any other delay inside the approved template.
The current single-location experience also omits branch and appointment-date
placeholders from the reminder itself.

Canonical Arabic reminder and post-visit copy:

- `tia_reminder_01`: `أهلًا {{1}} 👋 بفكرك إن عندك جلسة {{2}} الساعة {{3}}. مستنيينك 💛`
- `tia_post_visit_01`: `إزيك {{1}}؟ حبيت أطمن عليكي بعد {{2}} اللي كانت يوم {{3}}. كل حاجة تمام؟`

A toggle never creates or submits a template to Meta. Template provisioning is a
connection-onboarding concern; toggles only express whether an optional automation
should run. In the UI an enabled rule may therefore show **في انتظار التجهيز**
until its own template is approved, and **شغالة** only when the WhatsApp connection,
webhook, native transport, and that rule's template are ready.

If a Meta-approved template is still pending, Linka waits instead of attempting an
invalid proactive send. If Meta rejects a template, the Automation page surfaces
that status. Existing dispatch safety only releases a proactive template when Meta
reports that exact template as approved.

The post-visit intent is intentionally one concise message: check how the visit
went, offer help or the next booking, and invite feedback. Do not split these into
multiple automatic messages.

### AI CRM follow-ups and the 24-hour WhatsApp window

Existing AI CRM follow-ups use free-form text only while WhatsApp's 24-hour
customer-service window is open. Outside that window, Linka does not try to bypass
Meta policy with free-form text.

The standard `tia_ai_followup_ar` Meta-approved template is automatically created
and recorded in the connection's `config.ai_followup_templates` / legacy
`config.ai_followup_template` compatibility keys. The admin does not configure a
template-name pool manually.

Outside the 24-hour window, the existing AI follow-up runtime uses the approved
standard template. If the required template is not approved yet, the existing CRM
follow-up path falls back to a human CRM task rather than attempting a
provider-rejected send.

This is transport safety for the existing CRM runtime; it is not a separate admin
automation engine.

## WhatsApp proactive-message safety

Linka stores WhatsApp opt-in separately from marketing consent. A customer inbound
WhatsApp message records the WhatsApp-contact opt-in, while staff can explicitly
record or withdraw it from the patient profile. Proactive templates and automation
sends are blocked when opt-in is missing.

Provider account-level failures such as Meta error `131031` pause only that
clinic's WhatsApp connection. The Automation page surfaces the Meta-side action
that the clinic admin must complete. Authentication expiry/error is surfaced as a
reconnection action. AI CRM follow-ups fall back to staff work instead of retrying
indefinitely.

Each clinic keeps its own encrypted WABA/phone credential, so a restriction on one
clinic does not stop other tenants.

## WhatsApp transport worker

Import once for the Linka platform:

```text
n8n/workflows/tia_whatsapp_outbox_worker.json
```

This shared workflow does **not** use an n8n WhatsApp node. It only wakes:

```text
POST /api/v1/channels/whatsapp/transport/tick
```

Use one platform Header Auth credential:

```text
Header: X-Tia-Transport-Token
Value: <CHANNEL_TRANSPORT_WORKER_TOKEN>
```

The backend then selects each clinic connection, decrypts that clinic's provider
credential only in memory, refreshes provider/template health, processes pending
inbound events, claims due outbox dispatches, sends directly to Meta, and records
the result through the existing dispatch state machine.

Do not create one n8n workflow or WhatsApp credential per clinic.

## Safety behavior

- cancelled/rescheduled appointments cancel pending reminders when they can still
  be safely recalled before provider send;
- no-show appointments use the same cancellation-recovery behavior instead of a
  second no-show automation;
- changing a rule timing replans queued jobs;
- disabling an optional rule cancels pending jobs;
- successful booking confirmation belongs to the AI booking response, not the
  automation scheduler;
- manual cancellation stays terminal;
- duplicate scheduler ticks do not create duplicate jobs;
- proactive WhatsApp routing can use the CRM patient phone when there is exactly
  one real sendable WhatsApp connection;
- provider tokens and App Secrets are encrypted at rest and never returned to the dashboard;
- generic adapter/worker tokens remain hashed where those legacy/provider-neutral
  paths are still used;
- permanent provider failures stop retries and surface the required action;
- stale/missing automation heartbeat is surfaced to the admin.

There is no Gmail automation worker in the current product runtime.

## Cancellation recovery

`cancellation_recovery` is an optional WhatsApp automation and is disabled by default.
The admin can enable it and choose how long after `appointments.cancelled_at` it should run.
A no-show is treated as a cancellation-recovery case using `appointments.no_show_at`.
It reuses the normal appointment automation job/outbox path; there is no separate cancellation or no-show workflow/state machine.
The Meta template is `tia_cancellation_recovery_ar` with four positional body parameters: customer name, service, original appointment date, and original appointment time.

## Lead not-booked follow-up

`lead_not_booked_followup` is optional and disabled by default. The admin chooses the delay after the lead's latest recorded contact, falling back to lead creation time.
The planner creates one idempotent system AI CRM follow-up task per lead and reuses the existing `crm_follow_up` AutomationJob runtime; there is no lead-specific job type or workflow engine.
Before sending, Linka verifies that the rule is still enabled, the lead is still `new`, `contacted`, or `qualified`, and no other active follow-up task is already handling that lead. `booked`, `won`, `lost`, and `spam` leads are not contacted by this automation.
Inside WhatsApp's 24-hour window the normal AI follow-up composer is used. Outside that window the automatically provisioned `tia_ai_followup_ar` Meta template is required to be approved.
