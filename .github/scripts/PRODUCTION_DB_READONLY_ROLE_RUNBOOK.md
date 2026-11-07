# Tia Production Read-only Attestation Role Runbook

Status: REVIEW ONLY - DO NOT EXECUTE WITHOUT EXPLICIT PRODUCTION CHANGE APPROVAL.

This runbook is for the Project Owner / database administrator. It does not create a
Production role, alter a Production database, configure a GitHub Environment, or run
an attestation as part of this PR.

## Separation contract

| Purpose | GitHub Environment | Secret |
|---|---|---|
| Database migrations | `production` | `MIGRATION_DATABASE_URL` |
| Read-only attestation | `production-attestation` | `PRODUCTION_DB_READONLY_URL` |

The attestation workflow must never receive `MIGRATION_DATABASE_URL`.

## Intended database role

Role name: `tia_production_attestor`

The role is deliberately:

- LOGIN only after a password is set out-of-band.
- NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS.
- NOINHERIT, with no memberships in any other role.
- Granted CONNECT directly on the expected target database postgres, USAGE on
  public, and SELECT on public.alembic_version.
- Denied effective CONNECT on every non-target database by removing PUBLIC CONNECT
  only after preserving the reviewed access of existing application/service roles.
- Allowed to execute one zero-argument SECURITY DEFINER function that returns only
  the cluster system identifier.
- Not granted `pg_monitor`, `pg_read_all_settings`, or any broad monitoring role.


## Database identity and cluster-wide CONNECT boundary

The repository's approved attestation reference identifies the expected Tia
Production database as:

| Identity field | Expected value | Evidence |
|---|---|---|
| Database name | postgres | REFERENCE["database_name"] in production_db_readonly_attestation.py |
| Database OID | 5 | REFERENCE["database_oid"] in the same attestation contract |
| Schema | public | REFERENCE["schema"] |
| PostgreSQL version | 17.6 | REFERENCE["postgres_version"] |
| Alembic revision | 0096_patient_customer_id | REFERENCE["alembic_revision"] |

.env.example also uses /postgres as the database path for the migration
connection. The disposable PostgreSQL 17.6 CI database is named
attestation_ci; it is a test target and must never be mistaken for Production.

This is the expected identity encoded in the repository, not a live Production
verification. This PR must not read the Production secret or connect to Production
to establish it. The separately authorized owner/DBA process must verify the live
target before any future Production change.

A database-specific GRANT CONNECT ON DATABASE postgres TO
tia_production_attestor does not restrict the role to that database. PostgreSQL
has no DENY privilege that overrides PUBLIC or inherited CONNECT. On databases
whose ACL grants CONNECT to PUBLIC, every role can receive effective CONNECT
through PUBLIC, including a role with NOINHERIT. A direct
REVOKE CONNECT ... FROM tia_production_attestor cannot cancel that PUBLIC grant.

The required boundary is:

- tia_production_attestor: effective CONNECT on postgres only.
- Every non-target database: no PUBLIC CONNECT; no direct or inherited CONNECT
  path for the attestor.
- Existing application/service roles that require non-target database access:
  retain effective CONNECT through their existing direct/group grants or through
  explicitly approved replacement grants.
- Do not change the target database's existing PUBLIC ACL as part of this
  narrowly scoped repair.

## Cluster-wide CONNECT inventory and impact assessment

Run the following read-only inventory before preparing any ACL change. Retain the
raw datacl, expanded ACL rows, role matrix, database owner, and role-membership
output with the change record.

Database inventory and PUBLIC CONNECT ACL:

```
SELECT
    d.datname,
    d.datallowconn,
    pg_catalog.pg_get_userbyid(d.datdba) AS database_owner,
    d.datacl::text AS stored_datacl,
    EXISTS (
        SELECT 1
        FROM pg_catalog.aclexplode(
            COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
        ) AS acl
        WHERE acl.grantee = 0
          AND acl.privilege_type = 'CONNECT'
    ) AS public_connect,
    EXISTS (
        SELECT 1
        FROM pg_catalog.aclexplode(
            COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
        ) AS acl
        WHERE acl.grantee = 0
          AND acl.privilege_type = 'CONNECT'
          AND acl.is_grantable
    ) AS public_connect_grantable
FROM pg_catalog.pg_database AS d
ORDER BY d.datname;
```


Expanded database ACL rows, including grantor and grant-option evidence:

```
SELECT
    d.datname,
    d.datallowconn,
    d.datacl::text AS stored_datacl,
    acl.grantee,
    CASE
        WHEN acl.grantee = 0 THEN 'PUBLIC'
        ELSE pg_catalog.pg_get_userbyid(acl.grantee)
    END AS grantee_name,
    acl.privilege_type,
    acl.is_grantable,
    pg_catalog.pg_get_userbyid(acl.grantor) AS grantor_name
FROM pg_catalog.pg_database AS d
CROSS JOIN LATERAL pg_catalog.aclexplode(
    COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
) AS acl
ORDER BY d.datname, grantee_name, acl.privilege_type;
```

