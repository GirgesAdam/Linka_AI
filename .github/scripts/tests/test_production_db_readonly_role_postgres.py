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
                raise RuntimeError(
                    f"expected PostgreSQL 17.6, got server_version_num={version_num}"
                )
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
            admin.execute("CREATE SEQUENCE public.attestor_guard_sequence")
            admin.execute(
                sql.SQL(
                    "CREATE ROLE tia_attestor_ci LOGIN PASSWORD {} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                    "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2"
                ).format(sql.Literal(cls.password))
            )
            admin.execute(
                "ALTER ROLE tia_attestor_ci SET default_transaction_read_only = on"
            )
            admin.execute("GRANT CONNECT ON DATABASE attestation_ci TO tia_attestor_ci")
            admin.execute("GRANT USAGE ON SCHEMA public TO tia_attestor_ci")
            admin.execute("GRANT SELECT ON public.alembic_version TO tia_attestor_ci")
            admin.execute("REVOKE CREATE ON SCHEMA public FROM tia_attestor_ci")

            # Capture the catalog ACL and prove that PUBLIC, not a role membership
            # or a direct grant to the attestor, explains the original access.
            before = admin.execute(
                """
                SELECT
                    p.proacl::text,
                    p.proacl IS NULL,
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.aclexplode(
                            COALESCE(
                                p.proacl,
                                pg_catalog.acldefault('f', p.proowner)
                            )
                        ) AS acl
                        WHERE acl.grantee = 0
                          AND acl.privilege_type = 'EXECUTE'
                    ),
                    pg_catalog.has_function_privilege(%s, p.oid, 'EXECUTE'),
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.aclexplode(
                            COALESCE(
                                p.proacl,
                                pg_catalog.acldefault('f', p.proowner)
                            )
                        ) AS acl
                        WHERE acl.grantee = (
                            SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s
                        )
                          AND acl.privilege_type = 'EXECUTE'
                    )
                FROM pg_catalog.pg_proc AS p
                WHERE p.oid = 'pg_catalog.pg_control_system()'::regprocedure
                """,
                (cls.role_name, cls.role_name),
            ).fetchone()
            if before is None:
                raise RuntimeError("pg_catalog.pg_control_system() was not found")
            (
                cls.control_acl_before,
                cls.control_acl_uses_default,
                cls.public_execute_before,
                cls.attestor_effective_execute_before,
                cls.attestor_direct_execute_before,
            ) = before

            membership_count = admin.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_auth_members
                WHERE member = (
                    SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s
                )
                """,
                (cls.role_name,),
            ).fetchone()[0]
            if not cls.control_acl_uses_default:
                raise RuntimeError(
                    "expected stock PostgreSQL 17.6 default proacl; "
                    f"stored proacl={cls.control_acl_before!r}"
                )
            if not cls.public_execute_before:
                raise RuntimeError(
                    "PostgreSQL 17.6 baseline has no PUBLIC EXECUTE on pg_control_system(); "
                    f"stored proacl={cls.control_acl_before!r}"
                )
            if not cls.attestor_effective_execute_before:
                raise RuntimeError("expected the attestor to inherit EXECUTE through PUBLIC")
            if cls.attestor_direct_execute_before or membership_count != 0:
                raise RuntimeError(
                    "unexpected direct function grant or role membership in fresh CI database"
                )

            # The PUBLIC revocation and SECURITY DEFINER wrapper are atomic.
            # If wrapper creation or grants fail, the original ACL is restored
            # automatically by transaction rollback.
            with admin.transaction():
                admin.execute(
                    "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_control_system() FROM PUBLIC"
                )
                admin.execute(
                    "CREATE SCHEMA attestation_private AUTHORIZATION postgres"
                )
                admin.execute(
                    "REVOKE ALL ON SCHEMA attestation_private FROM PUBLIC"
                )
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

            after = admin.execute(
                """
                SELECT
                    p.proacl::text,
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.aclexplode(
                            COALESCE(
                                p.proacl,
                                pg_catalog.acldefault('f', p.proowner)
                            )
                        ) AS acl
                        WHERE acl.grantee = 0
                          AND acl.privilege_type = 'EXECUTE'
                    ),
                    pg_catalog.has_function_privilege(%s, p.oid, 'EXECUTE'),
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.aclexplode(
                            COALESCE(
                                p.proacl,
                                pg_catalog.acldefault('f', p.proowner)
                            )
                        ) AS acl
                        WHERE acl.grantee = (
                            SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s
                        )
                          AND acl.privilege_type = 'EXECUTE'
                    )
                FROM pg_catalog.pg_proc AS p
                WHERE p.oid = 'pg_catalog.pg_control_system()'::regprocedure
                """,
                (cls.role_name, cls.role_name),
            ).fetchone()
            if after is None:
                raise RuntimeError("could not verify pg_control_system() ACL after revocation")
            (
                cls.control_acl_after,
                cls.public_execute_after,
                cls.attestor_effective_execute_after,
                cls.attestor_direct_execute_after,
            ) = after
            if (
                cls.public_execute_after
                or cls.attestor_effective_execute_after
                or cls.attestor_direct_execute_after
            ):
                raise RuntimeError(
                    "PUBLIC revocation did not remove direct attestor execution"
                )

            wrapper = admin.execute(
                """
                SELECT
                    p.prosecdef,
                    p.proowner = 'postgres'::regrole,
                    owner.rolsuper,
                    p.proconfig,
                    pg_catalog.has_function_privilege(%s, p.oid, 'EXECUTE'),
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.aclexplode(
                            COALESCE(
                                p.proacl,
                                pg_catalog.acldefault('f', p.proowner)
                            )
                        ) AS acl
                        WHERE acl.grantee = 0
                          AND acl.privilege_type = 'EXECUTE'
                    ),
                    p.pronargs,
                    p.prosrc
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_roles AS owner ON owner.oid = p.proowner
                WHERE p.oid =
                    'attestation_private.cluster_system_identifier()'::regprocedure
                """,
                (cls.role_name,),
            ).fetchone()
            if wrapper is None:
                raise RuntimeError("SECURITY DEFINER wrapper was not created")
            (
                cls.wrapper_is_security_definer,
                cls.wrapper_owned_by_postgres,
                cls.wrapper_owner_is_superuser,
                cls.wrapper_config,
                cls.wrapper_execute_granted,
                cls.wrapper_public_execute,
                cls.wrapper_argument_count,
                cls.wrapper_source,
            ) = wrapper
            if not (
                cls.wrapper_is_security_definer
                and cls.wrapper_owned_by_postgres
                and cls.wrapper_owner_is_superuser
                and cls.wrapper_execute_granted
                and not cls.wrapper_public_execute
                and cls.wrapper_argument_count == 0
                and any("search_path=pg_catalog" in setting for setting in cls.wrapper_config or [])
                and "pg_catalog.pg_control_system()" in cls.wrapper_source
            ):
                raise RuntimeError("SECURITY DEFINER wrapper ACL/owner/search_path is unsafe")

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "admin_conninfo"):
            return
        with psycopg.connect(cls.admin_conninfo, autocommit=True) as admin:
            admin.execute(
                "REVOKE EXECUTE ON FUNCTION "
                "attestation_private.cluster_system_identifier() FROM tia_attestor_ci"
            )
            admin.execute(
                "DROP FUNCTION IF EXISTS "
                "attestation_private.cluster_system_identifier()"
            )
            admin.execute("DROP SCHEMA IF EXISTS attestation_private")
            admin.execute("REVOKE CONNECT ON DATABASE attestation_ci FROM tia_attestor_ci")
            admin.execute("REVOKE USAGE ON SCHEMA public FROM tia_attestor_ci")
            admin.execute("REVOKE SELECT ON public.alembic_version FROM tia_attestor_ci")
            admin.execute("DROP ROLE IF EXISTS tia_attestor_ci")
            admin.execute("DROP SEQUENCE IF EXISTS public.attestor_guard_sequence")
            admin.execute("DROP TABLE IF EXISTS public.alembic_version")
            # Restore effective PUBLIC access before the disposable CI service
            # is destroyed. The stored ACL becomes explicit; each run requires a
            # freshly initialized PostgreSQL 17.6 database.
            if getattr(cls, "public_execute_before", False):
                admin.execute(
                    "GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO PUBLIC"
                )

    def test_public_acl_was_the_only_source_of_direct_execute(self):
        self.assertTrue(self.public_execute_before)
        self.assertTrue(self.control_acl_uses_default)
        self.assertIsNone(self.control_acl_before)
        self.assertTrue(self.attestor_effective_execute_before)
        self.assertFalse(self.attestor_direct_execute_before)
        self.assertFalse(self.public_execute_after)
        self.assertFalse(self.attestor_effective_execute_after)
        self.assertFalse(self.attestor_direct_execute_after)

    def test_role_cannot_change_table_or_function_acls(self):
        snapshot_query = """
            SELECT
                (
                    SELECT relacl::text
                    FROM pg_catalog.pg_class
                    WHERE oid = 'public.alembic_version'::regclass
                ),
                (
                    SELECT proacl::text
                    FROM pg_catalog.pg_proc
                    WHERE oid = 'pg_catalog.pg_control_system()'::regprocedure
                ),
                (
                    SELECT proacl::text
                    FROM pg_catalog.pg_proc
                    WHERE oid =
                        'attestation_private.cluster_system_identifier()'::regprocedure
                )
        """
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            before = admin.execute(snapshot_query).fetchone()

        # PostgreSQL may report GRANT/REVOKE by a non-owner as successful no-ops
        # with warnings. Verify the actual ACL catalog state, not just SQLSTATE.
        with psycopg.connect(self.role_conninfo, autocommit=True) as conn:
            conn.execute("SET default_transaction_read_only = off")
            conn.execute(
                "GRANT INSERT ON TABLE public.alembic_version TO PUBLIC"
            )
            conn.execute(
                "REVOKE SELECT ON TABLE public.alembic_version FROM PUBLIC"
            )
            conn.execute(
                "GRANT EXECUTE ON FUNCTION "
                "attestation_private.cluster_system_identifier() TO PUBLIC"
            )
            conn.execute(
                "REVOKE EXECUTE ON FUNCTION "
                "attestation_private.cluster_system_identifier() FROM PUBLIC"
            )

        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            after = admin.execute(snapshot_query).fetchone()
            self.assertEqual(after, before)
            privileges = admin.execute(
                """
                SELECT
                    pg_catalog.has_function_privilege(
                        'tia_attestor_ci',
                        'pg_catalog.pg_control_system()',
                        'EXECUTE'
                    ),
                    pg_catalog.has_function_privilege(
                        'tia_attestor_ci',
                        'attestation_private.cluster_system_identifier()',
                        'EXECUTE'
                    ),
                    pg_catalog.has_table_privilege(
                        'tia_attestor_ci', 'public.alembic_version', 'INSERT'
                    )
                """
            ).fetchone()
            self.assertEqual(privileges, (False, True, False))

    def test_role_reads_only_the_wrapper_surface_and_cannot_escalate(self):
        with psycopg.connect(self.role_conninfo, autocommit=True) as conn:
            self.assertEqual(
                conn.execute("SHOW default_transaction_read_only").fetchone()[0],
                "on",
            )
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
                SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
                       rolreplication, rolbypassrls, rolinherit
                FROM pg_catalog.pg_roles
                WHERE rolname = current_user
                """
            ).fetchone()
            self.assertEqual(attrs, (True, False, False, False, False, False, False))

            self.assertFalse(
                conn.execute(
                    "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
                ).fetchone()[0]
            )
            self.assertFalse(
                conn.execute(
                    "SELECT has_schema_privilege("
                    "current_user, 'attestation_private', 'CREATE')"
                ).fetchone()[0]
            )
            self.assertFalse(
                conn.execute(
                    "SELECT has_table_privilege(current_user, "
                    "'public.alembic_version', 'INSERT')"
                ).fetchone()[0]
            )
            self.assertFalse(
                conn.execute(
                    "SELECT has_function_privilege("
                    "current_user, 'pg_catalog.pg_control_system()', 'EXECUTE')"
                ).fetchone()[0]
            )
            self.assertTrue(
                conn.execute(
                    "SELECT has_function_privilege("
                    "current_user, "
                    "'attestation_private.cluster_system_identifier()', 'EXECUTE')"
                ).fetchone()[0]
            )

            memberships = conn.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_auth_members AS m
                JOIN pg_catalog.pg_roles AS member ON member.oid = m.member
                WHERE member.rolname = current_user
                """
            ).fetchone()[0]
            self.assertEqual(memberships, 0)
            self.assertFalse(
                conn.execute(
                    "SELECT pg_has_role(current_user, 'pg_monitor', 'MEMBER')"
                ).fetchone()[0]
            )
            self.assertFalse(
                conn.execute(
                    "SELECT pg_has_role(current_user, 'pg_monitor', 'SET')"
                ).fetchone()[0]
            )
            self.assertFalse(
                conn.execute(
                    "SELECT pg_has_role(current_user, 'postgres', 'SET')"
                ).fetchone()[0]
            )
            owned_objects = conn.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE c.relowner = current_user::regrole
                  AND n.nspname NOT LIKE 'pg_%'
                  AND n.nspname <> 'information_schema'
                """
            ).fetchone()[0]
            self.assertEqual(owned_objects, 0)
            conn.execute("ROLLBACK")

        denied_statements = (
            "SELECT pg_catalog.pg_control_system()",
            "GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO PUBLIC",
            "CREATE TABLE public.attestor_must_not_create(id integer)",
            "CREATE TABLE attestation_private.attestor_must_not_create(id integer)",
            (
                "CREATE FUNCTION public.attestor_must_not_create() RETURNS integer "
                "LANGUAGE sql AS 'SELECT 1'"
            ),
            "ALTER TABLE public.alembic_version ADD COLUMN unauthorized integer",
            (
                "ALTER FUNCTION attestation_private.cluster_system_identifier() "
                "OWNER TO tia_attestor_ci"
            ),
            "DROP FUNCTION attestation_private.cluster_system_identifier()",
            "DROP TABLE public.alembic_version",
            "TRUNCATE TABLE public.alembic_version",
            "INSERT INTO public.alembic_version(version_num) VALUES ('unauthorized')",
            "UPDATE public.alembic_version SET version_num = 'unauthorized'",
            "DELETE FROM public.alembic_version",
            "SELECT nextval('public.attestor_guard_sequence')",
            "SELECT setval('public.attestor_guard_sequence', 2)",
            "ALTER SEQUENCE public.attestor_guard_sequence INCREMENT BY 2",
            "DROP SEQUENCE public.attestor_guard_sequence",
            "CREATE ROLE tia_attestor_escalated NOLOGIN",
            "ALTER ROLE postgres SUPERUSER",
            "SET ROLE postgres",
            "SET SESSION AUTHORIZATION postgres",
        )
        for statement in denied_statements:
            with (
                self.subTest(statement=statement),
                psycopg.connect(self.role_conninfo, autocommit=True) as conn,
                self.assertRaises(psycopg.errors.InsufficientPrivilege),
            ):
                # The GUC is a default only, not a privilege boundary. Turn it off
                # so each negative statement proves the underlying ACL/ownership denial.
                conn.execute("SET default_transaction_read_only = off")
                conn.execute(statement)


if __name__ == "__main__":
    unittest.main()
