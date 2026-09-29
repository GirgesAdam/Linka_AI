# Linka v0.18.0 — External Integration Architecture

## Boundary

Linka owns intent, policy, CRM state, booking state, handoffs, messages, dispatch
state and automation idempotency.

n8n owns external provider credentials and provider execution.

No Meta access token, Google OAuth token, Gmail password, or raw Linka adapter
credential is stored in `channel_connections.config`.

## Customer contact contract

Linka does not collect or persist patient/customer email addresses. Customer identity
and CRM contact resolution use the external entity link and normalized phone number.
The old patient-email semantic capability and `send_email_to_customer` tool were
retired in v0.37.4.1. Staff/admin/doctor account emails remain separate operational
fields and are unaffected.

## Delivery truth

Queued != sent != delivered/read.

Agent tool success means durable outbox persistence only. Provider results are
recorded by the adapter and remain the source of delivery-state transitions.

## Multi-tenant scope

Every adapter token resolves one channel connection and therefore one
workspace. Every automation worker token resolves one workspace. A workflow
using one of these credentials cannot claim another workspace's dispatches.

## No new migration

v0.18 reuses the existing channel connection, identity, conversation, message,
dispatch and automation worker tables. No database migration is required.
