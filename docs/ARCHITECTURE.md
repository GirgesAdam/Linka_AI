# Linka Architecture

Linka separates AI interpretation from business truth. The AI layer can interpret intent and select capabilities, while authorization, patient identity, booking constraints, payment accounting, package consumption/refunds, imports, and analytics calculations are enforced by deterministic application services and PostgreSQL constraints.

## Main layers

```text
Next.js UI
   ↓
FastAPI routes
   ↓
Domain/application services
   ↓
SQLAlchemy models + deterministic policy
   ↓
PostgreSQL / Supabase
```

AI flows call deterministic capabilities rather than replacing them. External actions are represented durably and executed through integration/channel workers such as n8n.

Key domains include workspaces/auth, clinic configuration, CRM, appointments, payments/allocations, packages/usage, messaging/channels, automations, analytics, historical import/provenance, and integration sync state.

Historical imports use stable patient identifiers/normalized phone rules, retain provenance, support explicit append/replace semantics, and never merge patients by name alone.
