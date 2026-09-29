# Linka — WhatsApp Business Cloud

Linka owns the WhatsApp runtime directly. n8n is not the WhatsApp transport and is not the source of truth for conversations, delivery state, retries, booking state, or clinic credentials.

## Current architecture

```text
WhatsApp Business Cloud
        |
        v
Linka /api/v1/channels/whatsapp/webhook/{connection_id}
        |
        v
Linka + CRM + Booking + Handoff
        |
        v
Linka message_dispatches
        |
        v
Linka native Meta transport
        |
        v
WhatsApp Business Cloud
        |
        v
sent / delivered / read / failed callbacks
        |
        v
Linka channel_delivery_events
```

Meta calls Linka's webhook directly. Linka validates the webhook signature with the encrypted App Secret for that clinic connection. Outbound messages are sent from Linka to Meta Graph API using the encrypted provider credential for that connection.

The shared Railway service `tia-whatsapp-transport-waker` wakes the transport through:

```text
POST /api/v1/channels/whatsapp/transport/tick
Header: X-Tia-Transport-Token: <CHANNEL_TRANSPORT_WORKER_TOKEN>
```

The waker does not contain clinic Meta credentials. It only wakes Linka's native state machine.

## Clinic setup

Each clinic supplies its own Meta App / WhatsApp configuration through Linka's guided setup. Linka stores provider secrets encrypted and never returns the raw App Secret or System User Access Token after save.

The connection-specific webhook is:

```text
GET/POST /api/v1/channels/whatsapp/webhook/{connection_id}
```

The POST webhook is verified with `X-Hub-Signature-256` before inbound payloads are accepted.

## n8n responsibility

Oracle n8n remains useful for the Linka automation scheduler and clinic-sync wake-up. It does **not** need:

- a WhatsApp Trigger credential;
- a WhatsApp Business Cloud send credential;
- a per-clinic `X-Channel-Token` for the normal production WhatsApp path.

The production Oracle workflow that should stay active is:

```text
n8n/workflows/tia_automation_scheduler.json
```

`n8n/workflows/tia_whatsapp_outbox_worker.json` is only an alternate/reference native-transport waker and should not be active while the Railway waker is running.

## Retired legacy bridge

Older deployments used n8n to receive WhatsApp, call a generic channel adapter, claim Linka outbox rows, send through n8n provider nodes, and report provider results. That bridge is retired.

If an old Oracle n8n workflow is still repeatedly calling a generic channel-adapter outbox endpoint and receiving `401 Unauthorized`, disable that workflow. Do not weaken Linka authentication or rotate an old channel token just to preserve the retired path.

## Delivery and retry ownership

Linka owns:

- inbound idempotency;
- outbound dispatch claiming;
- provider-send retries;
- permanent failure handling;
- Meta message ID reconciliation;
- sent/delivered/read/failed callbacks;
- WhatsApp customer-service-window policy;
- template selection/approval checks;
- human handoff suppression/return-to-AI state.

This keeps provider behavior and business state in one deterministic state machine instead of splitting them between Linka and n8n.

## End-to-end verification

Use a phone number you control for live provider testing:

```text
Customer sends WhatsApp text
→ Meta calls Linka webhook
→ Linka resolves/stores inbound conversation
→ Linka processes the message
→ Linka queues outbound reply
→ Railway transport waker wakes Linka
→ Linka sends to Meta
→ Meta returns provider message ID
→ Linka records sent/delivered/read state
```

For current operational details, see `n8n/AUTOMATIONS_SETUP.md` and `n8n/REAL_RUNTIME_SETUP.md`.
