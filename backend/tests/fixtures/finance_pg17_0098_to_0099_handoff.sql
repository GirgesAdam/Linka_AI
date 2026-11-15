-- CI-only legacy ownership handoff before Migration 0099 on a disposable database.
DO $$
DECLARE
  current_revision text;
  expenses_owner text;
BEGIN
  IF current_user <> 'postgres' OR session_user <> 'postgres' THEN
    RAISE EXCEPTION '0098-to-0099 handoff must run as disposable postgres bootstrap identity';
  END IF;

  SELECT version_num INTO current_revision
  FROM public.alembic_version;
  IF current_revision IS DISTINCT FROM '0098_doctor_laser_weekday_exclusions' THEN
    RAISE EXCEPTION 'expected revision 0098 before handoff, found %', current_revision;
  END IF;

  SELECT pg_get_userbyid(c.relowner) INTO expenses_owner
  FROM pg_catalog.pg_class AS c
  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relname = 'expenses';
  IF expenses_owner IS DISTINCT FROM 'postgres' THEN
    RAISE EXCEPTION 'expected legacy expenses owner postgres before handoff, found %', expenses_owner;
  END IF;
END
$$;

ALTER TABLE public.expenses OWNER TO tia_migrator;

GRANT TRIGGER ON TABLE
  public.payment_transactions,
  public.payment_allocations,
  public.appointments
TO tia_migrator;

GRANT REFERENCES (id) ON TABLE public.workspaces TO tia_migrator;
GRANT REFERENCES (workspace_id, id) ON TABLE public.branches TO tia_migrator;
GRANT REFERENCES (id) ON TABLE public.users TO tia_migrator;
GRANT REFERENCES (workspace_id, id) ON TABLE public.payment_transactions TO tia_migrator;
GRANT REFERENCES (workspace_id, user_id) ON TABLE public.workspace_members TO tia_migrator;
