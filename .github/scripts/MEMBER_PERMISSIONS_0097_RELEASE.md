# Member Permissions 0097 — Manual Release Contract

**Preparation only.** This Draft PR does not authorize a production migration, merge, deployment, or change to secrets/settings.

## Source, workflow, and isolation

- Release workflow: .github/workflows/member-permissions-0097-release.yml
- Trigger: workflow_dispatch only. No push, PR, schedule, or automatic execution.
- The release script comes from the reviewed main commit (the workflow SHA).
  A **separate detached, clean checkout** supplies the original Backend,
  Alembic env.py and 0097 migration from the exact PR #306 approved SHA:
  594c920ca3655851e5c9c54f7d6fec3dc7e2d32a.
- No backend/** or frontend/** changes. Railway currently watches backend/**
  and frontend/vercel.json has git.deploymentEnabled=false. Verify actual
  provider settings again before a future merge of this workflow PR.
- The new workflow uses database-migration-production concurrency, matching
  the old production database migration workflow. Railway Pre-deploy does
  not participate in this GitHub concurrency group. Owner coordination is
  still mandatory.

## Mandatory production safety gates

1. The release workflow must first be separately approved and merged to the
   default branch. It must be dispatched from main only, at run attempt 1.
2. The GitHub production Environment must have actual REQUIRED REVIEWERS.
   The control-phase job reads and verifies protection rules before the
   production job can receive any secrets. At 01K review, Required Reviewers
   were **NOT configured**. Setting changes are NOT authorized in this PR.
   If the GitHub token cannot read the rules, the job fails closed.
3. The GitHub production Environment MIGRATION_DATABASE_URL value is
   **NOT independently attested**. A separately authorized operator must
   confirm the connection to the same Railway/Supabase production target
   through a trusted channel without logging or disclosing connection data.
   The release job independently checks project ref ycuxjlkhnubztgqmhtom,
   database postgres, connection username/hostname/port, active revision
   0096_patient_customer_id, and absent 0097 columns.
4. The owner must establish a new, genuine, time-bound release freeze,
   confirm all GitHub merge/workflow and Railway deploy initiators, and
   confirm no other migration runner can target that database. Services
   must continue running. Post the signed JSON comment below on PR #306
   from GirgesAdam. The comment alone cannot mechanically freeze Railway.
5. The workflow checks fresh GitHub main/PR state, protection rules, new
   owner freeze, expiry and closure, previous release workflow runs,
   legacy migration workflow activity, and run attempt 1.
6. Same SQLAlchemy Engine, same transaction, same backend PID:
   SET LOCAL lock_timeout = 5s; SET LOCAL statement_timeout = 60s.
   A nonblocking PostgreSQL transaction advisory lock and a second
   0096+missing-columns check execute **before any DDL**.
7. Strict allowlist: two ALTER TABLE workspace_members additions and
   exactly one Alembic version update, all from original 0097 code.
   Any other write, lost timeout, wrong revision, expired freeze, or
   unexpected rerun aborts without automatic retry.
8. Success requires checking version 0097 and JSONB nullable plus
   INTEGER NOT NULL DEFAULT 0. The Release Operator must also check
   Railway pre-deploy No-op, Login/Workspace, Booking/CRM reads,
   WhatsApp/Automation, DB locks/errors, and API latency.

### Owner freeze JSON example — NOT an active attestation

The entire body of a new issue comment on PR #306 must be valid JSON,
authored by GirgesAdam. Replace workflow SHA, UTC timestamps and freeze ID
with real values before a separately approved dispatch. Maximum duration
30 minutes, never reuse a closed freeze.

    {
      "protocol": "linka_0097_release_freeze_v1",
      "status": "ACTIVE",
      "pr": 306,
      "operator": "GirgesAdam",
      "permissions_sha": "594c920ca3655851e5c9c54f7d6fec3dc7e2d32a",
      "workflow_sha": "REPLACE_WITH_REVIEWED_MAIN_SHA",
      "project_ref": "ycuxjlkhnubztgqmhtom",
      "database": "postgres",
      "migration": "0097_member_page_permissions",
      "freeze_id": "LINKA-306-0097-EXAMPLE123",
      "start_utc": "REPLACE_WITH_UTC_START",
      "end_utc": "REPLACE_WITH_UTC_END",
      "coordination": {
        "github_merges": "FROZEN",
        "github_migrations": "FROZEN",
        "railway_deploys": "FROZEN",
        "other_migration_runners": "FROZEN",
        "clinics_remain_online": true
      }
    }

The workflow dispatch requires the comment ID and explicit acknowledgement
EXECUTE_APPROVED_0097_ONLY. Post a separate FREEZE CLOSED comment containing
the same freeze ID when the freeze ends; never reuse a previous window.

## Isolated tests

Without secrets, unit suite:

    python -m unittest discover -s .github/scripts/tests -p test_*.py -v

The optional real-Alembic test requires a **local isolated PostgreSQL** at
0096 with a minimal workspace_members fixture, plus detached source checkout:

    LINKA_ISOLATED_0097_TEST=1
    LINKA_ISOLATED_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55484/postgres
    LINKA_APPROVED_SOURCE=/local/path/to/approved-sha-checkout
    python -m unittest discover -s .github/scripts/tests -p test_*.py -v

Tests exercise approved/wrong SHA, wrong target/revision, missing/expired
freeze, environment protection, transaction timeouts, extra DDL denial,
original 0096->0097 Alembic upgrade, exact schema, and repeat rejection.
The generic Alembic no-op was verified separately in 01F/01G; the release
workflow intentionally refuses a second execution rather than rerunning.

## Failure policy

Stop before DDL on any failed preflight. On a DDL/transaction failure, stop,
inspect actual revision/columns/locks, and DO NOT automatically retry or
downgrade. The next attempt needs separate Team Lead approval. A successful
schema migration does NOT authorize merging PR #306 or deploying a Backend.

The 5s lock timeout limits **individual lock acquisition**, while 60s
statement timeout limits **each statement**, not the total migration.
They do not guarantee zero downtime or latency impact on clinic operations.
