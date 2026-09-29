# Linka v0.16.0 — AI-Assisted Onboarding

## Goal

An Admin can describe clinic configuration naturally, for example:

> عندي فرعين في مدينة نصر والتجمع، بنشتغل من 10 لـ10، عندي دكتور أحمد
> ودكتورة سارة، وضيف خدمة ليزر بـ1500 ومدتها ساعة.

Linka converts the request into a typed configuration plan, validates it, asks
for explicit confirmation and then performs the real database writes.

## Flow

Admin message
→ Gemini 3.7 Flash onboarding planner
→ semantic configuration capabilities
→ typed `OnboardingPlan`
→ deterministic cross-reference/business validation
→ Admin sees plan summary
→ explicit semantic confirmation or Confirm button
→ atomic PostgreSQL execution
→ audit event
→ setup readiness refresh

## Capabilities

- `branch_configuration`
- `service_configuration`
- `doctor_configuration`
- `schedule_configuration`
- `booking_settings_configuration`

The model never returns internal tool names or database IDs.

## Write safety

The LLM cannot execute writes.

`execute_plan()` requires:

1. Admin workspace role.
2. persisted session owned by the same Admin/workspace.
3. `awaiting_confirmation` state.
4. matching optimistic session version.
5. a locally validated Pydantic plan.
6. valid branch/service/doctor cross-references.

Execution is one SQLAlchemy transaction. A database conflict rolls back the
configuration write rather than leaving a half-applied clinic setup.

## Idempotent upsert identities

- branch: workspace + branch code
- service: workspace + service slug
- doctor staff: email when supplied, otherwise exact normalized name
- doctor profile: workspace + staff
- doctor-branch/service assignments: existing composite relationship

This means re-confirming a completed session cannot duplicate writes, and
revising a plan can update existing setup objects.

## Destructive actions

AI onboarding v0.16.0 intentionally does not delete branches/services/doctors.
Destructive configuration changes should be a later capability with a separate
risk/confirmation policy.

## Persistent state

`onboarding_ai_sessions` stores:

- plan
- plan summary
- missing information
- last structured decision
- execution result
- version
- TTL/status

`onboarding_ai_events` records:

- messages
- plan proposals/revisions
- confirmation
- completed write
- cancellation/failure/expiry

Both tables use RLS plus revoked direct `anon`/`authenticated` access. Business
access remains through FastAPI.
