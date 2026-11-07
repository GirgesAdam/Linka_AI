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
- Limited to CONNECT on database `postgres`, USAGE on `public`, and SELECT on
  `public.alembic_version`.
- Allowed to execute one zero-argument SECURITY DEFINER function that returns only
  the cluster system identifier.
- Not granted `pg_monitor`, `pg_read_all_settings`, or any broad monitoring role.

## Root cause established on disposable PostgreSQL 17.6

The failed exact-head CI run `42377421472` showed that
`tia_attestor_ci` could execute `SELECT pg_control_system()` even though the role
had no role memberships and was not a member of `pg_monitor`.

The PostgreSQL 17.6 integration test now records the function's
`pg_catalog.pg_proc.proacl`, expands the effective ACL with
`acldefault('f', proowner)` when `proacl` is NULL, and checks
`has_function_privilege`. The cause is the function's PUBLIC EXECUTE ACL:

- `proacl IS NULL` means PostgreSQL is using the built-in default function ACL.
- The default function ACL includes EXECUTE for PUBLIC.
- `tia_attestor_ci` therefore had effective EXECUTE through PUBLIC, not through
  `pg_monitor`, a direct grant, or role inheritance.
- The narrow wrapper's own PUBLIC EXECUTE grant is separately revoked, then EXECUTE
  is granted only to the attestor role.

The repair revokes PUBLIC EXECUTE on this one function in the same transaction that
creates and grants the SECURITY DEFINER wrapper. It does not revoke or grant any
other `pg_catalog` function privilege.

## Scope and impact review — mandatory before any Production change

The ACL of a built-in function is stored in the current database's
`pg_catalog.pg_proc`. This change affects the current database only; it does not
change ACLs in other databases in the same PostgreSQL cluster.

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

Required results:

- `rolcanlogin = true` after password setup; all other elevated role attributes
  are false, and `rolconfig` includes `default_transaction_read_only=on`.
- The membership query returns zero rows.
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

Then connect as `tia_production_attestor` and verify:

- `SELECT attestation_private.cluster_system_identifier()` succeeds.
- `SELECT version_num FROM public.alembic_version` succeeds.
- `SELECT pg_is_in_recovery()` succeeds.
- The current-session `pg_stat_ssl` row is readable.
- Direct `SELECT pg_catalog.pg_control_system()` fails with insufficient privilege.
- CREATE, ALTER, DROP, TRUNCATE, INSERT, UPDATE, DELETE, sequence writes, GRANT,
  REVOKE, CREATE ROLE, and `SET ROLE postgres` fail with insufficient privilege.

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
REVOKE SELECT ON TABLE public.alembic_version FROM tia_production_attestor;

DROP ROLE tia_production_attestor;

-- The preflight required the original default ACL: proacl IS NULL, with
-- non-grantable PUBLIC EXECUTE. This restores that effective PUBLIC permission.
GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO PUBLIC;

COMMIT;
```

The rollback restores the original effective EXECUTE permission for PUBLIC and
leaves the function owner able to execute it. PostgreSQL may store the restored
grant as an explicit ACL instead of returning `proacl` to NULL; do not update
`pg_catalog.pg_proc` directly to force a catalog representation. Compare the
effective ACL with the retained pre-change snapshot.

If the pre-change ACL did not meet the runbook's default-ACL precondition, this
generic rollback is not valid. Stop and use the exact ACL snapshot to prepare a
separately reviewed restoration plan. Do not use `DROP OWNED` or alter the
migration role.