Direct, PUBLIC, inherited, and effective CONNECT matrix:

```
SELECT
    d.datname,
    r.rolname,
    r.rolcanlogin,
    r.rolsuper,
    pg_catalog.has_database_privilege(
        r.rolname, d.datname, 'CONNECT'
    ) AS effective_connect,
    EXISTS (
        SELECT 1
        FROM pg_catalog.aclexplode(
            COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
        ) AS acl
        WHERE acl.grantee = r.oid
          AND acl.privilege_type = 'CONNECT'
    ) AS direct_connect,
    EXISTS (
        SELECT 1
        FROM pg_catalog.aclexplode(
            COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
        ) AS acl
        WHERE acl.grantee = 0
          AND acl.privilege_type = 'CONNECT'
    ) AS public_connect,
    CASE
        WHEN r.rolsuper THEN 'SUPERUSER'
        WHEN EXISTS (
            SELECT 1
            FROM pg_catalog.aclexplode(
                COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
            ) AS acl
            WHERE acl.grantee = r.oid
              AND acl.privilege_type = 'CONNECT'
        ) THEN 'DIRECT'
        WHEN EXISTS (
            WITH RECURSIVE inherited_roles(role_oid) AS (
                SELECT m.roleid
                FROM pg_catalog.pg_auth_members AS m
                WHERE m.member = r.oid
                  AND m.inherit_option
                UNION
                SELECT m.roleid
                FROM pg_catalog.pg_auth_members AS m
                JOIN inherited_roles AS inherited
                  ON m.member = inherited.role_oid
                WHERE m.inherit_option
            )
            SELECT 1
            FROM inherited_roles AS inherited
            JOIN pg_catalog.pg_roles AS parent
              ON parent.oid = inherited.role_oid
            WHERE parent.rolsuper
               OR EXISTS (
                   SELECT 1
                   FROM pg_catalog.aclexplode(
                       COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
                   ) AS acl
                   WHERE acl.grantee = parent.oid
                     AND acl.privilege_type = 'CONNECT'
               )
        ) THEN 'INHERITED'
        WHEN EXISTS (
            SELECT 1
            FROM pg_catalog.aclexplode(
                COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
            ) AS acl
            WHERE acl.grantee = 0
              AND acl.privilege_type = 'CONNECT'
        ) THEN 'PUBLIC'
        WHEN pg_catalog.has_database_privilege(
            r.rolname, d.datname, 'CONNECT'
        ) THEN 'OTHER'
        ELSE 'NONE'
    END AS connect_path
FROM pg_catalog.pg_database AS d
CROSS JOIN pg_catalog.pg_roles AS r
WHERE d.datname <> 'postgres'
ORDER BY d.datname, r.rolcanlogin DESC, r.rolname;
```

Role membership paths:

```
SELECT
    parent.rolname AS granted_role,
    member.rolname AS member_role,
    m.admin_option,
    m.inherit_option,
    m.set_option
FROM pg_catalog.pg_auth_members AS m
JOIN pg_catalog.pg_roles AS parent ON parent.oid = m.roleid
JOIN pg_catalog.pg_roles AS member ON member.oid = m.member
ORDER BY parent.rolname, member.rolname;
```

Before proceeding:

1. Inventory every database, including databases with datallowconn = false.
   PUBLIC CONNECT is revoked from every non-target database where it exists;
   datallowconn = false does not make an ACL grant disappear.
2. Identify every existing login, service, and group role whose effective CONNECT
   would be lost when PUBLIC CONNECT is removed from a connectable non-target DB.
3. Obtain service-owner confirmation for each role/database pair that must retain
   access. Preserve group-role grants where that is the existing intended access
   path; do not replace them with arbitrary grants to individual users.
4. The approved regrant list must preserve the effective CONNECT of every existing
   login role. Regranting to a reviewed group role is allowed when that is the
   intended access path. The transaction fails and rolls back if any login role
   loses effective CONNECT after the approved grants are applied.
5. If a role cannot be mapped to an owner or its required access cannot be
   established, STOP. Do not execute a partial PUBLIC revoke.
6. If PUBLIC CONNECT has grant option, or its grantor is not the database owner,
   STOP for a separately reviewed ACL/rollback plan. Do not use CASCADE.

The repair changes database ACLs only on non-target databases. It leaves existing
direct grants and membership paths intact and explicitly regrants only approved
roles that would otherwise lose effective CONNECT. New roles will no longer
receive CONNECT automatically through PUBLIC on those databases.

