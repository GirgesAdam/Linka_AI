# Linka — Real n8n Runtime

Linka/PostgreSQL remains the system of record. The current production architecture keeps WhatsApp transport inside Linka and uses n8n only as the automation / clinic-sync scheduler.

Patient communication in the current product contract is WhatsApp-based. There is no Gmail automation runtime in the project.

The production FastAPI backend currently runs at:

```text
https://tia-api-production-54c5.up.railway.app
```

The Oracle deployment package in `deploy/oracle-n8n/` may continue hosting n8n and its own PostgreSQL database, but it must not own WhatsApp inbound, provider sends, booking state, retry state, credentials, or delivery reconciliation.

## Active Oracle n8n workflow

The only production workflow that should remain active on Oracle n8n is:

```text
tia_automation_scheduler.json
```

It wakes Linka for appointment/CRM automations and clinic-sync work. It does not send WhatsApp messages itself.

Set this environment variable on the self-hosted n8n runtime:

```text
TIA_API_BASE_URL=https://tia-api-production-54c5.up.railway.app
```

Recent n8n versions can block `$env` access in expressions. If the dedicated Linka n8n runtime requires expression access to `TIA_API_BASE_URL`, use `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` only on this trusted dedicated instance, never on a shared instance with untrusted workflow authors.

For local Docker development, workflow JSON may keep `http://host.docker.internal:8000` as a fallback.

## Automation worker authentication

Create an Automation Worker from Linka and store the one-time token in an n8n Header Auth credential:

```text
Header: X-Automation-Token
Value: <worker_token>
```

Never put raw worker tokens in Git, workflow JSON, screenshots, or chat logs.

## Current WhatsApp architecture

Meta calls Linka directly, and Linka sends to Meta directly:

```text
Customer WhatsApp
→ Meta webhook
→ Linka /api/v1/channels/whatsapp/webhook/{connection_id}
→ CRM / AI / Booking / Handoff
→ Linka message_dispatches
→ Linka native Meta transport
→ Meta Graph API
→ Meta sent/delivered/read/failed webhook
→ Linka channel_delivery_events
```

Clinic Meta credentials stay encrypted in Linka. n8n does not hold a clinic App Secret, access token, WABA credential, Phone Number credential, or WhatsApp provider credential.

The shared Railway service `tia-whatsapp-transport-waker` wakes the native transport every few seconds through:

```text
POST /api/v1/channels/whatsapp/transport/tick
Header: X-Tia-Transport-Token: <CHANNEL_TRANSPORT_WORKER_TOKEN>
```

The backend then selects eligible clinic connections, decrypts the correct clinic credential only in memory, refreshes provider/template health, processes pending inbound events, claims due dispatches, sends to Meta, and records the provider result through Linka's state machine.

`n8n/workflows/tia_whatsapp_outbox_worker.json` remains a compatible alternative/reference waker for development or recovery, but it should not run in production while the Railway transport waker is active. Production must have one active transport waker, not two.

## Retired legacy WhatsApp workflows

Old Oracle n8n copies that normalize inbound WhatsApp, call the generic channel adapter, claim the generic outbox, send through n8n WhatsApp nodes, or report provider status are retired.

In particular, deactivate any old workflow that calls legacy channel-adapter outbox endpoints. Do not repair its old `X-Channel-Token` authentication just to keep it alive. The native Meta transport supersedes that path.

If Railway logs show repeated `401 Unauthorized` requests from user agent `n8n` against a legacy channel-adapter outbox endpoint while `tia-whatsapp-transport-waker` is returning `200`, that is evidence an old imported Oracle workflow is still active and should be disabled.

## Automation + incremental clinic sync

`tia_automation_scheduler.json` wakes Linka once per minute on two independent branches using the same `X-Automation-Token`:

1. automation planning/claiming for reminders and CRM jobs;
2. connector-driven incremental clinic sync via `/api/v1/automations/adapter/clinic-sync/tick`.

Automation enable/disable state and admin-selected timing live in Linka/PostgreSQL. n8n does not own separate per-rule schedules; it only wakes the backend.

The clinic-sync call is only a wake-up signal. Linka decides whether a workspace is enabled, due, already leased, or temporarily backed off, then owns deterministic sync state and durable checkpoints.

## Retry and safety ownership

- Linka owns WhatsApp send retries, inbound retries, outbox reclaim, idempotency, provider status and delivery reconciliation.
- n8n may retry idempotent automation scheduler wake-up requests but must not blindly retry provider sends.
- cancelled/rescheduled appointments cancel pending reminders where safe.
- no-show uses the same cancellation-recovery behavior as cancellation.
- changing automation timing replans queued jobs deterministically.
- disabling an optional rule cancels pending jobs.
- clinic provider tokens and App Secrets are encrypted at rest and never returned to the dashboard after save.
- a provider/account restriction for one clinic must not stop other tenants.

## Production verification

Use this order when checking the live runtime:

1. Confirm `tia-api` readiness is healthy.
2. Confirm `tia-whatsapp-transport-waker` logs show HTTP `200` from `/api/v1/channels/whatsapp/transport/tick` and at least one ready connection when a real connection is configured.
3. Confirm Oracle n8n keeps `tia_automation_scheduler.json` active.
4. Confirm no retired Oracle WhatsApp workflow is still calling the legacy channel adapter.
5. Send a provider test only to a phone number you control.
6. Verify Linka outbound `sent`, then Meta `delivered/read` callbacks and an inbound reply.
7. Enable one approved automation rule and verify a real reminder end-to-end.
8. Configure an external clinic connector only when a real clinic integration is ready.
