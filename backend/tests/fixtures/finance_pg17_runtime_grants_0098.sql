-- Minimal CI-only Finance privileges for the actual tia_backend_runtime identity.
GRANT USAGE ON SCHEMA public TO tia_backend_runtime;
GRANT SELECT ON TABLE public.workspaces, public.expenses, public.activity_events
  TO tia_backend_runtime;
GRANT INSERT ON TABLE public.activity_events TO tia_backend_runtime;
GRANT UPDATE, DELETE ON TABLE public.expenses TO tia_backend_runtime;
REVOKE ALL ON TABLE public.alembic_version FROM tia_backend_runtime;