### Mandatory guard for future databases

PostgreSQL's default database ACL grants CONNECT to PUBLIC for a newly created
database. There is no database-level ALTER DEFAULT PRIVILEGES setting that
removes this grant for one role. Therefore the existing-database repair is not
durable unless every future database is provisioned through the following
fail-closed sequence. The attestor role is NOCREATEDB and must never create
databases itself.

CREATE DATABASE must run outside a transaction:

```sql
CREATE DATABASE new_database WITH ALLOW_CONNECTIONS false;
REVOKE CONNECT ON DATABASE new_database FROM PUBLIC;
-- Add only the service-owner-approved database CONNECT grants:
GRANT CONNECT ON DATABASE new_database TO approved_application_group;
ALTER DATABASE new_database WITH ALLOW_CONNECTIONS true;

SELECT
    pg_catalog.has_database_privilege(
        'tia_production_attestor', 'new_database', 'CONNECT'
    ) AS attestor_connect,
    pg_catalog.has_database_privilege(
        'approved_application_group', 'new_database', 'CONNECT'
    ) AS application_group_connect;
```

Required result: attestor_connect = false and application_group_connect = true.
Then test real connections using the attestor and approved application credentials.
Do not enable connections until the ACL and effective-privilege checks pass.
If the database creation process cannot enforce this order, do not claim durable
cluster-wide isolation; keep Production attestation disabled and obtain a
separately reviewed connection-layer restriction or provisioning control.

## Root cause established on disposable PostgreSQL 17.6

The failed exact-head CI run `42377421472` showed that
`tia_attestor_ci` could execute `SELECT pg_control_system()` even though the role
had no role memberships and was not a member of `pg_monitor`.

The PostgreSQL 17.6 integration test records the built-in function ACL,
expands the default ACL when proacl is NULL, and checks
has_function_privilege. The separate CONNECT root cause is database-local:

- PostgreSQL's default database ACL grants CONNECT to PUBLIC.
- A direct GRANT CONNECT ON DATABASE postgres adds a grant; it does not remove
  PUBLIC CONNECT from other databases.
- A direct REVOKE CONNECT ... FROM tia_production_attestor is not a DENY and
  cannot override CONNECT inherited from PUBLIC.
- NOINHERIT prevents ordinary role-membership inheritance; it does not remove
  privileges granted to PUBLIC.
- Therefore the account can connect to non-target databases until PUBLIC CONNECT
  is removed from those databases or an independently supported connection-layer
  restriction is used.

The existing function-level finding remains separate: the stock PostgreSQL 17.6
default pg_control_system() function ACL includes PUBLIC EXECUTE. The function
repair revokes PUBLIC EXECUTE on that one function and grants the attestor only the
narrow SECURITY DEFINER wrapper. It does not change any other pg_catalog
function privilege.

## Scope and impact review — mandatory before any Production change

The ACL of a built-in function is stored in the current database's
`pg_catalog.pg_proc`. The function-level change affects the current database only;
it does not change function ACLs in other databases in the same PostgreSQL cluster.

The separate CONNECT isolation repair changes database ACLs on every inventoried
non-target database in the cluster. It does not change application table or
function ACLs in those databases. Existing login-role effective CONNECT is
preserved through the approved regrant process described above.

Revoking EXECUTE from PUBLIC affects every non-superuser role in this database
whose only effective path to `pg_control_system()` is PUBLIC. It does not remove
separate explicit grants or role memberships. The disposable CI database proves
the intended attestor behavior; it does not prove that no other Production role or
external job depends on this function.

Before approving the Production change, the DBA must:

1. Save the pre-change `proacl` output and the expanded ACL rows below.
2. Review the effective-access inventory for all login roles and relevant service
   roles. A role showing EXECUTE is a potential affected consumer, not proof that
   it actually calls the function.
3. Search the application, operational scripts, dashboards, and scheduled jobs for
   direct calls to `pg_control_system()`.
4. Confirm with the service owners that no legitimate consumer requires direct
   access, or separately approve migration of that consumer to a reviewed wrapper.
5. Obtain explicit Project Owner approval for the database-local PUBLIC revoke.

If a required consumer cannot use the approved wrapper, STOP. Do not keep PUBLIC
EXECUTE, grant `pg_monitor`, or broaden the attestor role to make the check pass.
Return the evidence and request a separately reviewed design.

## Preconditions

1. Confirm this is the single Tia Production database, not a staging or test target.
2. Confirm the active PostgreSQL version and current Alembic revision.
3. Confirm the administrator session is connected to the intended database
   (`postgres`).
4. Take and verify the normal database backup / recovery point required by the
   clinic's change process.
