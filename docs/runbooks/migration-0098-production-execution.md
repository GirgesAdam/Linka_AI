# Migration 0098 — Production execution runbook (proposal only)

**No Production/Staging access or execution is authorized by this document or by merging the workflow PR.**

Repository: GirgesAdam/Linka_AI
Migration: `0097_member_page_permissions` → `0098_doctor_laser_weekday_exclusions`
Approved migration Git-blob SHA-256: `0d1c63717653e5b7f56e91698279b8896636c014601a75f75cf0794a9f77d100`

## Scope, controls and design decision

- Preserve existing `.github/workflows/database-migrate.yml` unchanged for historical 0096→0097. Use separate manual-only `.github/workflows/database-migrate-0098.yml` for 0098.
- Source preflight has no DB credentials. Protected job uses GitHub environment `production` and environment secrets `PRODUCTION_MIGRATION_0098_DATABASE_URL` plus `PRODUCTION_MIGRATION_DB_CA_CERT`. An administrator must verify GitHub Environment **required reviewers** and branch restrictions; YAML `environment: production` alone does not enforce these settings.
- Pin exact dispatch SHA, approved SHA, current remote Main SHA and SHA-256 of Git blob. Reject any mismatch or rerun attempt.
- Pin approved Supabase session pooler `aws-1-eu-west-3.pooler.supabase.com:5432`, DB `postgres`, user `postgres.ycuxjlkhnubztgqmhtom`. Require `verify-full` TLS and a trusted Supabase CA certificate. If endpoint/CA differs, stop for new owner/DBA review, never weaken pinning.
- The runner invokes the existing approved 0098 `upgrade()` via Alembic `MigrationContext` / `Operations` **instead of the general Alembic CLI**. This is deliberate for atomic, under-lock revalidation and needs independent engineering review.
- Reuse existing session advisory lock, transaction and timeout controls. Read current Alembic version **under the same lock before upgrade**. Run pre-commit schema checks in one transaction; failure before commit rolls back DDL and version update.
- Never add automatic downgrade, database restore, retries, or opportunistic data/backfill changes.

TLS references: https://supabase.com/docs/guides/database/psql and https://supabase.com/docs/guides/platform/ssl-enforcement

## Approvals and backup prerequisites for a future operator

1. Independently review the workflow Draft PR, its exact-HEAD CI, governance, and secrets. Obtain separate Project Owner approval to **merge the workflow PR**.
2. Verify Production GitHub Environment reviewer protection, branch restrictions and non-exposed protected secrets. Do not pass credentials as workflow dispatch inputs.
3. After workflow merge, obtain fresh, separately authorized Production database attestation: identity, PostgreSQL version, roles, exactly 0097, target 0098 table absent, parent unique constraints. Historical evidence is not sufficient.
4. Obtain a fresh encrypted backup under an independently authorized process; verify encryption/hash, restorability, scope and timestamp. Supabase Storage objects and global role passwords may be out of scope.
5. Define owner-approved Recovery Point Objective (RPO) and Recovery Time Objective (RTO). A snapshot alone cannot recover writes added afterward. Confirm PITR availability and reconciliation of recent clinical/appointment writes.
6. Obtain explicit **separate Project Owner approval for Production migration execution** after the workflow is merged, with a low-traffic window and monitoring agreed. No approval by implication.

## Future execution checklist — DO NOT EXECUTE IN THIS TASK

1. Confirm the approved Main commit, exact migration hash, fresh DB/schema state, protected environment approvals and recovery plan.
2. Manually dispatch **Production Database Migration 0098 (Manual Approval Only)** from Main with:
   - `expected_main_sha` = owner's approved exact Main SHA;
   - `expected_migration_sha256` = `0d1c63717653e5b7f56e91698279b8896636c014601a75f75cf0794a9f77d100`;
   - `expected_current_revision` = `0097_member_page_permissions`.
3. Review credential-free source preflight. Protected environment reviewer approves only after separate execution authorization.
4. Execution revalidates URL identity/TLS and schema under the advisory lock: source 0097, no 0098 table, expected roles, parent uniqueness, and transaction safety. Unexpected state fails closed.
5. Before commit, verify final Alembic revision, exactly eight columns, five constraints, two composite CASCADE FKs, five indexes (three explicit), enabled RLS, no direct anon/authenticated privileges, unchanged prior table/index catalog and aggregate business row counts.
6. Independently verify post-commit schema, actual Doctor/Laser behavior, appointment integrity and error rates. Record run ID, actor, workflow SHA, source/hash, approvals and outcome.
7. PR #328 remains a **separate release decision**. No automatic merge or deployment.

## Failure and recovery rules

- Preflight, lock, permission, TLS or schema mismatch: abort. Never bypass or retry blindly.
- Transactional upgrade failure: PostgreSQL should roll back uncommitted DDL, but inspect actual revision/schema before proceeding.
- Post-commit failure: the DB may already be at 0098. Stop; do not retry upgrade. Review schema, escalate to DBA/owner, propose reviewed **roll-forward** corrections if appropriate.
- Application incompatibility: consider separate approved application rollback/feature disable. Destructive `alembic downgrade` is not a safe default.
- Restore from backup requires separate approval, verified PITR/RPO, and reconciliation because any new clinical writes after the backup point can otherwise be lost.

**Governance:** Workflow review → workflow merge approval → fresh Production attestation → explicit execution approval → migration & verification → independent PR #328 release decision.
