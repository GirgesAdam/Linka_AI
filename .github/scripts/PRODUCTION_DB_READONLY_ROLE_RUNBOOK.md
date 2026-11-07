# Tia Production Read-only Attestation Role Runbook

Status: REVIEW ONLY - DO NOT EXECUTE WITHOUT AN EXPLICIT PRODUCTION CHANGE APPROVAL.

This runbook is for the Project Owner / database administrator. It does not create a
role, alter a database, configure a GitHub Environment, or run an attestation.

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
- Allowed to execute one narrowly scoped SECURITY DEFINER function that returns
  the cluster system identifier. This avoids granting broad `pg_monitor` access
  merely to call `pg_control_system()`.

## Preconditions

1. Confirm this is the single Tia Production database, not a staging or test target.
2. Confirm the active PostgreSQL version and current Alembic revision.
3. Confirm the administrator session is connected to the intended database.
4. Take and verify the normal database backup / recovery point required by the
   clinic's change process.
5. Review this SQL and the resulting grants before execution.

## SQL - administrator review required

Run in the Tia Production database as the database owner / administrator.
The SQL intentionally fails if the role or private schema already exists; do not
reuse or alter an existing role without a separate review.

```sql
BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles
               WHERE rolname = 'tia_production_attestor') THEN
        RAISE EXCEPTION 'tia_production_attestor already exists; stop for review';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_namespace
               WHERE nspname = 'attestation_private') THEN
        RAISE EXCEPTION 'attestation_private schema already exists; stop for review';
    END IF;
END
$$;

CREATE ROLE tia_production_attestor
    NOLOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS
    CONNECTION LIMIT 2;

GRANT CONNECT ON DATABASE postgres TO tia_production_attestor;
GRANT USAGE ON SCHEMA public TO tia_production_attestor;
GRANT SELECT ON TABLE public.alembic_version TO tia_production_attestor;
REVOKE CREATE ON SCHEMA public FROM tia_production_attestor;
DO $$
BEGIN
    IF has_schema_privilege('tia_production_attestor', 'public', 'CREATE') THEN
        RAISE EXCEPTION 'public schema CREATE is inherited; stop for separate privilege review';
    END IF;
END
$$;

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
history:

```text
\password tia_production_attestor
ALTER ROLE tia_production_attestor LOGIN;
```

Do not paste the password or resulting connection URI into chat, logs, tickets, or
source control.

## Required privilege verification

Run as an administrator and inspect the results before creating a connection URI:

```sql
SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
       rolreplication, rolbypassrls
FROM pg_catalog.pg_roles
WHERE rolname = 'tia_production_attestor';

SELECT parent.rolname AS granted_role
FROM pg_catalog.pg_auth_members m
JOIN pg_catalog.pg_roles parent ON parent.oid = m.roleid
JOIN pg_catalog.pg_roles member ON member.oid = m.member
WHERE member.rolname = 'tia_production_attestor';

SELECT
    has_database_privilege('tia_production_attestor', 'postgres', 'CONNECT') AS can_connect,
    has_schema_privilege('tia_production_attestor', 'public', 'USAGE') AS public_usage,
    has_schema_privilege('tia_production_attestor', 'public', 'CREATE') AS public_create,
    has_table_privilege('tia_production_attestor', 'public.alembic_version', 'SELECT') AS can_read_revision,
    has_table_privilege('tia_production_attestor', 'public.alembic_version', 'INSERT') AS can_insert_revision,
    has_function_privilege(
        'tia_production_attestor',
        'attestation_private.cluster_system_identifier()',
        'EXECUTE'
    ) AS can_read_cluster_fingerprint,
    pg_has_role('tia_production_attestor', 'pg_monitor', 'MEMBER') AS pg_monitor_member,
    pg_has_role('tia_production_attestor', 'pg_monitor', 'SET') AS can_set_pg_monitor;
```

Expected: all role attributes false except LOGIN (inspect with `rolcanlogin`);
membership query returns zero rows; can_connect/public_usage/can_read_revision/
can_read_cluster_fingerprint are true; public_create/can_insert_revision/
pg_monitor_member/can_set_pg_monitor are false.

Then connect using this role and verify that:
- `SELECT attestation_private.cluster_system_identifier()` succeeds.
- `SELECT version_num FROM public.alembic_version` succeeds.
- `SELECT pg_is_in_recovery()`, current-session `pg_stat_ssl`, and the
  attestation fingerprint queries succeed.
- `CREATE TABLE`, `INSERT`, `UPDATE`, `DELETE`, `GRANT`, and `SET ROLE
  postgres` fail with insufficient privilege.

## GitHub configuration - separate owner action

After the role is independently verified, create/use GitHub Environment
`production-attestation` and add the connection URI as
`PRODUCTION_DB_READONLY_URL`. Do not copy the migration URI. Use the provider's
documented connection format for this custom role and verify it connects to the
same Tia Production database.

Do not run the attestation workflow until the Project Owner explicitly authorizes
that separate operation.

## Rollback plan - administrator only

If the role setup must be reversed, first remove the GitHub
`PRODUCTION_DB_READONLY_URL` secret, then use an administrator-reviewed change
to revoke the function/schema grants and drop the function, schema, and role.
Do not run a blanket `DROP OWNED` or alter the migration role.
