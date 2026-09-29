# Linka v0.17.0 — Production Readiness

This milestone adds a read-only operational gate around the systems already
implemented. It does not silently repair or mutate production data.

## Admin API

`GET /api/v1/operations/readiness`

Requires:

- a valid Supabase bearer token;
- `X-Workspace-ID`;
- an active `admin` workspace role.

The response is one of:

- `ready`
- `degraded`
- `not_ready`

## Fail conditions

A workspace is `not_ready` when a critical invariant is broken, including:

- database migration head is not `0013_ai_onboarding_sessions`;
- no active workspace admin exists;
- clinic configuration lacks branch/service/doctor/booking settings;
- automation jobs have stale processing locks beyond the normal reclaim window;
- message dispatches have stale processing locks;
- Gemini is not configured.

## Warning conditions

Warnings do not block the application but make the result `degraded`:

- no active external channel;
- no automation rule enabled;
- automation/dispatch failures in the last 24h;
- open human handoffs;
- expired flows still marked active;
- expired/failed AI onboarding sessions;
- no distinct onboarding provider fallback.

## Why stale locks are checked at 15 minutes

The channel and automation workers already reclaim processing work at about
10 minutes. The readiness gate intentionally waits longer, so it does not flag
normal worker recovery as a failure.

## CLI gate

From `backend/`:

```powershell
python scripts/run_production_readiness_gate.py --workspace-id <WORKSPACE_UUID>
```

The script is read-only and exits non-zero only for critical failures.

## Provider information

The endpoint exposes model names and failover configuration but never returns
provider API keys or Supabase secrets.

## Next hardening slice

After this gate is green, add deployment-level monitoring/alert transport
(Sentry/OpenTelemetry or the chosen observability stack) and subscription /
workspace entitlement enforcement as separate production concerns.
