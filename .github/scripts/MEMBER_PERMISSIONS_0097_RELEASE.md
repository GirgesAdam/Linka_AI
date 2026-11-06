# Member Permissions 0097 — Controlled Release Guard

**Preparation only.** PR #307 remains Draft. This document does not authorize a merge, deployment, Production setting change, Railway configuration change, or migration 0097.

## 1. Immutable scope and repository baseline

- Repository: **GirgesAdam/Linka_AI**
- PR #306 approved permissions source: **594c920ca3655851e5c9c54f7d6fec3dc7e2d32a**
- Migration: **0096_patient_customer_id -> 0097_member_page_permissions**
- Release workflow: **.github/workflows/member-permissions-0097-release.yml**
- Independent PostgreSQL integration workflow: **.github/workflows/member-permissions-0097-release-ci.yml**

The release code checks out the exact approved #306 commit detached and clean. The production release job is gated by a no-credentials preflight. PR #306 is not modified by this PR.

## 2. Read-only Migration Writer Inventory — 2026-11-06

### GitHub Actions

All 21 workflow YAML files under .github/workflows/ on main were inspected for Alembic/schema migration commands and database connection usage.

**Production schema writers found:**

| Workflow | Trigger | Direct migration command | Coordination |
|---|---|---|---|
| apply-0068-production.yml | Push to main when its own workflow file changes | python -m alembic upgrade head | No concurrency group |
| database-migrate.yml | Manual dispatch, staging or production | alembic upgrade head | database-migration-<environment> |

Both workflows consume GitHub Environment secret MIGRATION_DATABASE_URL. Their files were inspected read-only and were **not changed**.

**Isolated/test database migration commands:**

- ci.yml migrates a local PostgreSQL service container.
- grouped-multiservice-intent-live-review.yml runs clean migrations on its local PostgreSQL test database; its separate live database URL is not used by that local migration command.

Other workflows found to consume MIGRATION_DATABASE_URL for live review or evaluation include:

- agent-eval-batch-01.yml
- agent-eval-batch-02.yml
- automation-staging-rollback-gate.yml
- daily-clinic-conversation-review.yml
- extended-booking-conversation-review.yml
- fix5-no-show-package-live-verification.yml
- fix6-package-pricing-live-verification.yml
- human-conversation-quality-batch1-live-review.yml
- live-compound-semantic-smoke.yml
- v2-compound-occupied-review.yml
- v2-conversation-suite.yml
- v2-grouped-visit-live-review.yml
- v2-only-live-matrix.yml
- v2-read-semantics-review.yml
- v2-realistic-benchmark.yml
- v2-stateful-conversation-suite.yml

A connection to that secret alone is not evidence of a schema migration writer; no direct Alembic upgrade invocation was found in those workflow bodies.

### Railway

The authenticated Railway account exposed two projects in one personal workspace:

| Project | Environment | Services |
|---|---|---:|
| remarkable-kindness | production | 5 |
| surprising-intuition | production | 0 |

remarkable-kindness is strongly linked to Linka by the read-only evidence:

- tia-api source is GirgesAdam/Linka_AI, branch main, root directory backend.
- Its Railway domain is tia-api-production-54c5.up.railway.app.
- Its latest successful deployment commit is 1ee1545658d8794002b8b7f805e59c316b38ed36, exactly the inspected main SHA.
- tia-whatsapp-staging is also sourced from the same repository and main branch, with root directory backend.
- Both services expose the variable names DATABASE_URL and MIGRATION_DATABASE_URL. Values were not read, so their actual database targets are not proven equal.

**Railway migration writers:**

| Service | Railway environment | Pre-deploy command |
|---|---|---|
| tia-api | production | python -m alembic upgrade head |
| tia-whatsapp-staging | production | python -m alembic upgrade head |

Important: tia-whatsapp-staging is a service name; Railway reports its containing environment as production. The inventory did not show a separate staging environment. Do not infer a separate database from the service name.

Other services in this project:

- tia-agent-eval: repository-backed evaluation service, no Alembic pre-deploy command.
- tia-automation-waker: image-backed worker, no Alembic pre-deploy command.
- tia-whatsapp-transport-waker: image-backed worker, no Alembic pre-deploy command.

