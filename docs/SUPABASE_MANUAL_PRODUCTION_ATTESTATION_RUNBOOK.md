# Supabase Manual Production Database Attestation Runbook

**Status: REVIEW ONLY — NOT AUTHORIZED FOR PRODUCTION EXECUTION**

**Owner:** Project Owner / authorized DBA
**Independent reviewer:** Team Leader / Database Security Reviewer
**Repository baseline:** `4c52e30f6fd75de84a1bc77eb78442b03df391fb`
**Tracking:** [Issue #321](https://github.com/GirgesAdam/Linka_AI/issues/321)

This procedure defines a **manual, explicitly authorized, metadata-only** review
for the Supabase-managed Tia AI Production project. Writing this document did
**not** access Production, run SQL, modify permissions, or attest the live system.
It is not a deployment or migration approval.

## 1. Supabase compatibility and non-negotiable boundaries

- The historical [dedicated attestor runbook](../.github/scripts/PRODUCTION_DB_READONLY_ROLE_RUNBOOK.md)
  is **NOT APPROVED FOR SUPABASE PRODUCTION EXECUTION**. Do not create/enable
  `tia_production_attestor`, revoke `PUBLIC CONNECT` or `PUBLIC TEMPORARY`,
  provision a `SECURITY DEFINER` wrapper, or change Supabase-managed role ACLs.
- The existing `.github/workflows/production-db-readonly-attestation.yml` is
  **NOT AUTHORIZED FOR PRODUCTION DISPATCH**. It remains in the repository;
  these instructions do **not** claim it is technically disabled. Do not add
  `PRODUCTION_DB_READONLY_URL`; never substitute `MIGRATION_DATABASE_URL`.
- No production/staging connection, SQL execution, database mutation, secret
  access, role change, or workflow dispatch is authorized by this document.
  The operator needs a **new explicit Owner/DBA approval** for every live run.
- Avoid patients, appointments, medical content, application rows, passwords,
  API keys, credential tables, environment settings, and sensitive function
  source. Only the fixed queries in section 4 may be considered for approval.
- A SQL statement beginning with `SELECT` is **not automatically safe**:
  functions may have side effects. Do not modify the manifest, append a query,
  invoke arbitrary functions, or use `DO`, `CALL`, `SET`, `GRANT`, `REVOKE`,
  `CREATE`, `ALTER`, `DROP`, `INSERT`, `UPDATE`, `DELETE`, `EXPLAIN ANALYZE`,
  `COPY`, or `pg_control_system()`. The explicitly listed catalog and built-in
  inspection functions are the only permitted function calls.
- Supabase recommends preserving its provider-required PUBLIC grants; do not
  attempt to enforce the historical dedicated-role CONNECT/TEMP contract here.

## 2. Authorization gate and SQL execution context

Before any future live verification, record **all** of the following in a
restricted evidence register (never in an unrestricted GitHub issue):

| Required approval/evidence | Entry |
| --- | --- |
| Change/reference ID and UTC window | To be authorized |
| Supabase project name and complete project ref | Owner/DBA to verify in Dashboard |
| Target database | `postgres` — verify independently |
| Approved operator and Dashboard organization/project role | To be approved |
| Independent DBA approval of manifest revision | To be approved |
| Independent reviewer, retention policy, secure evidence location | To be approved |
| Authorized execution surface and query permissions | To be proved for the live account |

**Dashboard Read-Only assessment (not presumed available):**

- Supabase documents the Dashboard `Read-Only` organization/project role on
  **Team and Enterprise** plans. Neither the current plan nor operator access
  is verified by this repository.
- Supabase states Read-Only SQL query snippets are limited to `SELECT` and
  execute as `supabase_read_only_user`, which holds `pg_read_all_data`. This
  restricts mutation via the intended UI but gives a **broad read scope**, not
  the old narrowly limited Attestor role; the role can also have visibility
  into platform secrets. Never query those data, even if accessible.
- Project-scoped Dashboard access and the presence of an approved SQL execution
  surface must be verified for the particular project, plan, and account.
- An Owner/Admin/Developer SQL Editor session may execute via a powerful
  PostgreSQL role; reviewing a `SELECT` manifest is a **procedural** control,
  not equivalent to technical database-enforced read-only access.
- If a fixed query is unavailable or forbidden for the chosen Dashboard role,
  capture the sanitized failure, do **not** promote privileges automatically,
  and classify the resulting evidence per section 6. A separate Owner/DBA
  approval is required before changing the execution method.
- The optional Supabase Management API read-only query endpoint is documented
  as Beta/experimental; this runbook does not authorize its use or assume it
  provides the old dedicated-role security contract.

**Project identity:** compare the exact Supabase Dashboard project reference and
environment label with the Owner-approved target record **before** selecting
the SQL Editor or running anything. SQL database name is not a substitute for
Supabase project identity. Preserve the complete ref only in restricted evidence;
redact it in review summaries.

## 3. Preflight: fixed expectations, scope, and stop conditions

The existing repository contract specifies:

| Property | Expected value | Source |
| --- | --- | --- |
| Supabase project | Tia AI Production | Owner-approved project record; verify in Dashboard |
| Database name | `postgres` | Existing attestation reference |
| Database OID | `5` | Historical repository reference — must verify live |
| PostgreSQL | `17.6` (`server_version_num = 170006`) | Historical repository reference |
| Schema | `public` | Existing attestation reference |
| Alembic revision | exactly `0097_member_page_permissions` | Existing attestation reference |
| Cluster system identifier | Historical fingerprint exists in code, but **not verified by this manifest** | Do not call privileged control functions |
| Primary/replica | Detect where the approved role permits | `pg_is_in_recovery()` is read-only |
| Role/ACL expectations | No live policy verdict until reviewed against authorized baseline | Sections 4–6 |

**STOP BEFORE SQL** if the project reference or Owner/DBA approval is absent,
wrong project selected, role permissions not established, query text differs
from this manifest, or scope extends to application data.

**STOP AFTER SQL** if mandatory database identity, version, OID, schema, or
Alembic evidence is missing or differs; if an unexpected security condition
is observed; or if any query unexpectedly attempts a write or privileged
operation. Do not troubleshoot by switching to a higher role or running
alternative queries without separate approval.

## 4. Fixed SELECT-only SQL query manifest

Only these numbered statements may be considered for future Owner/DBA-approved
manual execution. Run individually in the authorized SQL surface, in this order.
Do not execute any query from this file solely because it exists here.

All permitted functions are PostgreSQL catalog inspection or read-only
session/replication-status functions. **No application business data is read**
except the explicitly approved migration version field.

### M01 — Session identity and server version (mandatory)

```sql
SELECT
    pg_catalog.current_database() AS database_name,
    session_user::text AS session_user,
    current_user::text AS current_user,
    pg_catalog.current_setting('server_version') AS server_version,
    pg_catalog.current_setting('server_version_num')::integer
        AS server_version_num;
```

Record the actual DB identity and session role; do not assume the Dashboard
operator identity equals `session_user`. Verify `170006` exactly.

### M02 — Database OID and basic database metadata (mandatory)

```sql
SELECT
    d.oid::text AS database_oid,
    d.datname,
    d.datallowconn,
    d.datistemplate
FROM pg_catalog.pg_database AS d
WHERE d.datname = pg_catalog.current_database();
```

Require exactly one result: `postgres` with OID `5`. A mismatch is `BLOCKED`.

### M03 — Schema existence (mandatory)

```sql
SELECT n.nspname AS schema_name
FROM pg_catalog.pg_namespace AS n
WHERE n.nspname = 'public';
```

Require exactly one `public` result. Do not read user tables in this step.

### M04 — Migration revision (mandatory; single approved metadata table)

```sql
SELECT version_num
FROM public.alembic_version
ORDER BY version_num;
```

Require **exactly one** row, `0097_member_page_permissions`. Zero rows,
multiple revisions, permission denied, or another revision is a `BLOCKED`
mandatory-evidence failure; do not substitute migration tools or credentials.

### M05 — Primary/replica state (conditional where available)

```sql
SELECT pg_catalog.pg_is_in_recovery() AS is_replica;
```

`false` suggests a primary; `true` a standby. Record limitations if the query is
not permitted. Do not use the result alone to prove an authorized project
identity or high-availability readiness.

### M06 — Database inventory and PUBLIC CONNECT/TEMP ACL (security evidence)

```sql
SELECT
    d.datname,
    d.datallowconn,
    d.datistemplate,
    pg_catalog.pg_get_userbyid(d.datdba) AS database_owner,
    d.datacl::text AS stored_datacl,
    EXISTS (
        SELECT 1
        FROM pg_catalog.aclexplode(
            COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
        ) AS a
        WHERE a.grantee = 0 AND a.privilege_type = 'CONNECT'
    ) AS public_connect,
    EXISTS (
        SELECT 1
        FROM pg_catalog.aclexplode(
            COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
        ) AS a
        WHERE a.grantee = 0 AND a.privilege_type = 'TEMPORARY'
    ) AS public_temp
FROM pg_catalog.pg_database AS d
ORDER BY d.datname;
```

NULL `datacl` represents default ACL interpretation; do not treat NULL as
"no permissions". Preserve `datallowconn` and template flags separately from
effective privileges.

### M07 — Expanded CONNECT/TEMP grants, grantors, grant options

```sql
SELECT
    d.datname,
    CASE
        WHEN a.grantee = 0 THEN 'PUBLIC'
        ELSE pg_catalog.pg_get_userbyid(a.grantee)
    END AS grantee,
    pg_catalog.pg_get_userbyid(a.grantor) AS grantor,
    a.privilege_type,
    a.is_grantable
FROM pg_catalog.pg_database AS d
CROSS JOIN LATERAL pg_catalog.aclexplode(
    COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
) AS a
WHERE a.privilege_type IN ('CONNECT', 'TEMPORARY')
ORDER BY d.datname, grantee, a.privilege_type;
```

Do not infer inheritance from direct ACL rows alone; compare M08–M10.

### M08 — Non-secret role attributes

```sql
SELECT
    rolname,
    rolcanlogin,
    rolsuper,
    rolinherit,
    rolcreatedb,
    rolcreaterole,
    rolreplication,
    rolbypassrls
FROM pg_catalog.pg_roles
ORDER BY rolname;
```

**Do not query** `pg_authid`, password hashes, `rolpassword`, or role
configuration containing secrets.

### M09 — Role membership paths

```sql
SELECT
    parent.rolname AS granted_role,
    member.rolname AS member_role,
    m.inherit_option,
    m.set_option,
    m.admin_option
FROM pg_catalog.pg_auth_members AS m
JOIN pg_catalog.pg_roles AS parent ON parent.oid = m.roleid
JOIN pg_catalog.pg_roles AS member ON member.oid = m.member
ORDER BY member.rolname, parent.rolname;
```

These paths explain potential inherited privileges, but review effective
privileges and PostgreSQL version-specific membership semantics separately.

### M10 — Effective CONNECT and TEMP by role/database

```sql
SELECT
    d.datname,
    d.datallowconn,
    r.rolname,
    r.rolcanlogin,
    pg_catalog.has_database_privilege(
        r.oid, d.oid, 'CONNECT'
    ) AS effective_connect,
    pg_catalog.has_database_privilege(
        r.oid, d.oid, 'TEMPORARY'
    ) AS effective_temp
FROM pg_catalog.pg_database AS d
CROSS JOIN pg_catalog.pg_roles AS r
WHERE r.rolcanlogin
   OR r.rolname = 'tia_production_attestor'
ORDER BY d.datname, r.rolname;
```

If `tia_production_attestor` does not exist, it produces **no row** for that
role: this is **NOT VERIFIED**, not a passing privilege check. Effective
CONNECT alone cannot prove successful authentication or a network route; no
non-target database connections are authorized by this procedure.

### M11 — Historical wrapper metadata presence (optional; no invocation)

```sql
SELECT
    n.nspname,
    p.proname,
    p.pronargs,
    p.prosecdef,
    pg_catalog.pg_get_userbyid(p.proowner) AS function_owner
FROM pg_catalog.pg_proc AS p
JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
WHERE n.nspname = 'attestation_private'
  AND p.proname = 'cluster_system_identifier'
  AND p.pronargs = 0;
```

No wrapper execution, `prosrc` extraction, `pg_control_system()` call, or
new wrapper is authorized. Absence of the wrapper is expected under the current
manual-only architecture and is **not** proof of a cluster fingerprint.

## 5. Evidence collection and independent review

Record each query using a restricted, encrypted evidence location outside the
repository; redact anything exported into the Team Leader report.

| Field | Evidence to retain |
| --- | --- |
| Review ID / UTC timestamp / approved time window | Human review register |
| Operator / Owner / DBA / reviewer | Separate identities; no self-approval |
| Project identity | Full project ref in restricted register, masked in tickets |
| Query IDs and reviewed manifest revision | M01–M11; no modified SQL |
| Database identity/version/OID/schema | M01–M03 actual results |
| Migration revision and cardinality | M04 actual result |
| Primary/replica status | M05 or reason unavailable |
| Database/role ACL observations | M06–M10, redacted summaries |
| Wrapper state | M11 or unavailable |
| Unavailable permissions and errors | Sanitized category, never secret text |
| Cluster system identifier | **NOT VERIFIED** unless a separately approved trusted source provides it |
| Technical read-only enforcement | Supabase role and available UI restrictions, or **NOT VERIFIED** |
| Reviewer decision and sign-off | Classification under section 6 |

Do not store raw role inventories, complete project identifiers, connection
strings, application data, secrets, or screenshots containing other sensitive
Dashboard content in an unrestricted GitHub PR/issue or committed Markdown file.
Do not publish captured output of `pg_roles`/`pg_auth_members` without review.

## 6. Mandatory verification classification

The reviewer must choose exactly one result and record reasons:

| Result | Definition |
| --- | --- |
| **VERIFIED** | All **independently approved** required identity, migration, role/ACL, cluster fingerprint, and technical read-only guarantees are actually evidenced. Missing old-contract safeguards cannot be silently waived or replaced by assumptions. |
| **LIMITED VERIFICATION** | Core identity and approved mandatory metadata match, but at least one old-contract guarantee (e.g., cluster system identifier or dedicated-role technical read-only enforcement) remains unverified. This is the normal maximum outcome for this manual manifest alone. |
| **BLOCKED** | Wrong project/DB/OID/version/revision, missing mandatory core evidence or authorization, unexpected security finding, unreviewed query, unauthorized access, or an unresolved identity inconsistency. Stop and escalate. |

Mandatory core evidence includes **approved project identity**, M01–M04
matching the approved baseline, and no unexplained security anomaly. M05–M11
are required for a *complete security inventory*; if any required security
assessment cannot be completed, state its limitation explicitly and do not
claim a comprehensive ACL attestation.

**No automatic release approval:** `LIMITED VERIFICATION` is *never* a
Production release authorization. `BLOCKED` prohibits approval based on this
evidence. Even `VERIFIED` does not deploy anything; Owner/release approval is
a separate control.

A Dashboard Read-Only account does not by itself prove the former dedicated
role's narrow privileges. Conversely, an administrative SQL Editor session
does not prove technical read-only enforcement. A missing cluster-system
fingerprint must be prominently documented and cannot be inferred from DB OID,
project ref, or schema version.

## 7. Stop, incident, and exception policy

- If an identity comparison fails or a query returns surprising results: stop,
  preserve sanitized evidence and seek independent DBA review.
- If M04 returns unexpected Alembic state: record `BLOCKED`; **do not** run a
  migration, schema repair, or update.
- If SQL permissions are insufficient: stop that query; mark evidence incomplete.
  Do not grant privileges, activate a role, use a migration credential, or retry
  via an elevated session without new authorization.
- If the operator sees patient data, tokens, connection secrets, or unexpected
  PII: do not retain or forward them; follow the project's incident process.
- If any query body has changed, or if a SELECT calls an unreviewed function:
  reject execution and request a separately reviewed manifest revision.
- If network, cluster identity, or provider permissions cannot be verified:
  state the limit. Do not invent a successful verification.
- Do not run the historical GitHub workflow or configure its URL secret.

## 8. Sign-off template (fill only after a separately approved live review)

| Decision field | Value |
| --- | --- |
| Owner authorization ID | **NOT PROVIDED** |
| Supabase project ref checked | **NOT PERFORMED** |
| Operator, UTC execution timestamp | **NOT PERFORMED** |
| Reviewed manifest hash/version | **NOT RECORDED** |
| M01–M04 identity results | **NOT EXECUTED** |
| M05–M11 security evidence | **NOT EXECUTED** |
| Cluster system identifier | **NOT VERIFIED** |
| Technical read-only enforcement | **NOT VERIFIED** |
| Final classification | **NOT ASSESSED** |
| DBA signature/time | **PENDING** |
| Independent Team Leader signature/time | **PENDING** |
| Owner release decision | **SEPARATE APPROVAL REQUIRED** |

## 9. References and history

- [Supabase access control and Read-Only dashboard role](https://supabase.com/docs/guides/platform/access-control):
  Team/Enterprise availability, SELECT-only query snippets, and
  `supabase_read_only_user` / `pg_read_all_data` caveat.
- [Supabase role PUBLIC privilege guidance](https://supabase.com/kb/troubleshooting/custom-role-inherits-privileges-that-were-not-explicitly-granted-ddaa1c):
  preserve provider-required PUBLIC permissions.
- [Supabase SQL Editor](https://supabase.com/features/sql-editor):
  authorized operator experience; not a guarantee of technical read-only.
- [Supabase Management API read-only query endpoint](https://supabase.com/docs/reference/api/v1-read-only-query):
  Beta/experimental; not authorized as the execution path here.
- [Historical dedicated attestor design](../.github/scripts/PRODUCTION_DB_READONLY_ROLE_RUNBOOK.md):
  archival design and isolated PostgreSQL 17.6 tests; not production-ready.
- [Historical production attestation script](../.github/scripts/production_db_readonly_attestation.py):
  dedicated-role and cluster fingerprint contract, retained unchanged.
- [Issue #321](https://github.com/GirgesAdam/Linka_AI/issues/321):
  F-02/F-03 remediation follow-up.

**Current state of this document:** documentation only; no Production
verification or database modification was performed.
