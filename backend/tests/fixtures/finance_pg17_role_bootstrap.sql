-- CI-only disposable PostgreSQL roles. Never use this fixture in Production.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'CREATE ROLE anon NOLOGIN';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated') THEN
    EXECUTE 'CREATE ROLE authenticated NOLOGIN';
  END IF;
END
$$;

CREATE ROLE tia_migrator
  LOGIN PASSWORD 'ci_only_migrator_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;

CREATE ROLE tia_backend_runtime
  LOGIN PASSWORD 'ci_only_runtime_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS NOINHERIT;

CREATE ROLE vault_ci_backend LOGIN PASSWORD 'ci_vault_backend_password' BYPASSRLS NOSUPERUSER;
