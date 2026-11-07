from __future__ import annotations

import os
import secrets
import unittest

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


class ReadOnlyRolePostgres176Tests(unittest.TestCase):
    """Exercise the proposed least-privilege role against disposable PostgreSQL 17.6."""

    @classmethod
    def setUpClass(cls):
        raw_url = os.environ.get("ATTESTATION_TEST_DATABASE_URL")
        if not raw_url:
            raise unittest.SkipTest("ATTESTATION_TEST_DATABASE_URL is not configured")
        url = make_url(raw_url)
        if (
            url.host not in {"127.0.0.1", "localhost"}
            or url.database != "attestation_ci"
            or url.username != "postgres"
        ):
            raise RuntimeError("integration test refuses a non-local disposable database")
        cls.admin_conninfo = raw_url
        cls.role_name = "tia_attestor_ci"
        cls.password = secrets.token_urlsafe(32)
        cls.role_conninfo = url.set(
            username=cls.role_name, password=cls.password
        ).render_as_string(hide_password=False)

        with psycopg.connect(raw_url, autocommit=True) as admin:
            version_num = int(admin.execute("SHOW server_version_num").fetchone()[0])
            if version_num != 170006:
                raise RuntimeError(f"expected PostgreSQL 17.6, got server_version_num={version_num}")
            if admin.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (cls.role_name,)
            ).fetchone():
                raise RuntimeError("test role already exists; refusing to reuse it")

            admin.execute(
                "CREATE TABLE public.alembic_version "
                "(version_num varchar(32) PRIMARY KEY)"
            )
            admin.execute(
                "INSERT INTO public.alembic_version(version_num) VALUES (%s)",
                ("0096_patient_customer_id",),
            )
            admin.execute(
                sql.SQL(
                    "CREATE ROLE tia_attestor_ci LOGIN PASSWORD {} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                    "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2"
                ).format(sql.Literal(cls.password))
            )
            admin.execute("GRANT CONNECT ON DATABASE attestation_ci TO tia_attestor_ci")
            admin.execute("GRANT USAGE ON SCHEMA public TO tia_attestor_ci")
            admin.execute("GRANT SELECT ON public.alembic_version TO tia_attestor_ci")
            admin.execute("REVOKE CREATE ON SCHEMA public FROM tia_attestor_ci")
            admin.execute(
                "CREATE SCHEMA attestation_private AUTHORIZATION postgres"
            )
            admin.execute("REVOKE ALL ON SCHEMA attestation_private FROM PUBLIC")
            admin.execute(
                """
                CREATE FUNCTION attestation_private.cluster_system_identifier()
                RETURNS text
                LANGUAGE sql
                SECURITY DEFINER
                SET search_path = pg_catalog
                AS $function$
                    SELECT system_identifier::text
                    FROM pg_catalog.pg_control_system();
                $function$
                """
            )
            admin.execute(
                "ALTER FUNCTION attestation_private.cluster_system_identifier() "
                "OWNER TO postgres"
            )
            admin.execute(
                "REVOKE ALL ON FUNCTION "
                "attestation_private.cluster_system_identifier() FROM PUBLIC"
            )
            admin.execute(
                "GRANT USAGE ON SCHEMA attestation_private TO tia_attestor_ci"
            )
            admin.execute(
                "GRANT EXECUTE ON FUNCTION "
                "attestation_private.cluster_system_identifier() TO tia_attestor_ci"
            )

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "admin_conninfo"):
            return
        with psycopg.connect(cls.admin_conninfo, autocommit=True) as admin:
            admin.execute(
                "DROP FUNCTION IF EXISTS "
                "attestation_private.cluster_system_identifier()"
            )
            admin.execute("DROP SCHEMA IF EXISTS attestation_private")
            admin.execute("REVOKE CONNECT ON DATABASE attestation_ci FROM tia_attestor_ci")
            admin.execute("REVOKE USAGE ON SCHEMA public FROM tia_attestor_ci")
            admin.execute("REVOKE SELECT ON public.alembic_version FROM tia_attestor_ci")
            admin.execute("DROP ROLE IF EXISTS tia_attestor_ci")
            admin.execute("DROP TABLE IF EXISTS public.alembic_version")

    def test_role_can_read_only_attestation_surface_and_cannot_escalate(self):
        with psycopg.connect(self.role_conninfo, autocommit=True) as conn:
            conn.execute("BEGIN READ ONLY")
            self.assertEqual(
                conn.execute("SELECT current_database()").fetchone()[0],
                "attestation_ci",
            )
            self.assertFalse(conn.execute("SELECT pg_is_in_recovery()").fetchone()[0])
            cluster_identifier = conn.execute(
                "SELECT attestation_private.cluster_system_identifier()"
            ).fetchone()[0]
            self.assertRegex(cluster_identifier, r"^[0-9]{10,30}$")
            self.assertEqual(
                conn.execute("SELECT version_num FROM public.alembic_version").fetchone()[0],
                "0096_patient_customer_id",
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
                ).fetchone()
            )
            attrs = conn.execute(
                """
                SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication,
                       rolbypassrls, rolinherit
                FROM pg_roles WHERE rolname = current_user
                """
            ).fetchone()
            self.assertEqual(attrs, (False, False, False, False, False, False))
            self.assertFalse(
                conn.execute(
                    "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
                ).fetchone()[0]
            )
            self.assertFalse(
                conn.execute(
                    "SELECT has_table_privilege(current_user, "
                    "'public.alembic_version', 'INSERT')"
                ).fetchone()[0]
            )
            memberships = conn.execute(
                """
                SELECT count(*)
                FROM pg_auth_members m
                JOIN pg_roles member ON member.oid = m.member
                WHERE member.rolname = current_user
                """
            ).fetchone()[0]
            self.assertEqual(memberships, 0)
            self.assertFalse(
                conn.execute(
                    "SELECT pg_has_role(current_user, 'pg_monitor', 'SET')"
                ).fetchone()[0]
            )
            conn.execute("ROLLBACK")

        denied_statements = (
            "SELECT pg_control_system()",
            "CREATE TABLE public.attestor_must_not_create(id integer)",
            "INSERT INTO public.alembic_version(version_num) VALUES ('unauthorized')",
            "UPDATE public.alembic_version SET version_num = 'unauthorized'",
            "DELETE FROM public.alembic_version",
            "SET ROLE postgres",
        )
        for statement in denied_statements:
            with (
                self.subTest(statement=statement),
                psycopg.connect(self.role_conninfo, autocommit=True) as conn,
                self.assertRaises(psycopg.errors.InsufficientPrivilege),
            ):
                conn.execute(statement)


if __name__ == "__main__":
    unittest.main()
