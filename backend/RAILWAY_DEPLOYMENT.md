# Tia FastAPI on Railway

The backend is a long-running FastAPI service. Deploy the `backend/` directory as its own Railway service; do not deploy the API as a Vercel function.

## Service source

Use this repository and set:

- Root Directory: `/backend`
- Config as Code: `/backend/railway.json` (the file is not present in the current Main checkout; verify the live service configuration rather than assuming this path is active)
- Public Networking: enabled

`backend/Dockerfile` starts Uvicorn on Railway's injected `PORT`. The Railway healthcheck is `/api/v1/health/ready`, which verifies the API process and PostgreSQL connection before a deployment becomes active.

## Required runtime variables

Configure the backend with the production/staging values documented in the repository `.env.example`, especially:

- `DATABASE_URL`
- `MIGRATION_DATABASE_URL`
- `SUPABASE_URL`
- `SUPABASE_PUBLISHABLE_KEY`
- `SUPABASE_SECRET_KEY`
- `OPENAI_API_KEY`
- `CORS_ORIGINS`

Do not commit secret values.

## Database migrations — Release Governance

**Observed production configuration (read-only Railway inspection, 2026-11-10):**

Both Railway application services currently have the following live `preDeployCommand`:

```bash
python -m alembic upgrade head
```

- Project: `remarkable-kindness`
- Environment: `production`
- Service: `tia-api`
- Service: `tia-whatsapp`

These settings are controlled by the live Railway service configuration, not by a `railway.toml` or `railway.json` file in the current Main checkout. No live Railway settings were changed during this audit.

**Required owner action:** before claiming operational single-writer status, the Project Owner must separately approve and remove the `preDeployCommand` from each service's Railway production Deploy settings. Do not change the start command, build configuration, healthcheck, variables, networking, or other deploy settings. Until both live settings are re-read and confirmed empty, Railway remains a potential automatic production migration runner.

**Canonical runner proposal:** `.github/workflows/database-migrate.yml` is the intended GitHub Actions migration entry point, subject to approval and merge of PR #313 and the migration/release prerequisites documented in that PR. PR #313 is currently open and draft; it is not yet the approved operational path. Do not retire the legacy workflow or declare Single Writer PASS before the canonical runner is approved and active.

**Legacy workflow:** `.github/workflows/apply-0068-production.yml` is a separate production migration workflow triggered by a push to `main` when that workflow file's path changes. It runs `python -m alembic upgrade head`. Its retirement is proposed separately and must only be merged after the canonical GitHub runner is approved and available. The required merge order and dependencies must be reviewed by the Project Owner.

## Migration serialization

Every online Alembic run acquires the same PostgreSQL **session-level advisory lock** in `alembic/env.py` before Alembic reads the current revision or applies migrations.

The lock is held on the same PostgreSQL connection for the complete migration run. Session-level locking survives transaction commits and Alembic `autocommit_block()` boundaries, including the `CREATE INDEX CONCURRENTLY` operation in migration `0043_analytics_scale_guards`.

A process waits up to **300 seconds** for the shared lock, polling once per second. If the lock is not acquired within that bound, Alembic exits with a clear timeout error. The lock serializes online Alembic commands but does not replace the release-governance requirement to have only one approved production migration runner.

## Wire the public API to Tia runtimes

After Railway assigns the backend HTTPS origin, use the origin with no trailing slash in both places:

```text
Frontend server: TIA_API_URL=https://YOUR_TIA_API_DOMAIN
n8n runtime:     TIA_API_BASE_URL=https://YOUR_TIA_API_DOMAIN
```

The n8n runtime also needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` on the dedicated Tia n8n instance so the committed workflow expressions can read `TIA_API_BASE_URL`.

## Activation check

Before enabling patient-facing automations, verify in order:

1. `GET /api/v1/health/live` returns 200.
2. `GET /api/v1/health/ready` returns 200.
3. The n8n automation worker heartbeat becomes fresh in Tia.
4. The WhatsApp outbox worker is active with the real Meta credential.
5. Run the first end-to-end reminder only against a controlled test phone.

A stale automation worker means Tia may plan durable jobs but no external scheduler is currently executing them.