5. Review the current ACL and all potentially affected consumers before proceeding.
6. Confirm the administrator is authorized to revoke EXECUTE on the built-in
   function and to create a SECURITY DEFINER function owned by `postgres`.

## Pre-change ACL evidence — read-only

Run these queries before the change and retain the non-secret output with the
change record.

Stored ACL and effective PUBLIC entries:

```sql
SELECT
    p.oid::regprocedure AS function_name,
    pg_catalog.pg_get_userbyid(p.proowner) AS function_owner,
    p.proacl::text AS stored_proacl,
    p.proacl IS NULL AS uses_default_acl,
    acl.grantee,
    CASE
        WHEN acl.grantee = 0 THEN 'PUBLIC'
        ELSE pg_catalog.pg_get_userbyid(acl.grantee)
    END AS grantee_name,
    acl.privilege_type,
    acl.is_grantable,
    pg_catalog.pg_get_userbyid(acl.grantor) AS grantor_name
FROM pg_catalog.pg_proc AS p
CROSS JOIN LATERAL pg_catalog.aclexplode(
    COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
) AS acl
WHERE p.oid = 'pg_catalog.pg_control_system()'::regprocedure
ORDER BY grantee_name, privilege_type;
```

Effective EXECUTE for login roles:

```sql
SELECT
    r.rolname,
    r.rolcanlogin,
    r.rolsuper,
    pg_catalog.has_function_privilege(
        r.oid,
        'pg_catalog.pg_control_system()'::regprocedure,
        'EXECUTE'
    ) AS effective_execute
FROM pg_catalog.pg_roles AS r
WHERE NOT r.rolsuper
ORDER BY r.rolcanlogin DESC, r.rolname;
```

For the disposable PostgreSQL 17.6 baseline, the expected source is the PUBLIC
EXECUTE entry in the default ACL, with no direct grant to `tia_attestor_ci` and
no role membership. The production DBA must use the actual query output, not
assume that Production has the same ACL.

The runbook below deliberately stops if the stored ACL is not the default
(`proacl IS NULL`), if PUBLIC does not have EXECUTE, or if the PUBLIC grant is
grantable. Such a state needs an individually reviewed ACL-preservation and
rollback plan; do not run the generic script.

## SQL — administrator review and approval required

Run only in the Tia Production database as the authorized database administrator.
The SQL intentionally fails if the role or private schema already exists. It also
rechecks the expected original ACL inside the transaction. If any statement fails,
the transaction must be rolled back.

