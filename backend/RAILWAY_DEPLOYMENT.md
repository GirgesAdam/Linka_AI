# Tia FastAPI on Railway

## Main baseline and previously recorded Railway configuration — 2026-11-11

> **Baseline accuracy.** The committed Main state recorded here is `101a0ce8f40d4628e6b695f07ef9129d88684e54`, after PR #317 retired the external Staging migration path. The workflow description below reflects that merged Main state, not an open Draft PR. This task did not access a remote database, execute a migration, or change Railway or Supabase configuration. Railway configuration observations are carried forward from the prior read-only read-back recorded on 2026-11-11; they were not re-read during this task.

### 1. Canonical GitHub Actions migration workflow — current Main baseline

- The canonical external migration workflow on Main is .github/workflows/database-migrate.yml.
- After PR #317, the workflow is manually invoked through workflow_dispatch with required approved Main SHA, Migration 0097 SHA-256, and expected current revision inputs. It has no Environment selector.
- The external workflow has no migrate-staging job. Its only external migration job is migrate-production, which uses the protected Production Environment and the dedicated PRODUCTION_MIGRATION_DATABASE_URL credential. This does not establish the live secret value or endpoint.
- The migrate-production job uses the GitHub production Environment; it is restricted to refs/heads/main and the first workflow attempt (run_attempt == 1).
- Production preflight fails closed unless the supplied expected Main SHA equals github.sha, the checked-out HEAD equals that same SHA, and the SHA is a valid exact commit.
- Production preflight verifies the raw Git-blob SHA-256 of backend/alembic/versions/0097_member_page_permissions.py at that exact commit. It also requires the expected previous revision 0096_patient_customer_id.
- Before Production migration, the workflow attests exactly one current Alembic head and verifies it equals the supplied expected previous revision. After migration, it attests exactly one final head and verifies 0097_member_page_permissions.
- The Production job uses the explicit target alembic upgrade 0097_member_page_permissions; it does not use alembic upgrade head.
- The Production-only external migration workflow retains global serialization: group: database-migration-global, queue: max, and cancel-in-progress: false. Other Staging-scoped review and evaluation Jobs are not external Alembic migration runners.
- PostgreSQL advisory locking is an additional database-level safeguard. backend/alembic/env.py wraps the complete online Alembic migration in migration_session_lock; backend/app/database/migration_lock.py implements a PostgreSQL session-level advisory lock with a 300-second wait timeout.
- The former apply-0068-production.yml legacy workflow is absent. database-migrate.yml is the only canonical external migration workflow on this baseline.
- GitHub CI and selected review workflows also run migrations against disposable local PostgreSQL service containers. These local test migrations are not external Staging or Production migration runners.

### Production migration revision contract

- **Pre-migration state:** `0096_patient_customer_id`, enforced by `EXPECTED_PREVIOUS_REVISION` in `.github/scripts/production_migration_preflight.py` and checked against the single current Production revision before migration.
- **Post-migration expected final state:** `0097_member_page_permissions`, used as the explicit migration target and final revision attestation.
- **Read-only Production attestation:** `.github/scripts/production_db_readonly_attestation.py` expects only the post-migration final revision `0097_member_page_permissions`. It does not treat `0096_patient_customer_id` as an acceptable attested final state.
- This is the repository release contract, not evidence of the live Production revision. Production migration status remains `NOT VERIFIED` in this task; no Production database was accessed.

### 2. Merged retirement of the external Staging migration path — PR #317

PR #317 merged the following Production-only migration governance changes into Main:

- Removed the migrate-staging job from .github/workflows/database-migrate.yml.
- Removed the workflow_dispatch Environment selector. The remaining required inputs are the approved Main SHA, Migration 0097 SHA-256, and expected current revision.
- Made the preflight Production-only; it rejects staging or any other non-Production target.
- Retained one external migration job: migrate-production.
- Preserved the Production GitHub Environment, Project Owner approval boundary, Main restriction, first-attempt restriction, Production credential guard and PRODUCTION_MIGRATION_DATABASE_URL isolation, exact Main SHA and migration hash checks, expected previous revision, pre/post revision attestations, advisory lock, exact Migration 0097 target, audit evidence, and global concurrency (cancel-in-progress: false).
- Preserved all other Staging Environment-scoped uses for Agent evaluation, conversation and booking reviews, semantic checks, and database-backed functional tests. The workflow audit identified 17 Jobs across 16 workflows that bind secrets.MIGRATION_DATABASE_URL (including the V2 Full Evidence Audit alias). Fourteen invoke database-backed review code; three fixture-only harness Jobs (human-conversation-quality, v2-conversation-suite, and v2-stateful-conversation-suite) do not open a database connection even though the credential is currently bound. None of these Jobs is an Alembic migration runner, and none was changed by PR #317. The actual endpoint configured in the secret is not verified.
- Preserved automation-staging-rollback-gate.yml, including its database-backed write/rollback tests and its explicit HTTP blocking.
- Preserved the V2 Full Evidence Audit split: disposable local PostgreSQL for Alembic setup, and real-Agent evidence through the Staging-scoped credential alias with per-scenario transaction rollback; the actual endpoint behind that credential is not verified.
- Preserved the grouped multi-service review jobs that use disposable local PostgreSQL and run alembic upgrade head; their staging Environment label does not make the local database an external Staging migration target.
- Preserved the GitHub CI disposable PostgreSQL migration tests and all migration revision/security tests.
- Do not delete, rename, move, or change values for MIGRATION_DATABASE_URL or PRODUCTION_MIGRATION_DATABASE_URL. The former remains referenced by other Staging-environment review Jobs; its actual endpoint is unverified. The latter remains scoped to the protected Production job; its actual endpoint is also unverified.

