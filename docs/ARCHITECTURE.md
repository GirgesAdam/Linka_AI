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

## Appointment booking vs attendance confirmation

Booking creation and attendance confirmation are separate product events. A newly created or rescheduled Linka appointment is persisted as `pending` with `confirmed_at = NULL`; a successful booking response acknowledges that the appointment was recorded and must not claim that attendance was confirmed. The `confirmed` status is written only by the canonical explicit confirmation operation after the backend validates the appointment and, for customer confirmation, the configured local-date confirmation window.

The pre-appointment automation uses the historical database key `appointment_reminder_6h`, but its current product meaning is an **attendance-confirmation ticket**. It may ask the patient to confirm attendance or start the locked reschedule flow; it is not a delayed duplicate booking-success message. Structured action IDs, not visible Arabic text, authorize those writes.

**Reception UI invariant:** the reception-facing Arabic label `قيد الانتظار` is intentional stable product copy. Backend appointment/attendance-confirmation refactors must not rename or reinterpret that visible label. Change it only as a separate, explicit Product-approved UI copy decision.

Key domains include workspaces/auth, clinic configuration, CRM, appointments, payments/allocations, packages/usage, messaging/channels, automations, analytics, historical import/provenance, and integration sync state.

Historical imports use stable patient identifiers/normalized phone rules, retain provenance, support explicit append/replace semantics, and never merge patients by name alone.