```sql
BEGIN;

DO $preflight$
DECLARE
    v_proacl_is_null boolean;
    v_public_execute boolean;
    v_public_grantable boolean;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'tia_production_attestor'
    ) THEN
        RAISE EXCEPTION
            'tia_production_attestor already exists; stop for separate review';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace
        WHERE nspname = 'attestation_private'
    ) THEN
        RAISE EXCEPTION
            'attestation_private already exists; stop for separate review';
    END IF;

    SELECT
        p.proacl IS NULL,
        EXISTS (
            SELECT 1
            FROM pg_catalog.aclexplode(
                COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
            ) AS acl
            WHERE acl.grantee = 0
              AND acl.privilege_type = 'EXECUTE'
        ),
        EXISTS (
            SELECT 1
            FROM pg_catalog.aclexplode(
                COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
            ) AS acl
            WHERE acl.grantee = 0
              AND acl.privilege_type = 'EXECUTE'
              AND acl.is_grantable
        )
    INTO v_proacl_is_null, v_public_execute, v_public_grantable
    FROM pg_catalog.pg_proc AS p
    WHERE p.oid = 'pg_catalog.pg_control_system()'::regprocedure;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'pg_control_system() was not found';
    END IF;
    IF NOT v_proacl_is_null THEN
        RAISE EXCEPTION
            'pg_control_system() has a non-default ACL; stop and review exact grants';
    END IF;
    IF NOT v_public_execute THEN
        RAISE EXCEPTION
            'PUBLIC EXECUTE was not found; do not apply the assumed repair';
    END IF;
    IF v_public_grantable THEN
        RAISE EXCEPTION
            'PUBLIC EXECUTE has grant option; stop for a custom rollback plan';
    END IF;
END
$preflight$;

-- Capture the current effective CONNECT set for every connectable non-target DB.
-- The DBA must review this inventory and populate the approved regrant table.
CREATE TEMP TABLE connect_before ON COMMIT DROP AS
SELECT d.datname AS database_name, r.rolname AS role_name
FROM pg_catalog.pg_database AS d
CROSS JOIN pg_catalog.pg_roles AS r
WHERE d.datname <> 'postgres'
  AND d.datallowconn
  AND r.rolcanlogin
  AND pg_catalog.has_database_privilege(r.rolname, d.datname, 'CONNECT');

CREATE TEMP TABLE public_connect_before ON COMMIT DROP AS
SELECT
    d.datname AS database_name,
    d.datacl::text AS stored_datacl,
    d.datdba AS database_owner_oid
FROM pg_catalog.pg_database AS d
WHERE d.datname <> 'postgres'
  AND EXISTS (
      SELECT 1
      FROM pg_catalog.aclexplode(
          COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
      ) AS acl
      WHERE acl.grantee = 0
        AND acl.privilege_type = 'CONNECT'
  );

CREATE TEMP TABLE approved_connect_regrant (
    database_name name NOT NULL,
    role_name name NOT NULL,
    PRIMARY KEY (database_name, role_name)
) ON COMMIT DROP;

CREATE TEMP TABLE connect_regrants_added (
    database_name name NOT NULL,
    role_name name NOT NULL,
    PRIMARY KEY (database_name, role_name)
) ON COMMIT DROP;

-- Insert only role/database pairs approved by the service owners.
-- Examples (replace with the complete reviewed inventory; do not copy blindly):
-- INSERT INTO pg_temp.approved_connect_regrant VALUES ('app_database', 'app_runtime');
-- INSERT INTO pg_temp.approved_connect_regrant VALUES ('app_database', 'migration_role');

DO $connect_isolation$
DECLARE
    v_database record;
    v_grant record;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_temp.public_connect_before AS before_acl
        JOIN pg_catalog.pg_database AS d
          ON d.datname = before_acl.database_name
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
        ) AS acl
        WHERE acl.grantee = 0
          AND acl.privilege_type = 'CONNECT'
          AND (
              acl.is_grantable
              OR acl.grantor <> d.datdba
              OR d.datdba <> (
                  SELECT oid
                  FROM pg_catalog.pg_roles
                  WHERE rolname = current_user
              )
          )
    ) THEN
        RAISE EXCEPTION
            'non-target PUBLIC CONNECT has grant option or non-owner grantor; stop for custom rollback';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_temp.approved_connect_regrant AS a
        LEFT JOIN pg_catalog.pg_database AS d
          ON d.datname = a.database_name
        LEFT JOIN pg_catalog.pg_roles AS r
          ON r.rolname = a.role_name
        WHERE d.datname IS NULL
           OR d.datname = 'postgres'
           OR NOT d.datallowconn
           OR r.oid IS NULL
           OR NOT pg_catalog.has_database_privilege(
               a.role_name, a.database_name, 'CONNECT'
           )
    ) THEN
        RAISE EXCEPTION
            'approved CONNECT regrant is not an existing effective role/database grant';
    END IF;

    -- Revoke only non-target PUBLIC CONNECT; never use CASCADE.
    FOR v_database IN
        SELECT before_acl.database_name
        FROM pg_temp.public_connect_before AS before_acl
        ORDER BY before_acl.database_name
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE CONNECT ON DATABASE %I FROM PUBLIC',
            v_database.database_name
        );
    END LOOP;

    -- Regrant only owner-approved roles. A group role may be listed when that
    -- is the intended access path for existing login roles.
    FOR v_grant IN
        SELECT a.database_name, a.role_name
        FROM pg_temp.approved_connect_regrant AS a
        WHERE NOT pg_catalog.has_database_privilege(
            a.role_name, a.database_name, 'CONNECT'
        )
        ORDER BY a.database_name, a.role_name
    LOOP
        EXECUTE pg_catalog.format(
            'GRANT CONNECT ON DATABASE %I TO %I',
            v_grant.database_name,
            v_grant.role_name
        );
        INSERT INTO pg_temp.connect_regrants_added(database_name, role_name)
        VALUES (v_grant.database_name, v_grant.role_name);
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM pg_temp.connect_before AS b
        WHERE NOT pg_catalog.has_database_privilege(
            b.role_name, b.database_name, 'CONNECT'
        )
    ) THEN
        RAISE EXCEPTION
            'post-repair effective CONNECT differs from the pre-change inventory';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_database AS d
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
        ) AS acl
        WHERE d.datname <> 'postgres'
          AND acl.grantee = 0
          AND acl.privilege_type = 'CONNECT'
    ) THEN
        RAISE EXCEPTION 'PUBLIC CONNECT remains on a non-target database';
    END IF;
END
$connect_isolation$;

-- Save these result sets outside the transaction/change record for exact rollback.
SELECT database_name, stored_datacl, database_owner_oid
FROM pg_temp.public_connect_before
ORDER BY database_name;

SELECT database_name, role_name
FROM pg_temp.connect_regrants_added
ORDER BY database_name, role_name;

SELECT database_name, role_name
FROM pg_temp.connect_before
ORDER BY database_name, role_name;

CREATE ROLE tia_production_attestor
    NOLOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS
    CONNECTION LIMIT 2;

-- Defense in depth only; database ACLs must independently deny all writes.
ALTER ROLE tia_production_attestor SET default_transaction_read_only = on;

GRANT CONNECT ON DATABASE postgres TO tia_production_attestor;
GRANT USAGE ON SCHEMA public TO tia_production_attestor;
GRANT SELECT ON TABLE public.alembic_version TO tia_production_attestor;

REVOKE CREATE ON SCHEMA public FROM tia_production_attestor;

DO $schema_check$
BEGIN
    IF has_schema_privilege(
        'tia_production_attestor', 'public', 'CREATE'
    ) THEN
        RAISE EXCEPTION
            'public schema CREATE is inherited; stop for separate privilege review';
    END IF;
END
$schema_check$;

-- This is database-local and affects all roles relying only on PUBLIC EXECUTE.
-- Keep it in this transaction with the SECURITY DEFINER wrapper creation.
REVOKE EXECUTE ON FUNCTION pg_catalog.pg_control_system() FROM PUBLIC;

CREATE SCHEMA attestation_private AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA attestation_private FROM PUBLIC;

CREATE FUNCTION attestation_private.cluster_system_identifier()
RETURNS text
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    SELECT system_identifier::text
    FROM pg_catalog.pg_control_system();
$function$;

ALTER FUNCTION attestation_private.cluster_system_identifier() OWNER TO postgres;
REVOKE ALL ON FUNCTION attestation_private.cluster_system_identifier() FROM PUBLIC;

GRANT USAGE ON SCHEMA attestation_private TO tia_production_attestor;
GRANT EXECUTE ON FUNCTION attestation_private.cluster_system_identifier()
    TO tia_production_attestor;

COMMIT;
```