No Railway project settings, variables, sources, commands, deployments, or staged changes were modified. Variable values were not read or printed.

**Identity boundary:** the project-to-Linka association is strongly established by repository, production service, domain, and matching deployed commit. The actual database endpoint/project-ref behind Railway's secret values was not read, so equality with the GitHub Production MIGRATION_DATABASE_URL and Supabase project ycuxjlkhnubztgqmhtom remains unverified.

### Separate Oracle n8n database (not the Linka/Supabase target)

The repository also contains deploy/oracle-n8n/upgrade-postgres-17.sh. This is a manual host-side PostgreSQL 16-to-17 database-engine migration for the private n8n database only: it dumps the n8n database, stops n8n, removes the old n8n PostgreSQL data volume, starts PostgreSQL 17, and restores the dump. deploy/oracle-n8n/README.md explicitly states that this private PostgreSQL database is used only by n8n and that Tia FastAPI stays on Railway. It is therefore a separate database writer, not evidence of a writer to the Linka/Supabase PostgreSQL target. The script was inspected read-only and not executed.

Other Oracle scripts (import-production-workflows.sh, wire-tia-runtime.sh, publish-scheduler.sh, and healthcheck-production.sh) perform n8n workflow-state reads/writes or stack recovery through the n8n database. They are data/operations writers for the separate n8n database, not Alembic schema migration runners.

## 3. GitHub protection evidence

Read-only repository evidence at the inventory snapshot:

- main branch response: protected=false and protection.enabled=false.
- Repository Rulesets endpoint returned an empty list.
- Detailed Branch Protection endpoint returned HTTP 403 Resource not accessible by integration.
- Production Environment endpoint was not exposed by the connected GitHub read integration.

The Team Lead's latest audit comment (#6682745441) reports that Production currently has NO Required Reviewers. The connected GitHub read integration cannot independently re-fetch the Environment object, so that setting remains reported by the Team Lead but not independently re-verified through this connection.

The release guard queries the Production Environment at runtime and fails closed if the endpoint is unavailable, if Required Reviewers are missing, or if self-review prevention is not enabled. It also requires a custom Production deployment branch policy containing exactly one branch rule named main. The currently observed unprotected main therefore blocks the release preflight. The runtime guard fails closed if the branch-policy endpoint is unreadable or its policy is broader than main.

## 4. Independent engineering changes in this PR

### Transaction and database-session boundary

- Exactly one SQLAlchemy transaction is allowed during the migration.
- The same SQLAlchemy Connection object, PostgreSQL backend PID, and transaction ID are bound and checked before every approved write.
- A second transaction/connection, changed PID, or changed transaction ID fails closed.
- Exact write-set verification runs in SQLAlchemy's commit event before PostgreSQL can commit; post-command verification is an additional check.
- SET LOCAL lock_timeout = 5s, statement_timeout = 60s, and search_path = public are set and verified in the migration transaction.
- The session must report database postgres, schema public, and search path exactly public.
- Production URLs require Supabase project/username/port allowlisting and sslmode=verify-full. The connected PostgreSQL session must report TLS 1.2 or TLS 1.3.
- Only the two approved workspace_members column additions and one exact Alembic version update are accepted. Other non-read statements are rejected; SELECT statements containing known side-effect functions or write keywords are denied.

### GitHub control and freeze boundary

- Workflow-run and PR-comment history uses page-by-page retrieval; truncated history fails closed.
- Both known GitHub production migration workflows are checked for active runs.
- Freeze comment must be authored by the owner, match the exact source/workflow SHAs, be within its 30-minute UTC window, and have identical created_at and updated_at values.
- The complete PR #306 comment history is checked for a unique freeze ID and an unchanged stored copy.
- Production Environment must be named production, contain at least one actual Required Reviewer identity, prevent self-review, and have exactly one custom deployment branch policy: branch main.
- main must report protected=true and protection.enabled=true before a manual release can pass preflight.

### Deliberate hard blocks: Railway target and GitHub/Railway Single Writer

RAILWAY_DATABASE_TARGET_ATTESTED = False and RAILWAY_SINGLE_WRITER_PROOF_VERIFIED = False remain deliberate code-level release stops.

