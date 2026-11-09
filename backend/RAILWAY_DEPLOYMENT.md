# Tia FastAPI on Railway

The backend is a long-running FastAPI service. Deploy the `backend/` directory as its own Railway service; do not deploy the API as a Vercel function.

## Service source

Use this repository and set:

- Root Directory: `/backend`
- Config as Code: `/backend/railway.json`
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

## Database migrations

Railway currently runs `python -m alembic upgrade head` in the `preDeployCommand` of both production application services, `tia-api` and `tia-whatsapp`. Either service can be deployed independently, so each keeps this command. No dedicated migration service is required.

Every online Alembic run acquires the same PostgreSQL **session-level advisory lock** in `alembic/env.py` before Alembic reads the current revision or applies migrations. The lock is shared by the API, WhatsApp, and online GitHub migration workflows because it is enforced by PostgreSQL, not by a service-specific or workflow-specific mutex.

The lock is held on the same PostgreSQL connection for the complete migration run. Session-level locking survives transaction commits and Alembic `autocommit_block()` boundaries, including the `CREATE INDEX CONCURRENTLY` operation in migration `0043_analytics_scale_guards`. SQLAlchemy's autobegun transaction is committed immediately after each try-lock statement so Alembic starts with an idle connection. The lock is explicitly released in a `finally` path; the `NullPool` connection is closed at the end of the run, which also releases the PostgreSQL session lock if the process or connection fails.

A process waits up to **300 seconds** for the shared lock, polling once per second. If the lock is not acquired within that bound, Alembic exits with a clear timeout error and the pre-deploy operation fails; the application deployment must not proceed on a failed migration command. The timeout is a code default and requires no Railway variable.

This database-level lock also serializes online Alembic commands started by GitHub Actions. GitHub Actions concurrency groups remain useful for ordering runs within a workflow, but they are not the cross-service protection mechanism.

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
