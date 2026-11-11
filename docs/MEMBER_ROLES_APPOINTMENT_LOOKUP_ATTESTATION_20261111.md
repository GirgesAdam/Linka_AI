# Member Roles — Appointment Lookup Separation & Attestation

**Date:** 2026-11-11
**Repository:** `GirgesAdam/Linka_AI`
**Scope:** Knowledge API authorization, appointment-only lookup contracts, and read-only Production Attestation hardening.

## Authorization contract

- `GET /clinic/knowledge` is protected by the `administration` section guard added by the Knowledge Administration Authorization workstream (PR 2). This snapshot guard was not present on the Main baseline before this workstream.
- The `POST /clinic/knowledge/ai/propose` and `POST /clinic/knowledge/ai/apply` endpoints retain their Administration guard; their business logic is unchanged by the snapshot-guard addition.
- The existing workspace `admin` role retains its administrator/manager bypass. The current role contract has only `admin` and `member`; no separate `manager` role is defined.
- A Member needs the existing `administration` grant to read the full Knowledge Snapshot.
- Clinic Operations alone, no grants, inactive membership, cross-workspace membership, and unauthenticated requests do not grant Knowledge Snapshot access.
- No new section permission or role was introduced.

## Appointment-only lookup contracts

### `GET /booking/branch-schedules`

Requires authenticated active workspace membership and does not require either section grant.

Returns an envelope scoped to the authenticated workspace:

- `workspace_timezone`: resolved from the authenticated Workspace and present even when no active branches exist.
- `branches[]`: active branches in that Workspace, each containing `id`, `name`, `timezone`, `is_active`, and `working_hours[]` with `weekday`, `start_time`, and `end_time`.

Each branch timezone takes precedence for that branch; the workspace timezone is the fallback when no branch is selected. `Africa/Cairo` remains the final fallback if the stored workspace timezone is invalid or unavailable.

The response excludes branch phone, email, and address fields.

### `GET /crm/patients/lookup`

Requires authenticated active workspace membership and does not require either section grant.

Two read-only lookup forms are provided:

- `GET /crm/patients/lookup?q=...` searches with `limit` (1–100) and `offset`.
- `POST /crm/patients/lookup/batch` accepts a JSON list of 1–200 patient IDs, avoiding long query strings for appointment pages.

The response is restricted to `id`, `first_name`, `last_name`, and `phone`. Phone values are omitted from the SQL projection and returned as null unless `include_phone=true` is explicitly requested by the phone-search or selected-patient workflow.

Both lookup modes are scoped to the authenticated workspace. IDs belonging to another workspace are omitted rather than returned.

## Frontend integration

- Appointments uses branch schedules for branch hours and timezone.
- Appointments uses batch patient lookup for the names displayed beside its current appointment set.
- Patient phone search and the selected-patient booking flow explicitly request phone data.
- Reschedule uses branch schedules and only looks up names for the other appointments displayed in the selected schedule.
- Neither Appointments nor Reschedule calls `GET /clinic/knowledge`.
- Knowledge editing actions remain separate and retain their existing Administration protection.

## Production Attestation contract

The migration revision sequence is:

| Phase | Expected revision | Contract source |
|---|---|---|
| Pre-migration state | `0096_patient_customer_id` | `EXPECTED_PREVIOUS_REVISION` in `.github/scripts/production_migration_preflight.py` |
| Post-migration / read-only attestation state | `0097_member_page_permissions` | `REFERENCE["alembic_revision"]` in `.github/scripts/production_db_readonly_attestation.py` |

The read-only attestation checks the post-migration final state (`0097_member_page_permissions`); `0096_patient_customer_id` is only the approved pre-migration starting revision. This repository contract does not prove the live Production revision. Production migration status remains `NOT VERIFIED`.

The attestor verifies the two permission columns on `public.workspace_members` using PostgreSQL catalog metadata only:

| Column | Expected definition |
|---|---|
| `page_permissions` | `jsonb`, nullable, no default |
| `permissions_version` | `integer`, NOT NULL, default `0` |

A missing table/column, wrong type, nullability/default drift, old or missing revision, multiple revisions, unexpected schema, unexpected PostgreSQL version, or missing read-only credential fails closed.

The schema probe does not select membership rows or patient/appointment data. The attestation remains one read-only transaction with rollback. Migration credentials are never used as a fallback.

The PostgreSQL 17.6 CI integration uses the disposable `attestation_ci` service and a least-privilege local test role. It does not use Production or Staging credentials.

## Verification scope

The results below belong to different verification phases and test scopes. Their counts are not directly interchangeable.

### Earlier cumulative verification phase

- Focused backend/API, authorization, route-wiring, UI-contract, and migration-governance suite: **79 passed**.
- Read-only Production Attestation unit suite: **45 passed**.
- Frontend unit suite: **22 passed**.
- Existing Playwright browser-contract suites: **15 passed**. These are browser-level contract tests, not a complete authenticated Appointments/Reschedule flow.
- Frontend lint, TypeScript typecheck, and production build: passed.
- `ruff check app tests alembic` and Python `compileall` for `app`, `tests`, and `alembic`: passed.
- `ruff format --check` was attempted but is not clean because pre-existing staged files contain formatting differences outside the new repair blocks. No mass formatting was applied.

### Later focused repair verification

- A later focused backend/API regression re-run recorded **50 passed**. This was a different, narrower test selection and does not replace or supersede the earlier 79-test result.
- F-01 timezone fallback regression coverage: **8 cases passed**.
- PostgreSQL 17.6 role/catalog integration was not available in the earlier local phase because the Docker Desktop Linux engine was unavailable and `ATTESTATION_TEST_DATABASE_URL` was not configured; the PostgreSQL integration classes were skipped in that phase.
- In a later isolated run, PostgreSQL 17.6 integration completed with **37/37 passed** against a disposable local test database. This is local integration evidence only, not Production evidence.
- The PostgreSQL 17.6 CI integration uses the disposable `attestation_ci` service and a least-privilege test role. It does not use Production or Staging credentials.
- PR 1 (`#318`) local verification on its exact HEAD recorded 23 backend tests, 22 frontend unit tests, 14 Appointment Playwright contracts, Ruff, compileall, TypeScript, ESLint, and Next.js production build as passed. Its GitHub Actions run `43065391241` completed with all four jobs successful.
- PR 2 (`#319`) local verification on its exact HEAD recorded 19 backend tests, Ruff, and compileall as passed. Its GitHub Actions run `43066302927` completed with all four jobs successful.

### Explicit verification limits

- Full authenticated Appointments/Reschedule browser E2E was not run. The existing `final-internal-gate.spec.ts` uses shared `TIA_E2E_*` workspaces and performs state-changing inbox and branch operations; no isolated fixture-backed application test environment is configured. It was not run or expanded.
- Local or CI Attestation success does not prove the live Production revision or Production database identity. Production Migration 0097 remains **NOT VERIFIED**.
- No Production or Staging database was contacted to obtain any of these results.

## Safety boundary

No Production or Staging database connection, SQL/DDL, migration, secret read/change, environment change, Railway configuration change, deployment, restart, rollback, Main push, merge, or workflow dispatch is authorized or performed by this task.