Set the password interactively in psql so it is not embedded in this file or shell
history. Keep the role NOLOGIN until the password has been set:

```text
\password tia_production_attestor
ALTER ROLE tia_production_attestor LOGIN;
```

Do not paste the password or resulting connection URI into chat, logs, tickets, or
source control.

## Required post-change privilege verification

Run as the administrator and retain the results.

Role attributes and memberships:

```sql
SELECT
    rolname, rolcanlogin, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
    rolreplication, rolbypassrls, rolconfig
FROM pg_catalog.pg_roles
WHERE rolname = 'tia_production_attestor';

-- Effective database CONNECT must be true only for the target database.
SELECT
    d.datname,
    d.datallowconn,
    pg_catalog.has_database_privilege(
        'tia_production_attestor', d.datname, 'CONNECT'
    ) AS attestor_effective_connect,
    EXISTS (
        SELECT 1
        FROM pg_catalog.aclexplode(
            COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
        ) AS acl
        WHERE acl.grantee = 0
          AND acl.privilege_type = 'CONNECT'
    ) AS public_connect
FROM pg_catalog.pg_database AS d
ORDER BY d.datname;
-- Required: attestor_effective_connect is true for postgres and false for
-- every other database; public_connect is false for every non-target database.

SELECT parent.rolname AS granted_role
FROM pg_catalog.pg_auth_members AS m
JOIN pg_catalog.pg_roles AS parent ON parent.oid = m.roleid
JOIN pg_catalog.pg_roles AS member ON member.oid = m.member
WHERE member.rolname = 'tia_production_attestor';
```

Function ACL and effective access:

```sql
SELECT
    p.proacl::text AS control_function_acl,
    EXISTS (
        SELECT 1
        FROM pg_catalog.aclexplode(
            COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
        ) AS acl
        WHERE acl.grantee = 0
          AND acl.privilege_type = 'EXECUTE'
    ) AS public_execute,
    pg_catalog.has_function_privilege(
        'tia_production_attestor',
        'pg_catalog.pg_control_system()',
        'EXECUTE'
    ) AS direct_control_function_execute,
    pg_catalog.has_function_privilege(
        'tia_production_attestor',
        'attestation_private.cluster_system_identifier()',
        'EXECUTE'
    ) AS wrapper_execute,
    pg_catalog.pg_has_role(
        'tia_production_attestor', 'pg_monitor', 'MEMBER'
    ) AS pg_monitor_member,
    pg_catalog.pg_has_role(
        'tia_production_attestor', 'pg_monitor', 'SET'
    ) AS can_set_pg_monitor,
    pg_catalog.pg_has_role(
        'tia_production_attestor', 'postgres', 'SET'
    ) AS can_set_postgres
FROM pg_catalog.pg_proc AS p
WHERE p.oid = 'pg_catalog.pg_control_system()'::regprocedure;
```