#### Credential binding and target verification boundary

| Environment | Credential | Verified fact | Not verified |
|---|---|---|---|
| Staging | MIGRATION_DATABASE_URL | Workflows reference secrets.MIGRATION_DATABASE_URL inside Jobs declaring environment: staging; intended for Staging-scoped evaluation and review workflows. | Secret value, resolved endpoint/host, database name or identity, and whether it actually points to a Staging database. |
| Production | PRODUCTION_MIGRATION_DATABASE_URL | migrate-production references the credential inside environment: production, behind the existing protected Environment approval boundary. | Secret value, resolved endpoint/host, database name or identity, and whether it actually points to the intended Production database. |

The workflow audit verifies credential references and GitHub Environment scope only. It does not read secret values or verify the database endpoint selected by either credential. No secret values were read or displayed during this task.

**Important:** PR #317 is merged. The Production-only workflow described in section 1 is the current Main state; the old Draft-PR description of an active Staging migration job is superseded.

### 3. Railway migration-runner retirement — previously recorded live Production read-back

> Prior read-only read-back recorded on 2026-11-11 for Project remarkable-kindness, Environment production. These values were not re-read or changed during this PR. No variable values or secrets were read.

| Railway service | Previously recorded pre-deploy migration configuration |
|---|---|
| tia-api | Empty ([]) |
| tia-whatsapp | Empty ([]) |
| tia-agent-eval | Empty ([]) |
| tia-automation-waker | Not configured |
| tia-whatsapp-transport-waker | Not configured |

The prior read-back recorded no active Alembic pre-deploy command on these five services. This task does not modify or newly attest Railway configuration. The merged Production-only GitHub workflow is a repository governance change; it does not itself change Railway service settings.

### 4. Railway watch configuration — previously recorded read-back

> Prior read-only read-back recorded on 2026-11-11. Watch Patterns were not re-read or changed during this PR.

| Setting | tia-api | tia-whatsapp |
|---|---|---|
| Root Directory | /backend (API value: backend) | /backend (API value: backend) |
| Check Suites | ON | OFF |

The previously recorded Watch Patterns were:

    /backend/**
    !/backend/tests/**
    !/backend/app/services/agent_v2/test_harness.py
    !/backend/scripts/test_ai_onboarding_gemini_schema.py
    !/backend/scripts/test_gemini_provider.py
    !/backend/scripts/test_n8n_runtime_contract.py
    !/backend/RAILWAY_DEPLOYMENT.md

These are Railway deployment-trigger exclusions only: they do not remove the files from Git, disable GitHub Actions, or prevent GitHub CI from testing them.

### 5. Migration authorization boundary

    Production Migration: NOT AUTHORIZED
    Staging Migration:    NOT AUTHORIZED

- The previous Project Owner review recorded the Production Environment approval gate as enabled with GirgesAdam as the sole reviewer, Administrator Bypass disabled, Prevent Self-review disabled, and the deployment branch restricted to main. Those Environment protection settings were not re-read during this task; this is a prior verification record, not a new attestation.
- The Project Owner previously deferred Production migration credential verification and Staging credential target verification. The presence or correctness of PRODUCTION_MIGRATION_DATABASE_URL and the Staging credential target must not be assumed from workflow references or Railway runtime variable names. No secret values were accessed or displayed during this task.
- The Production workflow credential guard fails closed if PRODUCTION_MIGRATION_DATABASE_URL is unavailable to the protected Production job. This documentation does not claim that the credential is configured or that Production is ready to migrate.
- The Required runtime variables list below describes application runtime variable names; it is not evidence that GitHub Environment migration credentials exist, have the correct target, or are authorized for use.
- Any future Production migration requires separate explicit Project Owner authorization and applicable credential/target verification. Do not dispatch the workflow based on this documentation alone.

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

At the time PR #313 merged, the workflow retained manual workflow_dispatch, Main restriction, exact Main SHA verification, Migration 0097 SHA-256 verification, expected pre-migration revision validation, exact pre/post revision checks, explicit Production target `alembic upgrade 0097_member_page_permissions`, PostgreSQL session advisory locking, secret isolation, least-privilege `contents: read`, and non-cancelling global concurrency. The former external Staging `alembic upgrade head` job described in that historical PR #313 state was subsequently retired by PR #317; it is not present on the current Main baseline.

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
