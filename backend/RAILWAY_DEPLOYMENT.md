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

## Database migrations - Current Release Governance (2026-11-10)

### Canonical GitHub Actions runner

PR #313 was merged successfully into main.

- PR #313: **MERGED**
- Merge commit / Main baseline: c780d1715a139b8fa39232ec41c494ede5dced1d
- Canonical workflow: .github/workflows/database-migrate.yml
- The canonical workflow is present on Main and is the approved GitHub Actions migration entry point.

The canonical workflow retains its release controls: manual workflow_dispatch, Production restricted to refs/heads/main, exact 40-character Main SHA verification, checkout pinned to github.sha with persist-credentials: false, Migration 0097 SHA-256 verification, expected pre-migration revision validation, rejection of zero or multiple current Alembic heads, final exact revision attestation, explicit Production target alembic upgrade 0097_member_page_permissions, shared PostgreSQL session advisory locking, secret isolation, least-privilege contents: read, cancel-in-progress: false, and the existing Staging alembic upgrade head behavior.

The previous description of PR #313 as open/draft and the canonical workflow as a proposal is historical and superseded by the merge above. The merge does not itself authorize a Production migration; the Single-Owner technical Production approval gate remains a separate future task and is not implemented by PR #313.

### Live Railway Production migration configuration - read-only observation

**Last verified by read-only Railway configuration inspection on 2026-11-10:**

- Project: remarkable-kindness
- Environment: production

Both application services still have this live preDeployCommand:

    python -m alembic upgrade head

| Railway service | Live preDeployCommand |
|---|---|
| tia-api | python -m alembic upgrade head |
| tia-whatsapp | python -m alembic upgrade head |

These values were read from the live Railway service configuration. No Railway configuration, secrets, environment variables, deployments, or service settings were changed as part of this PR.

**Required separate Project Owner action:** removing the two Railway preDeployCommand values is outside PR #314. It requires a separate explicit owner decision and a separate controlled task. Until both live commands have been removed and re-read as empty, Railway remains an additional automatic Production migration execution path. Do not claim operational Single Writer.

### Historical Production Migration 0097 execution evidence

A historical Railway deployment of tia-whatsapp ran Migration 0097 after PR #306 was merged:

- Service: tia-whatsapp
- Deployment: b25ab80c-a67e-4acb-85ff-16e7e904cbc7
- Deployment created: 2026-11-10T17:41:06Z
- Historical deployment state at the later read: REMOVED

The retained Railway deploy log records:

    Running upgrade 0096_patient_customer_id -> 0097_member_page_permissions

The same deployment log subsequently records:

    Application startup complete.
    GET /api/v1/health/ready
    HTTP 200

This is evidence that the migration command ran and that the application passed its readiness check in that deployment. It is **not** an independent database revision attestation and must not be described as one. Migration 0097 must not be re-run or rolled back as part of this PR.

### Correct Single Writer status

    CANONICAL GITHUB ACTIONS RUNNER:
    MERGED - PR #313

    LEGACY GITHUB MIGRATION WORKFLOW:
    PENDING RETIREMENT - THIS PR #314

    RAILWAY tia-api preDeployCommand:
    ACTIVE

    RAILWAY tia-whatsapp preDeployCommand:
    ACTIVE

    OPERATIONAL SINGLE WRITER:
    NOT ACHIEVED

PR #314 retires only the legacy GitHub Actions workflow .github/workflows/apply-0068-production.yml. It does not remove either Railway preDeployCommand. Therefore, even after PR #314 is merged, the Railway commands will remain additional migration paths until the Project Owner separately authorizes and completes their removal.

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