Wrapper definition and ACL:

```sql
SELECT
    p.prosecdef,
    pg_catalog.pg_get_userbyid(p.proowner) AS function_owner,
    owner.rolsuper AS owner_is_superuser,
    p.proconfig,
    p.proacl::text AS wrapper_acl,
    EXISTS (
        SELECT 1
        FROM pg_catalog.aclexplode(
            COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
        ) AS acl
        WHERE acl.grantee = 0
          AND acl.privilege_type = 'EXECUTE'
    ) AS wrapper_public_execute,
    p.pronargs,
    p.prosrc
FROM pg_catalog.pg_proc AS p
JOIN pg_catalog.pg_roles AS owner ON owner.oid = p.proowner
WHERE p.oid =
    'attestation_private.cluster_system_identifier()'::regprocedure;
```

ACL snapshot for any role-initiated GRANT/REVOKE verification:

```sql
SELECT
    c.relacl::text AS alembic_version_acl,
    control.proacl::text AS control_function_acl,
    wrapper.proacl::text AS fingerprint_wrapper_acl
FROM pg_catalog.pg_class AS c
CROSS JOIN pg_catalog.pg_proc AS control
CROSS JOIN pg_catalog.pg_proc AS wrapper
WHERE c.oid = 'public.alembic_version'::regclass
  AND control.oid = 'pg_catalog.pg_control_system()'::regprocedure
  AND wrapper.oid =
      'attestation_private.cluster_system_identifier()'::regprocedure;
```

Retain this output before and after the attestor's GRANT/REVOKE attempts. The ACL
values and effective privileges must remain unchanged.

Required results:

- `rolcanlogin = true` after password setup; all other elevated role attributes
  are false, and `rolconfig` includes `default_transaction_read_only=on`.
- The membership query returns zero rows.
- Effective CONNECT is true for postgres only and false for every non-target
  database, including non-connectable template databases.
- PUBLIC CONNECT is absent from every non-target database.
- The saved pre-change effective CONNECT matrix is unchanged for every existing
  role that was approved to retain access.
- Direct `pg_control_system()` EXECUTE and its PUBLIC EXECUTE ACL are false.
- Wrapper EXECUTE is true for the attestor and `wrapper_public_execute` is false.
- `pg_monitor` membership and SET access are false.
- `postgres` SET access is false.
- The wrapper is SECURITY DEFINER, owned by the authorized `postgres` role,
  has no arguments, and fixes `search_path` to `pg_catalog`.
- The role owns no tables or sequences and has no CREATE on either schema.
- Verify ownership with the following read-only query:

  ```sql
  SELECT n.nspname, c.relname, c.relkind
  FROM pg_catalog.pg_class AS c
  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
  WHERE c.relowner = 'tia_production_attestor'::regrole
    AND n.nspname NOT LIKE 'pg_%'
    AND n.nspname <> 'information_schema';
  ```

  It must return zero rows.
- `default_transaction_read_only` is `on` for new role sessions. This is defense
  in depth only: a role can change this session default, so underlying object and
  function privileges must still deny writes independently.

### Real database connection verification

Use the same disposable or owner-approved verification environment and a secure
PGPASSFILE (never place the password in shell history or command arguments).

1. Connect as tia_production_attestor to postgres. It must succeed and
   SELECT current_database() must return postgres.
2. Attempt a real connection as tia_production_attestor to every non-target
   database with datallowconn = true. Every attempt must fail at connection
   startup with permission denied for database / missing CONNECT privilege.
3. For non-target databases with datallowconn = false, verify
   has_database_privilege is false and confirm the connection attempt is rejected
   because the database does not allow connections.
4. For every approved application role/database pair in the saved regrant
   manifest, connect using that role's real credential and verify
   SELECT current_database() returns the requested database.
5. Do not proceed to GitHub Environment/Secret configuration if any expected
   connection result differs. Preserve the exact failure output and use the
   saved rollback manifests.

Then connect as `tia_production_attestor` and verify:

- `SELECT attestation_private.cluster_system_identifier()` succeeds.
- `SELECT version_num FROM public.alembic_version` succeeds.
- `SELECT pg_is_in_recovery()` succeeds.
- The current-session `pg_stat_ssl` row is readable.
- Direct `SELECT pg_catalog.pg_control_system()` fails with insufficient privilege.
- CREATE, ALTER, DROP, TRUNCATE, INSERT, UPDATE, DELETE, sequence writes,
  CREATE ROLE, and `SET ROLE postgres` fail with insufficient privilege.
