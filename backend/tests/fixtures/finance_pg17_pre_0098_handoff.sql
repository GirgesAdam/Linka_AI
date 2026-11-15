-- CI-only ownership/privilege handoff on a disposable database at revision 0097.
DO $$
DECLARE
  current_revision text;
  role_ok boolean;
BEGIN
  IF current_user <> 'postgres' OR session_user <> 'postgres' THEN
    RAISE EXCEPTION 'pre-0098 handoff must run as disposable postgres bootstrap identity';
  END IF;

  SELECT version_num INTO current_revision
  FROM public.alembic_version;
  IF current_revision IS DISTINCT FROM '0097_member_page_permissions' THEN
    RAISE EXCEPTION 'expected revision 0097 before handoff, found %', current_revision;
  END IF;

  SELECT (
    rolcanlogin
    AND NOT rolsuper
    AND NOT rolcreatedb
    AND NOT rolcreaterole
    AND NOT rolreplication
    AND NOT rolbypassrls
    AND NOT rolinherit
  ) INTO role_ok
  FROM pg_catalog.pg_roles
  WHERE rolname = 'tia_migrator';
  IF role_ok IS DISTINCT FROM TRUE THEN
    RAISE EXCEPTION 'tia_migrator attributes do not match the approved migrator identity';
  END IF;

  SELECT (
    rolcanlogin
    AND NOT rolsuper
    AND NOT rolcreatedb
    AND NOT rolcreaterole
    AND NOT rolreplication
    AND rolbypassrls
    AND NOT rolinherit
  ) INTO role_ok
  FROM pg_catalog.pg_roles
  WHERE rolname = 'tia_backend_runtime';
  IF role_ok IS DISTINCT FROM TRUE THEN
    RAISE EXCEPTION 'tia_backend_runtime attributes do not match the approved runtime identity';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_auth_members AS m
    JOIN pg_catalog.pg_roles AS member ON member.oid = m.member
    WHERE member.rolname IN ('tia_migrator', 'tia_backend_runtime')
  ) THEN
    RAISE EXCEPTION 'CI runtime/migrator roles must not inherit any role memberships';
  END IF;

  IF to_regclass('public.doctor_branches') IS NULL
     OR to_regclass('public.clinic_laser_devices') IS NULL THEN
    RAISE EXCEPTION '0098 referenced legacy tables are missing';
  END IF;
END
$$;

GRANT USAGE, CREATE ON SCHEMA public TO tia_migrator;
ALTER TABLE public.alembic_version OWNER TO tia_migrator;
GRANT REFERENCES (workspace_id, doctor_id, branch_id)
  ON TABLE public.doctor_branches TO tia_migrator;
GRANT REFERENCES (workspace_id, device_key)
  ON TABLE public.clinic_laser_devices TO tia_migrator;
REVOKE ALL ON TABLE public.alembic_version FROM tia_backend_runtime;

DO $$
BEGIN
  EXECUTE format(
    'GRANT CONNECT ON DATABASE %I TO tia_migrator, tia_backend_runtime',
    current_database()
  );
END
$$;