The actual Railway DATABASE_URL / MIGRATION_DATABASE_URL values were not read and cannot be compared to the GitHub Production secret without exposing secrets; target equality remains unverified. The code therefore raises PRODUCTION_RAILWAY_TARGET_UNVERIFIED.

An owner-authored freeze comment is an attestation, not a technical control that stops Railway Pre-deploy. GitHub concurrency does not coordinate with Railway, and the inventory shows two Railway services that can run Alembic. The code therefore raises PRODUCTION_SINGLE_WRITER_UNVERIFIED before the production job can receive credentials.

Do not change either hard block to True until Team Lead/Project Owner approves and independently verifiable target comparison and GitHub/Railway coordination controls are implemented and tested.

## 5. Actions and dependency pins

- actions/checkout is pinned to verified commit 1af3b93b6815bc44a9784bd300feb67ff0d1eeb3 (v6.0.0, Node 24).
- actions/setup-python is pinned to verified commit 83679a892e2d95755f2dac6acb0bfd1e9ac5d548 (v6.1.0, Node 24).
- Python is pinned to 3.12.9 in both workflows.
- pip is pinned to 25.3.
- Release dependencies and their transitive runtime dependencies are listed at exact versions in .github/scripts/requirements-0097-release.txt and installed with --no-deps followed by pip check.
- The PostgreSQL integration service uses the exact postgres:17.11-bookworm image tag.

**Supply-chain limitation:** package versions are exact-pinned, but wheel SHA-256 hashes and the PostgreSQL image digest are not yet locked. Do not claim artifact-hash immutability from version pins alone. This remains a hardening item for Team Lead review.

## 6. Independent PostgreSQL 17 Integration CI

.github/workflows/member-permissions-0097-release-ci.yml runs on pull requests and uses an isolated PostgreSQL 17.11 service container. It has no Production secrets.

The job:

1. Checks out the PR release code and exact approved #306 source separately.
2. Installs the exact version-pinned minimal Alembic runtime.
3. Creates only local Supabase compatibility roles in the isolated CI database.
4. Runs the approved Alembic chain to 0096_patient_customer_id, not Production.
5. Runs deterministic negative regressions and the real original-Alembic 0096 -> 0097 integration against that isolated database.

The integration asserts one transaction, one connection/PID/XID, exact write-set verification before commit, the exact two DDL statements, one version update, schema/defaults, rejection of an extra write, rejection of a second transaction, and rejection of a repeat migration.

## 7. Freeze example — NOT an active attestation

A future release freeze requires a new owner-authored JSON comment on PR #306 with the exact reviewed workflow SHA, exact approved permissions SHA, unique freeze ID, UTC window, and coordination fields. A previous comment must never be reused.

Even a valid freeze does not override either hard Railway gate.

## 8. Verification commands

Deterministic suite:

    python -m unittest discover -s .github/scripts/tests -p "test_*.py" -v

Isolated PostgreSQL integration (CI supplies PostgreSQL 17, roles, and a clean 0096 database):

    LINKA_ISOLATED_0097_TEST=1
    LINKA_ISOLATED_DATABASE_URL=postgresql+psycopg://ci_user:ci_password@127.0.0.1:5432/postgres
    LINKA_APPROVED_SOURCE=/path/to/detached-approved-594c-checkout
    python -m unittest discover -s .github/scripts/tests -p "test_*.py" -v

## 9. Production safety and failure policy

- PR #306 remains untouched.
- No changes to .github/workflows/apply-0068-production.yml or .github/workflows/database-migrate.yml.
- No Railway configuration or service changes.
- No GitHub Environment, branch-protection, ruleset, or secret changes.
- No Merge, Deployment, Migration Dispatch, or Migration 0097.
- The dedicated release workflow remains blocked until Environment policy, main branch protection, Railway target identity, and GitHub/Railway Single Writer guarantees are independently verified.

On any failed preflight or DDL guard, stop. Inspect actual revision, schema, locks, and run evidence. Never automatically retry or downgrade. A successful schema migration would not authorize merging PR #306 or deploying the Backend.