- A non-owner's GRANT/REVOKE statements may either be denied or return warnings
  without changing ACLs, depending on the privilege and grant option held. In
  particular, `GRANT EXECUTE` on `pg_control_system()` must be denied. For all
  attempts, verify before/after `pg_class.relacl` and `pg_proc.proacl` snapshots
  are identical and effective privileges did not change; a successful command
  response alone is not evidence that a grant or revoke took effect.

If direct access still succeeds, or wrapper access fails, STOP. Do not grant
`pg_monitor` or any wider role.

## GitHub configuration — separate owner action

After the database role and effective privileges are independently verified,
the Project Owner may configure GitHub Environment `production-attestation` and
add the connection URI as `PRODUCTION_DB_READONLY_URL`.

Use the approved Tia Production Session Pooler endpoint and the dedicated role.
The URI must use the SQLAlchemy driver form:

```text
postgresql+psycopg://...
```

Do not copy the migration URI. Verify the target fingerprint through the separately
authorized process. Do not run the attestation workflow until the Project Owner
explicitly authorizes that separate operation.

## Rollback — administrator only

Rollback is a separate approved database change. First remove
`PRODUCTION_DB_READONLY_URL` from the GitHub Environment and ensure no attestation
dispatch is running. Then, in the same Tia Production database:

```sql
BEGIN;

REVOKE EXECUTE ON FUNCTION
    attestation_private.cluster_system_identifier()
    FROM tia_production_attestor;

DROP FUNCTION IF EXISTS
    attestation_private.cluster_system_identifier();

DROP SCHEMA IF EXISTS attestation_private;

REVOKE CONNECT ON DATABASE postgres FROM tia_production_attestor;
REVOKE USAGE ON SCHEMA public FROM tia_production_attestor;

-- Populate these temporary manifests from the saved SQL outputs before running
-- this rollback. Do not execute with guessed or incomplete rows.
CREATE TEMP TABLE rollback_public_connect (
    database_name name PRIMARY KEY
) ON COMMIT DROP;

CREATE TEMP TABLE rollback_connect_regrants (
    database_name name NOT NULL,
    role_name name NOT NULL,
    PRIMARY KEY (database_name, role_name)
) ON COMMIT DROP;

-- INSERT only the database_name rows from the saved public_connect_before output:
-- INSERT INTO pg_temp.rollback_public_connect VALUES ('approved_non_target_db');
-- INSERT only the exact rows from the saved connect_regrants_added output:
-- INSERT INTO pg_temp.rollback_connect_regrants VALUES ('approved_non_target_db', 'approved_role');

DO $restore_connect_acl$
DECLARE
    v_grant record;
    v_database record;
BEGIN
    FOR v_grant IN
        SELECT database_name, role_name
        FROM pg_temp.rollback_connect_regrants
        ORDER BY database_name, role_name
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE CONNECT ON DATABASE %I FROM %I',
            v_grant.database_name,
            v_grant.role_name
        );
    END LOOP;

    FOR v_database IN
        SELECT database_name
        FROM pg_temp.rollback_public_connect
        ORDER BY database_name
    LOOP
        EXECUTE pg_catalog.format(
            'GRANT CONNECT ON DATABASE %I TO PUBLIC',
            v_database.database_name
        );
    END LOOP;
END
$restore_connect_acl$;

-- Compare expanded ACL rows, grantors, grant options, and the effective CONNECT
-- matrix with the saved pre-change outputs before committing the rollback.
-- Never blanket-GRANT PUBLIC or revoke an application role's pre-existing grant.
REVOKE SELECT ON TABLE public.alembic_version FROM tia_production_attestor;

DROP ROLE tia_production_attestor;

-- The preflight required the original default ACL: proacl IS NULL, with
-- non-grantable PUBLIC EXECUTE. This restores that effective PUBLIC permission.
GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO PUBLIC;

COMMIT;
```

The rollback restores the saved effective database CONNECT ACLs: remove only
the explicit application-role grants added by this repair, then restore PUBLIC
CONNECT only on databases where the pre-change snapshot showed it. The preflight
requires the original PUBLIC grantor to be the database owner and non-grantable,
so the DBA must perform restoration as that same owner. Compare the expanded ACL
rows, grantors, grant options, and effective CONNECT matrix with the retained
pre-change snapshots. PostgreSQL may store a semantically restored default ACL
as an explicit ACL rather than returning datacl to NULL; never update
pg_catalog.pg_database directly to force a catalog representation.

The function rollback likewise restores the original effective EXECUTE permission
for PUBLIC. PostgreSQL may store the restored grant as an explicit ACL instead of
returning proacl to NULL; compare the expanded ACL with the retained snapshot.

If the pre-change ACL did not meet the runbook's default-ACL precondition, this
generic rollback is not valid. Stop and use the exact ACL snapshot to prepare a
separately reviewed restoration plan. Do not use `DROP OWNED` or alter the
migration role.
