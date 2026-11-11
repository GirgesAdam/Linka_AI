# Tia FastAPI on Railway

## Current Verified Railway and Migration Configuration — 2026-11-11

> **Authoritative current snapshot.** This section records live Railway configuration and the canonical GitHub workflow verified on 2026-11-11 against Main commit `81224bdb0d6233c0709447d2c87f2582fc774754`. It supersedes conflicting operational claims in the historical sections below. No migration was run as part of this verification.

### 1. Canonical external migration workflow

- The only approved external Staging/Production migration runner is `.github/workflows/database-migrate.yml`.
- The workflow is manually invoked through `workflow_dispatch` and selects either `staging` or `production`.
- The Production job declares the GitHub Environment `production`; it is restricted to `refs/heads/main` and first workflow attempt (`run_attempt == 1`). The preflight fails closed unless the supplied expected Main SHA equals `github.sha`, the checked-out `HEAD` equals that same SHA, and the SHA is a valid exact commit.
- Production preflight verifies the raw Git-blob SHA-256 of `backend/alembic/versions/0097_member_page_permissions.py` at that exact commit. It also requires the expected previous revision `0096_patient_customer_id`.
- Before Production migration, the workflow attests exactly one current Alembic head and verifies it equals the supplied expected previous revision. After migration, it attests exactly one final head and verifies `0097_member_page_permissions`.
- The Production job uses the explicit target `alembic upgrade 0097_member_page_permissions`. The Staging job uses `alembic upgrade head`.
- Staging and Production share workflow-level global serialization: `group: database-migration-global`, `queue: max`, and `cancel-in-progress: false`. A Production approval wait therefore remains inside the same global queue and blocks a concurrent Staging migration.
- PostgreSQL advisory locking is an additional database-level safeguard. `backend/alembic/env.py` wraps the complete online Alembic migration in `migration_session_lock`; `backend/app/database/migration_lock.py` implements a PostgreSQL session-level advisory lock with a 300-second wait timeout.
- No legacy GitHub migration workflow remains in `.github/workflows/`; the former `apply-0068-production.yml` is absent. `database-migrate.yml` is the canonical external workflow.
- GitHub CI still runs migrations against its own disposable PostgreSQL service container for tests. That CI-local database activity is not an external Staging or Production migration runner.

### 2. Railway migration-runner retirement — live Production read-back

> Verified directly from the five live Railway service configurations in Project `remarkable-kindness`, Environment `production`, on 2026-11-11. No variable values or secrets were read.

| Railway service | Live pre-deploy migration configuration |
|---|---|
| `tia-api` | Empty (`[]`) |
| `tia-whatsapp` | Empty (`[]`) |
| `tia-agent-eval` | Empty (`[]`) |
| `tia-automation-waker` | Not configured |
| `tia-whatsapp-transport-waker` | Not configured |

No Railway service has an active Alembic pre-deploy command. Railway no longer runs Alembic automatically as part of these five services. The canonical GitHub Actions workflow above is the only approved external Staging/Production migration path; its existence does not authorize a dispatch.

### 3. Railway watch configuration — live read-back

> Both target services were read before and after the narrow Watch Patterns update. Only the `watchPatterns` field was written.

| Setting | `tia-api` | `tia-whatsapp` |
|---|---|---|
| Root Directory | `/backend` (API value: `backend`) | `/backend` (API value: `backend`) |
| Check Suites | ON | OFF |

Both services have exactly these seven Watch Patterns:

```text
/backend/**
!/backend/tests/**
!/backend/app/services/agent_v2/test_harness.py
!/backend/scripts/test_ai_onboarding_gemini_schema.py
!/backend/scripts/test_gemini_provider.py
!/backend/scripts/test_n8n_runtime_contract.py
!/backend/RAILWAY_DEPLOYMENT.md
```

The exclusions cover `backend/tests/**`, the four approved Test-only files, and this deployment document. These are Railway deployment-trigger exclusions only: they do **not** remove the files from Git, disable GitHub Actions, or prevent GitHub CI from testing them.

### 4. Migration authorization boundary

```text
Production Migration: NOT AUTHORIZED
Staging Migration:    NOT AUTHORIZED
```

- The previous Project Owner review recorded the Production Environment approval gate as enabled with `GirgesAdam` as the sole reviewer, Administrator Bypass disabled, Prevent Self-review disabled, and the deployment branch restricted to `main`. Those Environment protection settings were not re-read during this documentation task; this is a prior verification record, not a new attestation.
- The Project Owner deferred Production migration credential verification and Staging credential target verification. The presence or correctness of `PRODUCTION_MIGRATION_DATABASE_URL` and the Staging credential target must not be assumed from workflow references or Railway runtime variable names. No secret values were accessed or displayed.
- The Production workflow credential guard fails closed if `PRODUCTION_MIGRATION_DATABASE_URL` is unavailable to the protected Production job. This documentation does not claim that the credential is configured or that Production is ready to migrate.
- The `Required runtime variables` list above describes application runtime variable names; it is not evidence that GitHub Environment migration credentials are configured or that either migration is authorized.
- Any future Staging or Production migration requires a separate explicit Project Owner authorization and the applicable credential/target verification. Do not dispatch the workflow based on this documentation update.

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

The variable names above describe application runtime configuration only. They do not attest that GitHub Environment migration credentials exist, have the correct target, or are authorized for use. See the current Migration authorization boundary above.

## Historical Migration Release Governance Snapshot — 2026-11-10

### Historical canonical GitHub Actions runner — PR #313 snapshot

> Historical record: this subsection describes the state after PR #313 and before the later PR #315 hardening. The authoritative current workflow and controls are documented in the Current Verified section above.

PR #313 was merged successfully into main.

- PR #313: **MERGED**
- Merge commit / Main baseline: c780d1715a139b8fa39232ec41c494ede5dced1d
- Canonical workflow: .github/workflows/database-migrate.yml
- The canonical workflow is present on Main and is the approved GitHub Actions migration entry point.

The canonical workflow retains its release controls: manual workflow_dispatch, Production restricted to refs/heads/main, exact 40-character Main SHA verification, checkout pinned to github.sha with persist-credentials: false, Migration 0097 SHA-256 verification, expected pre-migration revision validation, rejection of zero or multiple current Alembic heads, final exact revision attestation, explicit Production target alembic upgrade 0097_member_page_permissions, shared PostgreSQL session advisory locking, secret isolation, least-privilege contents: read, cancel-in-progress: false, and the existing Staging alembic upgrade head behavior.

The previous description of PR #313 as open/draft and the canonical workflow as a proposal is historical and superseded by the merge above. The merge does not itself authorize a Production migration; the Single-Owner technical Production approval gate remains a separate future task and is not implemented by PR #313.

### Historical Railway Production migration configuration — superseded by PR #314

> Historical record: the two active Railway pre-deploy commands described below were subsequently retired. The live 2026-11-11 read-back above confirms empty/not-configured pre-deploy migration settings across all five Railway services.

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

### Historical Single Writer status — superseded by the current verified state

> Historical record: the status below predates PR #314 retirement and PR #315 governance hardening. It must not be read as the current Railway or GitHub migration-runner state.

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

## Migration serialization — PostgreSQL advisory lock

The GitHub Actions workflow adds a separate global serialization layer across Staging and Production using `database-migration-global`, `queue: max`, and `cancel-in-progress: false`. The PostgreSQL session-level advisory lock described below remains an additional database-level safeguard.

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
