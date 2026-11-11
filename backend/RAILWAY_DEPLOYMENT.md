# Tia FastAPI on Railway

## Main baseline and previously recorded Railway configuration — 2026-11-11

> **Baseline accuracy.** The committed Main state recorded here is 548cce86276601b8cf1a5fb765c6a6270cf0b2c1. This section distinguishes that current Main baseline from the proposed changes in the open Draft PR described below. This task did not access a remote database, execute a migration, or change Railway or Supabase configuration. Railway configuration observations are carried forward from the prior read-only read-back recorded on 2026-11-11; they were not re-read during this PR.

### 1. Canonical GitHub Actions migration workflow — current Main baseline

- The canonical external migration workflow on Main is .github/workflows/database-migrate.yml.
- On this Main baseline, the workflow is manually invoked through workflow_dispatch and exposes a choice between staging and production.
- The migrate-staging job uses the GitHub staging Environment and secrets.MIGRATION_DATABASE_URL; it runs alembic current, alembic upgrade head, and alembic current using that credential. The credential reference and Environment scope do not verify the secret value or its actual database endpoint; that endpoint was not verified.
- The migrate-production job uses the GitHub production Environment; it is restricted to refs/heads/main and the first workflow attempt (run_attempt == 1).
- Production preflight fails closed unless the supplied expected Main SHA equals github.sha, the checked-out HEAD equals that same SHA, and the SHA is a valid exact commit.
- Production preflight verifies the raw Git-blob SHA-256 of backend/alembic/versions/0097_member_page_permissions.py at that exact commit. It also requires the expected previous revision 0096_patient_customer_id.
- Before Production migration, the workflow attests exactly one current Alembic head and verifies it equals the supplied expected previous revision. After migration, it attests exactly one final head and verifies 0097_member_page_permissions.
- The Production job uses the explicit target alembic upgrade 0097_member_page_permissions; it does not use alembic upgrade head.
- Staging and Production share workflow-level global serialization: group: database-migration-global, queue: max, and cancel-in-progress: false.
- PostgreSQL advisory locking is an additional database-level safeguard. backend/alembic/env.py wraps the complete online Alembic migration in migration_session_lock; backend/app/database/migration_lock.py implements a PostgreSQL session-level advisory lock with a 300-second wait timeout.
- The former apply-0068-production.yml legacy workflow is absent. database-migrate.yml is the only canonical external migration workflow on this baseline.
- GitHub CI and selected review workflows also run migrations against disposable local PostgreSQL service containers. These local test migrations are not external Staging or Production migration runners.

### 2. Proposed migration governance in this Draft PR — not yet on Main

The proposed change in this PR removes only the external Staging migration path:

- Remove the migrate-staging job from .github/workflows/database-migrate.yml.
- Remove the workflow_dispatch Environment selector. The remaining required inputs are the approved Main SHA, Migration 0097 SHA-256, and expected current revision.
- Make the preflight Production-only and reject staging or any other non-Production target.
- Retain one external migration job: migrate-production.
- Preserve the Production GitHub Environment, Project Owner approval boundary, Main restriction, first-attempt restriction, Production credential guard and PRODUCTION_MIGRATION_DATABASE_URL isolation, exact Main SHA and migration hash checks, expected previous revision, pre/post revision attestations, advisory lock, exact Migration 0097 target, audit evidence, and global concurrency (cancel-in-progress: false).
- Preserve all other Staging Environment-scoped uses for Agent evaluation, conversation and booking reviews, semantic checks, and database-backed functional tests. The workflow audit identified 17 Jobs across 16 workflows that bind secrets.MIGRATION_DATABASE_URL (including the V2 Full Evidence Audit alias). Fourteen invoke database-backed review code; three fixture-only harness Jobs (human-conversation-quality, v2-conversation-suite, and v2-stateful-conversation-suite) do not open a database connection even though the credential is currently bound. None of these Jobs is an Alembic migration runner, and none is changed by this PR. The actual endpoint configured in the secret is not verified.
- Preserve automation-staging-rollback-gate.yml, including its database-backed write/rollback tests and its explicit HTTP blocking.
- Preserve the V2 Full Evidence Audit split: disposable local PostgreSQL for Alembic setup, and real-Agent evidence through the Staging-scoped credential alias with per-scenario transaction rollback; the actual endpoint behind that credential is not verified.
- Preserve the grouped multi-service review jobs that use disposable local PostgreSQL and run alembic upgrade head; their staging Environment label does not make the local database an external Staging migration target.
- Preserve the GitHub CI disposable PostgreSQL migration tests and all migration revision/security tests.
- Do not delete, rename, move, or change values for MIGRATION_DATABASE_URL or PRODUCTION_MIGRATION_DATABASE_URL. The former remains referenced by other Staging-environment review Jobs; its actual endpoint is unverified. The latter remains scoped to the protected Production job; its actual endpoint is also unverified.

#### Credential binding and target verification boundary

| Environment | Credential | Verified fact | Not verified |
|---|---|---|---|
| Staging | MIGRATION_DATABASE_URL | Workflows reference secrets.MIGRATION_DATABASE_URL inside Jobs declaring environment: staging; intended for Staging-scoped evaluation and review workflows. | Secret value, resolved endpoint/host, database name or identity, and whether it actually points to a Staging database. |
| Production | PRODUCTION_MIGRATION_DATABASE_URL | migrate-production references the credential inside environment: production, behind the existing protected Environment approval boundary. | Secret value, resolved endpoint/host, database name or identity, and whether it actually points to the intended Production database. |

The workflow audit verifies credential references and GitHub Environment scope only. It does not read secret values or verify the database endpoint selected by either credential. No secret values were read or displayed during this task.

**Important:** These are proposed PR semantics, not the current Main state. Until the Draft PR is reviewed and merged, Main still contains the Staging migration job described in section 1.

### 3. Railway migration-runner retirement — previously recorded live Production read-back

> Prior read-only read-back recorded on 2026-11-11 for Project remarkable-kindness, Environment production. These values were not re-read or changed during this PR. No variable values or secrets were read.

| Railway service | Previously recorded pre-deploy migration configuration |
|---|---|
| tia-api | Empty ([]) |
| tia-whatsapp | Empty ([]) |
| tia-agent-eval | Empty ([]) |
| tia-automation-waker | Not configured |
| tia-whatsapp-transport-waker | Not configured |

The prior read-back recorded no active Alembic pre-deploy command on these five services. This PR does not modify or newly attest Railway configuration. The proposed Production-only GitHub workflow change is a repository governance change; it does not itself change Railway service settings.

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
- Any future Production migration requires separate explicit Project Owner authorization and applicable credential/target verification. Do not dispatch the workflow based on this documentation or the Draft PR.

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
