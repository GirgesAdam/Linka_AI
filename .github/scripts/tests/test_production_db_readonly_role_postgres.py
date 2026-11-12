from __future__ import annotations

import os
import secrets
import unittest
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

runtime_probe = SimpleNamespace(
    **run_path(
        str(
            Path(__file__).resolve().parents[1]
            / "production_db_readonly_attestation.py"
        )
    )
)


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
                ("0097_member_page_permissions",),
            )
            admin.execute(
                """
                CREATE TABLE public.workspace_members (
                    id uuid PRIMARY KEY,
                    workspace_id uuid NOT NULL,
                    user_id uuid NOT NULL,
                    role varchar(50) NOT NULL,
                    is_active boolean NOT NULL DEFAULT true,
                    page_permissions jsonb NULL,
                    permissions_version integer NOT NULL DEFAULT 0,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
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
            admin.execute("DROP TABLE IF EXISTS public.workspace_members")
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

    def test_attestor_verifies_migration_0097_and_permission_schema_from_catalog(self):
        with psycopg.connect(self.role_conninfo, autocommit=True) as conn:
            conn.execute("BEGIN READ ONLY")
            self.assertEqual(
                conn.execute("SHOW transaction_read_only").fetchone()[0],
                "on",
            )
            revisions = conn.execute(
                "SELECT version_num FROM public.alembic_version ORDER BY version_num"
            ).fetchall()
            schema_verified, observed = runtime_probe.verify_member_permissions_schema(conn)

            self.assertEqual(revisions, [("0097_member_page_permissions",)])
            self.assertTrue(schema_verified)
            self.assertEqual(
                observed,
                {
                    "page_permissions": {
                        "data_type": "jsonb",
                        "not_null": False,
                        "default": None,
                    },
                    "permissions_version": {
                        "data_type": "integer",
                        "not_null": True,
                        "default": "0",
                    },
                },
            )
            self.assertFalse(
                conn.execute(
                    "SELECT has_table_privilege(current_user, 'public.workspace_members', 'SELECT')"
                ).fetchone()[0]
            )
            conn.execute("ROLLBACK")

    def test_attestor_schema_probe_detects_real_postgres_schema_drift(self):
        with psycopg.connect(self.admin_conninfo) as admin:
            admin.execute(
                "ALTER TABLE public.workspace_members "
                "ALTER COLUMN permissions_version TYPE bigint"
            )
            schema_verified, observed = runtime_probe.verify_member_permissions_schema(admin)
            admin.rollback()

        self.assertFalse(schema_verified)
        self.assertEqual(observed["permissions_version"]["data_type"], "bigint")

    def test_wrapper_body_drift_is_rejected_before_execution_and_restored(self):
        function_name = "attestation_private.cluster_system_identifier()"
        catalog_metadata = """
            SELECT proowner, prosecdef, pronargs, proconfig, proacl,
                   prolang, prorettype, proretset, prokind, provolatile,
                   proisstrict, proleakproof, proparallel
            FROM pg_catalog.pg_proc
            WHERE oid = %s::regprocedure
        """

        with psycopg.connect(self.role_conninfo, autocommit=True) as attestor:
            attestor.execute("BEGIN READ ONLY")
            runtime_probe.verify_runtime_identity(
                attestor, expected_role=self.role_name
            )
            attestor.execute("ROLLBACK")

        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            approved_metadata = admin.execute(
                catalog_metadata, (function_name,)
            ).fetchone()
            try:
                # CREATE OR REPLACE retains owner/ACL, language, return type, and
                # search_path; changing only SQL body must still fail closed.
                admin.execute(
                    """
                    CREATE OR REPLACE FUNCTION attestation_private.cluster_system_identifier()
                    RETURNS text
                    LANGUAGE sql
                    SECURITY DEFINER
                    SET search_path = pg_catalog
                    AS $function$
                        SELECT system_identifier::text
                        FROM pg_catalog.pg_control_system()
                        WHERE true;
                    $function$
                    """
                )
                self.assertEqual(
                    admin.execute(
                        catalog_metadata, (function_name,)
                    ).fetchone(),
                    approved_metadata,
                )

                with psycopg.connect(self.role_conninfo, autocommit=True) as attestor:
                    issued_queries = []

                    def recorded_execute(statement):
                        issued_queries.append(statement)
                        return attestor.execute(statement)

                    attestor.execute("BEGIN READ ONLY")
                    try:
                        with self.assertRaises(
                            runtime_probe.AttestationFailure
                        ) as failed:
                            runtime_probe.verify_runtime_identity(
                                SimpleNamespace(execute=recorded_execute),
                                expected_role=self.role_name,
                            )
                        self.assertEqual(
                            failed.exception.category,
                            "runtime_wrapper_definition_unverified",
                        )
                        self.assertFalse(
                            any(
                                "SELECT attestation_private.cluster_system_identifier()"
                                in query
                                for query in issued_queries
                            )
                        )
                    finally:
                        attestor.execute("ROLLBACK")
            finally:
                # Restore original definition even if a regression assertion fails.
                admin.execute(
                    """
                    CREATE OR REPLACE FUNCTION attestation_private.cluster_system_identifier()
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
                self.assertEqual(
                    admin.execute(
                        catalog_metadata, (function_name,)
                    ).fetchone(),
                    approved_metadata,
                )

        with psycopg.connect(self.role_conninfo, autocommit=True) as attestor:
            attestor.execute("BEGIN READ ONLY")
            runtime_probe.verify_runtime_identity(
                attestor, expected_role=self.role_name
            )
            attestor.execute("ROLLBACK")

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
                "0097_member_page_permissions",
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





class SecurityDefinerIsolationPostgres176Tests(unittest.TestCase):
    """Prove SECURITY DEFINER isolation and application ACL preservation on PG 17.6."""

    ATTESTOR_ROLE = "tia_attestor_function_ci"
    APP_DIRECT_ROLE = "sd_app_direct_ci"
    APP_GROUP_ROLE = "sd_app_group_ci"
    APP_INHERITED_ROLE = "sd_app_inherited_ci"
    APP_PUBLIC_ROLE = "sd_app_public_consumer_ci"
    FUTURE_OWNER_ROLE = "sd_future_owner_ci"
    UNCONFIGURED_OWNER_ROLE = "sd_unconfigured_owner_ci"
    DELEGATED_GRANTOR_ROLE = "sd_delegated_grantor_ci"
    RUNTIME_CREATEROLE_ROLE = "sd_runtime_createrole_ci"
    RUNTIME_CREATEDB_ROLE = "sd_runtime_createdb_ci"
    RUNTIME_BYPASSRLS_ROLE = "sd_runtime_bypassrls_ci"
    RUNTIME_INHERIT_ROLE = "sd_runtime_inherit_ci"

    OPS_SCHEMA = "sd_ops_private_ci"
    WRAPPER_SCHEMA = "attestation_private"
    FUTURE_SCHEMA = "sd_future_functions_ci"
    UNCONFIGURED_SCHEMA = "sd_unconfigured_functions_ci"
    WRAPPER_NAME = "cluster_system_identifier"

    @classmethod
    def _function_identifier(cls, row):
        return sql.Identifier(row["schema_name"], row["function_name"]) + sql.SQL("()")

    @classmethod
    def _inventory(cls, conn):
        rows = conn.execute(
            """
            WITH RECURSIVE
            attestor AS (
                SELECT oid
                FROM pg_catalog.pg_roles
                WHERE rolname = %s
            ),
            inherited_roles(role_oid) AS (
                SELECT m.roleid
                FROM pg_catalog.pg_auth_members AS m
                CROSS JOIN attestor AS a
                WHERE m.member = a.oid
                  AND m.inherit_option
                UNION
                SELECT m.roleid
                FROM pg_catalog.pg_auth_members AS m
                JOIN inherited_roles AS inherited
                  ON m.member = inherited.role_oid
                WHERE m.inherit_option
            )
            SELECT
                p.oid,
                p.oid::regprocedure::text AS function_identity,
                n.nspname AS schema_name,
                p.proname AS function_name,
                pg_catalog.pg_get_function_identity_arguments(p.oid)
                    AS identity_arguments,
                owner.oid AS owner_oid,
                owner.rolname AS owner_name,
                p.prosecdef AS security_definer,
                CASE
                    WHEN n.nspname IN ('pg_catalog', 'information_schema')
                      OR pg_catalog.left(n.nspname, 8) = 'pg_toast'
                      OR pg_catalog.left(n.nspname, 8) = 'pg_temp_'
                      OR pg_catalog.left(n.nspname, 13) = 'pg_toast_temp_'
                    THEN 'SYSTEM'
                    WHEN EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_depend AS extension_member
                        WHERE extension_member.classid = 'pg_catalog.pg_proc'::regclass
                          AND extension_member.objid = p.oid
                          AND extension_member.refclassid = 'pg_catalog.pg_extension'::regclass
                          AND extension_member.deptype = 'e'
                    )
                    THEN 'EXTENSION'
                    ELSE 'APPLICATION'
                END AS schema_class,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_extension AS e
                    WHERE e.extnamespace = n.oid
                ) AS extension_owned_schema,
                pg_catalog.has_schema_privilege(a.oid, n.oid, 'USAGE')
                    AS attestor_schema_usage,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            n.nspacl,
                            pg_catalog.acldefault('n', n.nspowner)
                        )
                    ) AS schema_acl
                    WHERE schema_acl.grantee = a.oid
                      AND schema_acl.privilege_type = 'USAGE'
                ) AS direct_schema_usage,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            n.nspacl,
                            pg_catalog.acldefault('n', n.nspowner)
                        )
                    ) AS schema_acl
                    WHERE schema_acl.grantee = 0
                      AND schema_acl.privilege_type = 'USAGE'
                ) AS public_schema_usage,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            n.nspacl,
                            pg_catalog.acldefault('n', n.nspowner)
                        )
                    ) AS schema_acl
                    JOIN inherited_roles AS inherited
                      ON inherited.role_oid = schema_acl.grantee
                    WHERE schema_acl.privilege_type = 'USAGE'
                ) AS inherited_schema_usage,
                pg_catalog.has_function_privilege(a.oid, p.oid, 'EXECUTE')
                    AS attestor_effective_execute,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            p.proacl,
                            pg_catalog.acldefault('f', p.proowner)
                        )
                    ) AS acl
                    WHERE acl.grantee = a.oid
                      AND acl.privilege_type = 'EXECUTE'
                ) AS direct_execute,
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
                ) AS public_execute,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            p.proacl,
                            pg_catalog.acldefault('f', p.proowner)
                        )
                    ) AS acl
                    JOIN inherited_roles AS inherited
                      ON inherited.role_oid = acl.grantee
                    WHERE acl.privilege_type = 'EXECUTE'
                ) AS inherited_execute,
                p.proconfig,
                COALESCE(
                    (
                        SELECT pg_catalog.substr(cfg.setting, 13)
                        FROM pg_catalog.unnest(
                            COALESCE(p.proconfig, ARRAY[]::text[])
                        ) AS cfg(setting)
                        WHERE pg_catalog.left(cfg.setting, 12) = 'search_path='
                        LIMIT 1
                    ),
                    '<unset>'
                ) AS function_search_path,
                COALESCE(
                    (
                        SELECT pg_catalog.jsonb_agg(
                            pg_catalog.jsonb_build_object(
                                'grantee',
                                CASE
                                    WHEN acl.grantee = 0 THEN 'PUBLIC'
                                    ELSE pg_catalog.pg_get_userbyid(acl.grantee)
                                END,
                                'grantor',
                                pg_catalog.pg_get_userbyid(acl.grantor),
                                'grantable',
                                acl.is_grantable
                            )
                            ORDER BY acl.grantee
                        )
                        FROM pg_catalog.aclexplode(
                            COALESCE(
                                p.proacl,
                                pg_catalog.acldefault('f', p.proowner)
                            )
                        ) AS acl
                        WHERE acl.privilege_type = 'EXECUTE'
                    ),
                    '[]'::jsonb
                ) AS execute_acl_evidence
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
            JOIN pg_catalog.pg_roles AS owner ON owner.oid = p.proowner
            CROSS JOIN attestor AS a
            WHERE p.prosecdef
            ORDER BY schema_class, n.nspname, p.proname,
                     pg_catalog.pg_get_function_identity_arguments(p.oid)
            """,
            (cls.ATTESTOR_ROLE,),
        ).fetchall()
        columns = (
            "oid", "function_identity", "schema_name", "function_name",
            "identity_arguments", "owner_oid", "owner_name", "security_definer",
            "schema_class", "extension_owned_schema", "attestor_schema_usage",
            "direct_schema_usage", "public_schema_usage",
            "inherited_schema_usage", "attestor_effective_execute",
            "direct_execute", "public_execute", "inherited_execute",
            "proconfig", "function_search_path", "execute_acl_evidence",
        )
        return [dict(zip(columns, row)) for row in rows]

    @classmethod
    def _acl_entries(cls, conn, function_oid):
        return conn.execute(
            """
            SELECT acl.grantee, acl.grantor, acl.is_grantable
            FROM pg_catalog.pg_proc AS p
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    p.proacl,
                    pg_catalog.acldefault('f', p.proowner)
                )
            ) AS acl
            WHERE p.oid = %s
              AND acl.privilege_type = 'EXECUTE'
            ORDER BY acl.grantee
            """,
            (function_oid,),
        ).fetchall()

    @classmethod
    def _state(cls, conn, function_oid):
        return conn.execute(
            """
            SELECT
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
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            p.proacl,
                            pg_catalog.acldefault('f', p.proowner)
                        )
                    ) AS acl
                    WHERE acl.grantee = (
                        SELECT oid
                        FROM pg_catalog.pg_roles
                        WHERE rolname = %s
                    )
                      AND acl.privilege_type = 'EXECUTE'
                ),
                pg_catalog.has_function_privilege(%s, p.oid, 'EXECUTE')
            FROM pg_catalog.pg_proc AS p
            WHERE p.oid = %s
            """,
            (cls.ATTESTOR_ROLE, cls.ATTESTOR_ROLE, function_oid),
        ).fetchone()

    @classmethod
    def _apply_function_policy(cls, reviewed_oids, approved_regrants):
        public_revoked = []
        direct_revoked = []
        regrants_added = []

        with (
            psycopg.connect(cls.admin_conninfo, autocommit=True) as admin,
            admin.transaction(),
        ):
                role = admin.execute(
                    """
                    SELECT oid, rolsuper
                    FROM pg_catalog.pg_roles
                    WHERE rolname = %s
                    """,
                    (cls.ATTESTOR_ROLE,),
                ).fetchone()
                if role is None or role[1]:
                    raise RuntimeError("attestor missing or has SUPERUSER")
                attestor_oid = role[0]

                if admin.execute(
                    """
                    SELECT count(*)
                    FROM pg_catalog.pg_auth_members
                    WHERE member = %s
                    """,
                    (attestor_oid,),
                ).fetchone()[0]:
                    raise RuntimeError("attestor has an unexpected role membership")

                inventory = cls._inventory(admin)
                wrapper_rows = [
                    row for row in inventory
                    if row["schema_name"] == cls.WRAPPER_SCHEMA
                    and row["function_name"] == cls.WRAPPER_NAME
                    and row["identity_arguments"] == ""
                ]
                if len(wrapper_rows) != 1:
                    raise RuntimeError("approved attestation wrapper is missing or ambiguous")
                wrapper = wrapper_rows[0]
                if (
                    not wrapper["security_definer"]
                    or wrapper["owner_name"] != "postgres"
                    or not wrapper["attestor_schema_usage"]
                    or not wrapper["attestor_effective_execute"]
                    or wrapper["public_execute"]
                    or wrapper["function_search_path"] != "pg_catalog"
                ):
                    raise RuntimeError("approved wrapper ACL/owner/search_path is unsafe")

                exposed_system_or_extension = [
                    row for row in inventory
                    if row["schema_class"] != "APPLICATION"
                    and row["attestor_schema_usage"]
                    and row["attestor_effective_execute"]
                ]
                if exposed_system_or_extension:
                    raise RuntimeError(
                        "attestor can execute a SECURITY DEFINER function in a "
                        "system/extension schema; DBA review required"
                    )

                candidates = [
                    row for row in inventory
                    if row["schema_class"] == "APPLICATION"
                    and row["attestor_schema_usage"]
                    and row["attestor_effective_execute"]
                    and row["oid"] != wrapper["oid"]
                ]
                candidate_oids = {row["oid"] for row in candidates}
                if candidate_oids - set(reviewed_oids):
                    raise RuntimeError(
                        "SECURITY DEFINER function consumer inventory is incomplete"
                    )

                for row in candidates:
                    if row["owner_name"] != "postgres":
                        raise RuntimeError(
                            "SECURITY DEFINER function owner is not the reviewed "
                            "current DBA; use an owner-specific approved change"
                        )
                    entries = cls._acl_entries(admin, row["oid"])
                    for grantee, grantor, grantable in entries:
                        if grantor != row["owner_oid"]:
                            raise RuntimeError(
                                "non-owner EXECUTE grantor requires separate review"
                            )
                        if grantable and grantee != row["owner_oid"]:
                            raise RuntimeError(
                                "non-owner EXECUTE grant option requires separate review"
                            )
                        if grantee in (0, attestor_oid) and (
                            grantor != row["owner_oid"] or grantable
                        ):
                            raise RuntimeError(
                                "PUBLIC/attestor EXECUTE grantor or grant option is unapproved"
                            )
                    if not (
                        row["direct_execute"]
                        or row["public_execute"]
                        or row["inherited_execute"]
                        or row["owner_oid"] == attestor_oid
                    ):
                        raise RuntimeError("unclassified effective EXECUTE path")

                for function_oid, role_name in approved_regrants:
                    if function_oid not in candidate_oids:
                        raise RuntimeError("application regrant references an unreviewed function")
                    app_role = admin.execute(
                        """
                        SELECT oid, rolsuper
                        FROM pg_catalog.pg_roles
                        WHERE rolname = %s
                        """,
                        (role_name,),
                    ).fetchone()
                    if app_role is None or app_role[1] or role_name == cls.ATTESTOR_ROLE:
                        raise RuntimeError("application regrant role is missing or privileged")
                    if not admin.execute(
                        "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
                        (role_name, function_oid),
                    ).fetchone()[0]:
                        raise RuntimeError(
                            "approved application role did not have pre-change EXECUTE"
                        )

                for row in candidates:
                    function_id = cls._function_identifier(row)
                    if row["public_execute"]:
                        admin.execute(
                            sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM PUBLIC").format(
                                function_id
                            )
                        )
                        public_revoked.append(row["oid"])
                    if row["direct_execute"]:
                        admin.execute(
                            sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM {}").format(
                                function_id, sql.Identifier(cls.ATTESTOR_ROLE)
                            )
                        )
                        direct_revoked.append(row["oid"])

                for function_oid, role_name in approved_regrants:
                    if not admin.execute(
                        "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
                        (role_name, function_oid),
                    ).fetchone()[0]:
                        row = next(item for item in candidates if item["oid"] == function_oid)
                        admin.execute(
                            sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
                                cls._function_identifier(row),
                                sql.Identifier(role_name),
                            )
                        )
                        regrants_added.append((function_oid, role_name))

                for row in candidates:
                    if admin.execute(
                        "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
                        (cls.ATTESTOR_ROLE, row["oid"]),
                    ).fetchone()[0]:
                        raise RuntimeError(
                            f"unapproved SECURITY DEFINER EXECUTE remains: "
                            f"{row['function_identity']}"
                        )

                for function_oid, role_name in approved_regrants:
                    if not admin.execute(
                        "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
                        (role_name, function_oid),
                    ).fetchone()[0]:
                        raise RuntimeError(
                            f"approved application EXECUTE was not preserved: {role_name}"
                        )

        return public_revoked, direct_revoked, regrants_added

    @classmethod
    def _create_probe_function(cls, admin, schema_name, function_name, table_schema):
        admin.execute(
            sql.SQL(
                """
                CREATE FUNCTION {}()
                RETURNS text
                LANGUAGE sql
                SECURITY DEFINER
                SET search_path = pg_catalog
                AS $function$
                    SELECT current_user::text || ':' || secret_value
                    FROM {}.sd_probe_secret_ci
                    LIMIT 1;
                $function$
                """
            ).format(
                sql.Identifier(schema_name, function_name),
                sql.Identifier(table_schema),
            )
        )

    @classmethod
    def _atomic_create_future_function(
        cls,
        schema_name,
        owner_role,
        function_name,
        *,
        revoke_public,
        approved_roles,
        expected_default_public,
    ):
        """Model the approved deployment gate: create, harden, verify, then commit."""
        function_identifier = sql.Identifier(schema_name, function_name)
        function_identity = f"{schema_name}.{function_name}()"
        with (
            psycopg.connect(cls.admin_conninfo, autocommit=True) as admin,
            admin.transaction(),
        ):
            admin.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner_role))
            )
            admin.execute(
                sql.SQL(
                    """
                    CREATE FUNCTION {}()
                    RETURNS text
                    LANGUAGE sql
                    SECURITY DEFINER
                    SET search_path = pg_catalog
                    AS $function$ SELECT current_user::text $function$
                    """
                ).format(function_identifier)
            )
            default_public = admin.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            p.proacl,
                            pg_catalog.acldefault('f', p.proowner)
                        )
                    ) AS acl
                    WHERE acl.grantee = 0
                      AND acl.privilege_type = 'EXECUTE'
                )
                FROM pg_catalog.pg_proc AS p
                WHERE p.oid = pg_catalog.to_regprocedure(%s)::oid
                """,
                (function_identity,),
            ).fetchone()[0]
            if default_public is not expected_default_public:
                raise RuntimeError(
                    "unexpected owner-specific default PUBLIC EXECUTE state"
                )

            # A second session must not see or execute transactional DDL.
            with psycopg.connect(
                cls.connection_urls[cls.ATTESTOR_ROLE],
                autocommit=True,
            ) as observer:
                visible_oid = observer.execute(
                    "SELECT pg_catalog.to_regprocedure(%s)::oid",
                    (function_identity,),
                ).fetchone()[0]
                if visible_oid is not None:
                    raise RuntimeError(
                        "uncommitted SECURITY DEFINER function became visible"
                    )
                try:
                    observer.execute(
                        sql.SQL("SELECT {}()").format(function_identifier)
                    )
                except psycopg.errors.UndefinedFunction:
                    pass
                else:
                    raise RuntimeError(
                        "attestor executed an uncommitted SECURITY DEFINER function"
                    )

            if revoke_public:
                admin.execute(
                    sql.SQL(
                        "REVOKE EXECUTE ON FUNCTION {}() FROM PUBLIC"
                    ).format(function_identifier)
                )
            for role_name in approved_roles:
                admin.execute(
                    sql.SQL("GRANT EXECUTE ON FUNCTION {}() TO {}").format(
                        function_identifier,
                        sql.Identifier(role_name),
                    )
                )
            admin.execute("RESET ROLE")

            state = admin.execute(
                """
                SELECT
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
                    pg_catalog.has_function_privilege(%s, p.oid, 'EXECUTE'),
                    pg_catalog.has_function_privilege(%s, p.oid, 'EXECUTE'),
                    pg_catalog.has_function_privilege(%s, p.oid, 'EXECUTE'),
                    pg_catalog.pg_get_userbyid(p.proowner)
                FROM pg_catalog.pg_proc AS p
                WHERE p.oid = pg_catalog.to_regprocedure(%s)::oid
                """,
                (
                    cls.ATTESTOR_ROLE,
                    cls.APP_DIRECT_ROLE,
                    cls.APP_INHERITED_ROLE,
                    cls.APP_PUBLIC_ROLE,
                    function_identity,
                ),
            ).fetchone()
            if state is None:
                raise RuntimeError("new SECURITY DEFINER function disappeared")
            (
                public_execute,
                attestor_execute,
                app_direct,
                app_inherited,
                app_explicit,
                actual_owner,
            ) = state
            if public_execute or attestor_execute:
                raise RuntimeError(
                    "pre-commit SECURITY DEFINER PUBLIC/attestor EXECUTE guard failed"
                )
            if not (app_direct and app_inherited and app_explicit):
                raise RuntimeError(
                    "pre-commit approved application EXECUTE verification failed"
                )
            if actual_owner != owner_role:
                raise RuntimeError("future SECURITY DEFINER function owner mismatch")
            function_oid = admin.execute(
                "SELECT pg_catalog.to_regprocedure(%s)::oid",
                (function_identity,),
            ).fetchone()[0]
        return function_oid

    @classmethod
    def _cleanup(cls):
        if not getattr(cls, "resources_created", False):
            return
        with psycopg.connect(cls.admin_conninfo, autocommit=True) as admin:
            for schema_name in (
                cls.WRAPPER_SCHEMA,
                cls.OPS_SCHEMA,
                cls.FUTURE_SCHEMA,
                cls.UNCONFIGURED_SCHEMA,
            ):
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema_name)
                    )
                )
            admin.execute(
                "DROP FUNCTION IF EXISTS pg_catalog.sd_system_exposure_probe_ci()"
            )
            admin.execute("DROP EXTENSION IF EXISTS hstore")
            admin.execute(
                "DROP FUNCTION IF EXISTS public.sd_runtime_unexpected_definer_ci()"
            )
            if getattr(cls, "control_public_execute_before", False):
                admin.execute(
                    "GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO PUBLIC"
                )
            for function_name in (
                "sd_public_definer_probe_ci",
                "sd_direct_definer_probe_ci",
                "sd_public_only_definer_probe_ci",
                "sd_unreviewed_definer_probe_ci",
                "sd_rollback_definer_probe_ci",
                "sd_extension_member_probe_ci",
            ):
                admin.execute(
                    sql.SQL("DROP FUNCTION IF EXISTS public.{}()").format(
                        sql.Identifier(function_name)
                    )
                )
            admin.execute("DROP TABLE IF EXISTS public.sd_probe_secret_ci")
            role_names = (
                cls.ATTESTOR_ROLE,
                cls.APP_DIRECT_ROLE,
                cls.APP_GROUP_ROLE,
                cls.APP_INHERITED_ROLE,
                cls.APP_PUBLIC_ROLE,
                cls.UNCONFIGURED_OWNER_ROLE,
                cls.RUNTIME_CREATEROLE_ROLE,
                cls.RUNTIME_CREATEDB_ROLE,
                cls.RUNTIME_BYPASSRLS_ROLE,
                cls.RUNTIME_INHERIT_ROLE,
            )
            existing = {
                row[0]
                for row in admin.execute(
                    "SELECT rolname FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)",
                    (list(role_names),),
                ).fetchall()
            }
            if existing:
                admin.execute(
                    sql.SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(
                        sql.SQL(", ").join(
                            sql.Identifier(name)
                            for name in role_names
                            if name in existing
                        )
                    )
                )
            for role_name in (
                cls.ATTESTOR_ROLE,
                cls.APP_DIRECT_ROLE,
                cls.APP_INHERITED_ROLE,
                cls.APP_GROUP_ROLE,
                cls.APP_PUBLIC_ROLE,
                cls.UNCONFIGURED_OWNER_ROLE,
                cls.DELEGATED_GRANTOR_ROLE,
                cls.RUNTIME_CREATEROLE_ROLE,
                cls.RUNTIME_CREATEDB_ROLE,
                cls.RUNTIME_BYPASSRLS_ROLE,
                cls.RUNTIME_INHERIT_ROLE,
            ):
                admin.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name))
                )
            existing_owner = admin.execute(
                "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s",
                (cls.FUTURE_OWNER_ROLE,),
            ).fetchone()
            if existing_owner:
                admin.execute(
                    sql.SQL("DROP OWNED BY {}").format(
                        sql.Identifier(cls.FUTURE_OWNER_ROLE)
                    )
                )
                admin.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(
                        sql.Identifier(cls.FUTURE_OWNER_ROLE)
                    )
                )
        cls.resources_created = False

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
        cls.resources_created = False
        cls.passwords = {
            cls.ATTESTOR_ROLE: secrets.token_urlsafe(32),
            cls.APP_DIRECT_ROLE: secrets.token_urlsafe(32),
            cls.APP_INHERITED_ROLE: secrets.token_urlsafe(32),
            cls.APP_PUBLIC_ROLE: secrets.token_urlsafe(32),
            cls.RUNTIME_CREATEROLE_ROLE: secrets.token_urlsafe(32),
            cls.RUNTIME_CREATEDB_ROLE: secrets.token_urlsafe(32),
            cls.RUNTIME_BYPASSRLS_ROLE: secrets.token_urlsafe(32),
            cls.RUNTIME_INHERIT_ROLE: secrets.token_urlsafe(32),
        }
        cls.connection_urls = {
            role_name: url.set(
                username=role_name, password=password
            ).render_as_string(hide_password=False)
            for role_name, password in cls.passwords.items()
        }

        with psycopg.connect(raw_url, autocommit=True) as admin:
            if int(admin.execute("SHOW server_version_num").fetchone()[0]) != 170006:
                raise RuntimeError("expected PostgreSQL 17.6")
            cls.control_public_execute_before = admin.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_catalog.aclexplode(
                        COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                    ) AS acl
                    WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                )
                FROM pg_catalog.pg_proc AS p
                WHERE p.oid = 'pg_catalog.pg_control_system()'::regprocedure
                """
            ).fetchone()[0]
            role_names = (
                cls.ATTESTOR_ROLE, cls.APP_DIRECT_ROLE, cls.APP_GROUP_ROLE,
                cls.APP_INHERITED_ROLE, cls.APP_PUBLIC_ROLE,
                cls.FUTURE_OWNER_ROLE, cls.UNCONFIGURED_OWNER_ROLE,
                cls.DELEGATED_GRANTOR_ROLE,
                cls.RUNTIME_CREATEROLE_ROLE, cls.RUNTIME_CREATEDB_ROLE,
                cls.RUNTIME_BYPASSRLS_ROLE, cls.RUNTIME_INHERIT_ROLE,
            )
            for role_name in role_names:
                if admin.execute(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s",
                    (role_name,),
                ).fetchone():
                    raise RuntimeError(f"test role already exists: {role_name}")
            schema_names = (
                cls.WRAPPER_SCHEMA, cls.OPS_SCHEMA,
                cls.FUTURE_SCHEMA, cls.UNCONFIGURED_SCHEMA,
            )
            for schema_name in schema_names:
                if admin.execute(
                    "SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s",
                    (schema_name,),
                ).fetchone():
                    raise RuntimeError(f"test schema already exists: {schema_name}")
            for function_name in (
                "sd_public_definer_probe_ci",
                "sd_direct_definer_probe_ci",
                "sd_public_only_definer_probe_ci",
                "sd_unreviewed_definer_probe_ci",
                "sd_rollback_definer_probe_ci",
                "sd_extension_member_probe_ci",
            ):
                if admin.execute(
                    """
                    SELECT 1
                    FROM pg_catalog.pg_proc AS p
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public' AND p.proname = %s
                    """,
                    (function_name,),
                ).fetchone():
                    raise RuntimeError(f"test function already exists: {function_name}")
            if admin.execute(
                "SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'hstore'"
            ).fetchone():
                raise RuntimeError("hstore extension already exists in disposable database")
            if admin.execute(
                """
                SELECT 1
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'pg_catalog'
                  AND p.proname = 'sd_system_exposure_probe_ci'
                """
            ).fetchone():
                raise RuntimeError("system exposure test function already exists")
            if admin.execute(
                "SELECT 1 FROM pg_catalog.pg_class WHERE oid = to_regclass('public.sd_probe_secret_ci')"
            ).fetchone():
                raise RuntimeError("test secret table already exists")
            if admin.execute(
                "SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'hstore'"
            ).fetchone():
                raise RuntimeError("hstore extension already exists in disposable database")

            cls.resources_created = True
            try:
                with admin.transaction():
                    admin.execute(
                        "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_control_system() FROM PUBLIC"
                    )
                    noinherit_roles = {
                        cls.ATTESTOR_ROLE,
                        cls.RUNTIME_CREATEROLE_ROLE,
                        cls.RUNTIME_CREATEDB_ROLE,
                        cls.RUNTIME_BYPASSRLS_ROLE,
                    }
                    for role_name, password in cls.passwords.items():
                        inheritance = (
                            "NOINHERIT" if role_name in noinherit_roles else "INHERIT"
                        )
                        admin.execute(
                            sql.SQL(
                                "CREATE ROLE {} LOGIN PASSWORD {} "
                                "NOSUPERUSER NOCREATEDB NOCREATEROLE {} "
                                "NOREPLICATION NOBYPASSRLS"
                            ).format(
                                sql.Identifier(role_name),
                                sql.Literal(password),
                                sql.SQL(inheritance),
                            )
                        )
                    for role_name, option in (
                        (cls.RUNTIME_CREATEROLE_ROLE, "CREATEROLE"),
                        (cls.RUNTIME_CREATEDB_ROLE, "CREATEDB"),
                        (cls.RUNTIME_BYPASSRLS_ROLE, "BYPASSRLS"),
                    ):
                        admin.execute(
                            sql.SQL("ALTER ROLE {} WITH {}").format(
                                sql.Identifier(role_name), sql.SQL(option)
                            )
                        )
                        admin.execute(
                            sql.SQL(
                                "ALTER ROLE {} SET default_transaction_read_only = on"
                            ).format(sql.Identifier(role_name))
                        )
                    for role_name in (
                        cls.ATTESTOR_ROLE,
                        cls.RUNTIME_INHERIT_ROLE,
                    ):
                        admin.execute(
                            sql.SQL(
                                "ALTER ROLE {} SET default_transaction_read_only = on"
                            ).format(sql.Identifier(role_name))
                        )
                    admin.execute(
                        sql.SQL(
                            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS"
                        ).format(sql.Identifier(cls.APP_GROUP_ROLE))
                    )
                    for role_name in (
                        cls.FUTURE_OWNER_ROLE,
                        cls.UNCONFIGURED_OWNER_ROLE,
                        cls.DELEGATED_GRANTOR_ROLE,
                    ):
                        admin.execute(
                            sql.SQL(
                                "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB "
                                "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                            ).format(sql.Identifier(role_name))
                        )

                    admin.execute(
                        sql.SQL("CREATE SCHEMA {} AUTHORIZATION postgres").format(
                            sql.Identifier(cls.WRAPPER_SCHEMA)
                        )
                    )
                    admin.execute(
                        sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(
                            sql.Identifier(cls.WRAPPER_SCHEMA)
                        )
                    )
                    admin.execute("CREATE EXTENSION hstore WITH SCHEMA public")
                    admin.execute(
                        sql.SQL("CREATE SCHEMA {} AUTHORIZATION postgres").format(
                            sql.Identifier(cls.OPS_SCHEMA)
                        )
                    )
                    admin.execute(
                        sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                            sql.Identifier(cls.FUTURE_SCHEMA),
                            sql.Identifier(cls.FUTURE_OWNER_ROLE),
                        )
                    )
                    admin.execute(
                        sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                            sql.Identifier(cls.UNCONFIGURED_SCHEMA),
                            sql.Identifier(cls.UNCONFIGURED_OWNER_ROLE),
                        )
                    )

                    admin.execute(
                        sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(
                            sql.Identifier(cls.ATTESTOR_ROLE)
                        )
                    )
                    for schema_name in ("public", cls.OPS_SCHEMA):
                        admin.execute(
                            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}, {}, {}, {}").format(
                                sql.Identifier(schema_name),
                                sql.Identifier(cls.ATTESTOR_ROLE),
                                sql.Identifier(cls.APP_DIRECT_ROLE),
                                sql.Identifier(cls.APP_GROUP_ROLE),
                                sql.Identifier(cls.APP_INHERITED_ROLE),
                                sql.Identifier(cls.APP_PUBLIC_ROLE),
                            )
                        )
                    admin.execute(
                        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                            sql.Identifier(cls.WRAPPER_SCHEMA),
                            sql.Identifier(cls.ATTESTOR_ROLE),
                        )
                    )
                    for schema_name in (cls.FUTURE_SCHEMA, cls.UNCONFIGURED_SCHEMA):
                        admin.execute(
                            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                                sql.Identifier(schema_name),
                                sql.Identifier(cls.ATTESTOR_ROLE),
                            )
                        )

                    for schema_name in (cls.FUTURE_SCHEMA, cls.UNCONFIGURED_SCHEMA):
                        admin.execute(
                            sql.SQL(
                                "GRANT USAGE ON SCHEMA {} TO {}, {}, {}, {}"
                            ).format(
                                sql.Identifier(schema_name),
                                sql.Identifier(cls.APP_DIRECT_ROLE),
                                sql.Identifier(cls.APP_GROUP_ROLE),
                                sql.Identifier(cls.APP_INHERITED_ROLE),
                                sql.Identifier(cls.APP_PUBLIC_ROLE),
                            )
                        )

                    admin.execute(
                        "CREATE TABLE public.sd_probe_secret_ci(secret_value text NOT NULL)"
                    )
                    admin.execute(
                        "INSERT INTO public.sd_probe_secret_ci VALUES ('PUBLIC_PROBE_SECRET')"
                    )
                    admin.execute(
                        "REVOKE ALL ON public.sd_probe_secret_ci FROM PUBLIC"
                    )
                    admin.execute(
                        sql.SQL(
                            "CREATE TABLE {}.sd_probe_secret_ci(secret_value text NOT NULL)"
                        ).format(sql.Identifier(cls.OPS_SCHEMA))
                    )
                    admin.execute(
                        sql.SQL(
                            "INSERT INTO {}.sd_probe_secret_ci VALUES ('OPS_PROBE_SECRET')"
                        ).format(sql.Identifier(cls.OPS_SCHEMA))
                    )
                    admin.execute(
                        sql.SQL("REVOKE ALL ON {}.sd_probe_secret_ci FROM PUBLIC").format(
                            sql.Identifier(cls.OPS_SCHEMA)
                        )
                    )

                    cls._create_probe_function(
                        admin, "public", "sd_public_definer_probe_ci", "public"
                    )
                    cls._create_probe_function(
                        admin, cls.OPS_SCHEMA, "sd_ops_definer_probe_ci", cls.OPS_SCHEMA
                    )
                    cls._create_probe_function(
                        admin, "public", "sd_direct_definer_probe_ci", "public"
                    )
                    cls._create_probe_function(
                        admin, "public", "sd_public_only_definer_probe_ci", "public"
                    )
                    cls._create_probe_function(
                        admin, "public", "sd_unreviewed_definer_probe_ci", "public"
                    )
                    cls._create_probe_function(
                        admin, "public", "sd_rollback_definer_probe_ci", "public"
                    )
                    admin.execute(
                        """
                        CREATE FUNCTION public.sd_extension_member_probe_ci()
                        RETURNS text LANGUAGE sql SECURITY DEFINER
                        SET search_path = pg_catalog
                        AS $function$ SELECT current_user::text $function$
                        """
                    )
                    admin.execute(
                        "REVOKE EXECUTE ON FUNCTION public.sd_extension_member_probe_ci() "
                        "FROM PUBLIC"
                    )
                    admin.execute(
                        "ALTER EXTENSION hstore ADD FUNCTION "
                        "public.sd_extension_member_probe_ci()"
                    )

                    admin.execute(
                        "GRANT EXECUTE ON FUNCTION public.sd_public_definer_probe_ci() "
                        "TO sd_app_direct_ci"
                    )
                    admin.execute(
                        sql.SQL(
                            "GRANT EXECUTE ON FUNCTION {}.sd_ops_definer_probe_ci() TO {}"
                        ).format(
                            sql.Identifier(cls.OPS_SCHEMA),
                            sql.Identifier(cls.APP_GROUP_ROLE),
                        )
                    )
                    admin.execute(
                        sql.SQL("GRANT {} TO {}").format(
                            sql.Identifier(cls.APP_GROUP_ROLE),
                            sql.Identifier(cls.APP_INHERITED_ROLE),
                        )
                    )

                    admin.execute(
                        "REVOKE EXECUTE ON FUNCTION public.sd_direct_definer_probe_ci() "
                        "FROM PUBLIC"
                    )
                    admin.execute(
                        "GRANT EXECUTE ON FUNCTION public.sd_direct_definer_probe_ci() "
                        "TO tia_attestor_function_ci"
                    )
                    admin.execute(
                        "REVOKE EXECUTE ON FUNCTION public.sd_rollback_definer_probe_ci() "
                        "FROM PUBLIC"
                    )
                    admin.execute(
                        "GRANT EXECUTE ON FUNCTION public.sd_rollback_definer_probe_ci() "
                        "TO tia_attestor_function_ci"
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
                        "REVOKE ALL ON FUNCTION "
                        "attestation_private.cluster_system_identifier() FROM PUBLIC"
                    )
                    admin.execute(
                        "GRANT EXECUTE ON FUNCTION "
                        "attestation_private.cluster_system_identifier() "
                        "TO tia_attestor_function_ci"
                    )
                    cls.candidate_rows_before = [
                        row for row in cls._inventory(admin)
                        if row["schema_class"] == "APPLICATION"
                        and row["attestor_schema_usage"]
                        and row["attestor_effective_execute"]
                        and not (
                            row["schema_name"] == cls.WRAPPER_SCHEMA
                            and row["function_name"] == cls.WRAPPER_NAME
                        )
                    ]
                    if len(cls.candidate_rows_before) != 6:
                        raise RuntimeError(
                            "expected six exposed application SECURITY DEFINER fixtures; "
                            f"got {len(cls.candidate_rows_before)}"
                        )
                    cls.reviewed_oids = {row["oid"] for row in cls.candidate_rows_before}
                    cls.approved_regrants = [
                        (
                            next(row["oid"] for row in cls.candidate_rows_before
                                 if row["function_name"] == "sd_public_definer_probe_ci"),
                            cls.APP_DIRECT_ROLE,
                        ),
                        (
                            next(row["oid"] for row in cls.candidate_rows_before
                                 if row["function_name"] == "sd_ops_definer_probe_ci"),
                            cls.APP_GROUP_ROLE,
                        ),
                        (
                            next(row["oid"] for row in cls.candidate_rows_before
                                 if row["function_name"] == "sd_public_only_definer_probe_ci"),
                            cls.APP_PUBLIC_ROLE,
                        ),
                    ]
                    cls.pre_policy_states = {
                        row["oid"]: cls._state(admin, row["oid"])
                        for row in cls.candidate_rows_before
                    }

                with psycopg.connect(
                    cls.connection_urls[cls.ATTESTOR_ROLE],
                    autocommit=True,
                ) as exposed_attestor:
                    cls.public_exposure_before = exposed_attestor.execute(
                        "SELECT public.sd_public_definer_probe_ci()"
                    ).fetchone()[0]
                    cls.ops_exposure_before = exposed_attestor.execute(
                        sql.SQL("SELECT {}.sd_ops_definer_probe_ci()").format(
                            sql.Identifier(cls.OPS_SCHEMA)
                        )
                    ).fetchone()[0]
                    cls.direct_exposure_before = exposed_attestor.execute(
                        "SELECT public.sd_direct_definer_probe_ci()"
                    ).fetchone()[0]
                if cls.public_exposure_before != "postgres:PUBLIC_PROBE_SECRET":
                    raise RuntimeError("PUBLIC SECURITY DEFINER fixture did not run as owner")
                if cls.ops_exposure_before != "postgres:OPS_PROBE_SECRET":
                    raise RuntimeError("non-public schema SECURITY DEFINER fixture did not run as owner")
                if cls.direct_exposure_before != "postgres:PUBLIC_PROBE_SECRET":
                    raise RuntimeError("direct EXECUTE fixture did not run as owner")

                try:
                    cls._apply_function_policy(set(), cls.approved_regrants)
                except RuntimeError as exc:
                    cls.fail_closed_error = str(exc)
                else:
                    raise RuntimeError("incomplete function consumer inventory did not fail closed")

                with psycopg.connect(raw_url, autocommit=True) as verify:
                    for row in cls.candidate_rows_before:
                        if cls._state(verify, row["oid"]) != cls.pre_policy_states[row["oid"]]:
                            raise RuntimeError(
                                "fail-closed function preflight changed an ACL before aborting"
                            )
                cls.fail_closed_snapshot_verified = True

                (
                    cls.public_revoked,
                    cls.direct_revoked,
                    cls.function_regrants_added,
                ) = cls._apply_function_policy(
                    cls.reviewed_oids, cls.approved_regrants
                )
            except Exception:
                cls._cleanup()
                raise

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "resources_created", False):
            cls._cleanup()

    def _connect_as(self, role_name):
        return psycopg.connect(self.connection_urls[role_name], autocommit=True)

    def test_public_security_definer_exposure_is_removed(self):
        self.assertEqual(self.public_exposure_before, "postgres:PUBLIC_PROBE_SECRET")
        with self._connect_as(self.ATTESTOR_ROLE) as conn:
            self.assertTrue(
                conn.execute(
                    "SELECT has_schema_privilege(current_user, 'public', 'USAGE')"
                ).fetchone()[0]
            )
            self.assertFalse(
                conn.execute(
                    "SELECT has_function_privilege("
                    "current_user, 'public.sd_public_definer_probe_ci()', 'EXECUTE')"
                ).fetchone()[0]
            )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT public.sd_public_definer_probe_ci()")

    def test_different_application_schema_is_covered(self):
        self.assertEqual(self.ops_exposure_before, "postgres:OPS_PROBE_SECRET")
        with self._connect_as(self.ATTESTOR_ROLE) as conn:
            self.assertTrue(
                conn.execute(
                    sql.SQL("SELECT has_schema_privilege(current_user, {}, 'USAGE')").format(
                        sql.Literal(self.OPS_SCHEMA)
                    )
                ).fetchone()[0]
            )
            self.assertFalse(
                conn.execute(
                    "SELECT has_function_privilege(current_user, %s, 'EXECUTE')",
                    (self.OPS_SCHEMA + ".sd_ops_definer_probe_ci()",),
                ).fetchone()[0]
            )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    sql.SQL("SELECT {}.sd_ops_definer_probe_ci()").format(
                        sql.Identifier(self.OPS_SCHEMA)
                    )
                )

    def test_direct_attestor_execute_path_is_removed(self):
        direct_oid = next(
            row["oid"] for row in self.candidate_rows_before
            if row["function_name"] == "sd_direct_definer_probe_ci"
        )
        before = self.pre_policy_states[direct_oid]
        self.assertFalse(before[0])
        self.assertTrue(before[1])
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            after = self._state(admin, direct_oid)
        self.assertFalse(after[0])
        self.assertFalse(after[1])
        self.assertFalse(after[2])
        with (
            self._connect_as(self.ATTESTOR_ROLE) as conn,
            self.assertRaises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute("SELECT public.sd_direct_definer_probe_ci()")

    def test_application_direct_execute_is_preserved(self):
        with self._connect_as(self.APP_DIRECT_ROLE) as conn:
            self.assertEqual(
                conn.execute("SELECT public.sd_public_definer_probe_ci()").fetchone()[0],
                "postgres:PUBLIC_PROBE_SECRET",
            )
            self.assertTrue(
                conn.execute(
                    "SELECT has_function_privilege("
                    "current_user, 'public.sd_public_definer_probe_ci()', 'EXECUTE')"
                ).fetchone()[0]
            )
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            direct = admin.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                    ) AS acl
                    WHERE acl.grantee = (
                        SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s
                    )
                      AND acl.privilege_type = 'EXECUTE'
                )
                FROM pg_catalog.pg_proc AS p
                WHERE p.oid = 'public.sd_public_definer_probe_ci()'::regprocedure
                """,
                (self.APP_DIRECT_ROLE,),
            ).fetchone()[0]
            self.assertTrue(direct)

    def test_application_inherited_execute_is_preserved(self):
        with self._connect_as(self.APP_INHERITED_ROLE) as conn:
            self.assertEqual(
                conn.execute(
                    sql.SQL("SELECT {}.sd_ops_definer_probe_ci()").format(
                        sql.Identifier(self.OPS_SCHEMA)
                    )
                ).fetchone()[0],
                "postgres:OPS_PROBE_SECRET",
            )
            self.assertTrue(
                conn.execute(
                    "SELECT has_function_privilege(current_user, %s, 'EXECUTE')",
                    (self.OPS_SCHEMA + ".sd_ops_definer_probe_ci()",),
                ).fetchone()[0]
            )
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            self.assertTrue(
                admin.execute(
                    "SELECT pg_catalog.pg_has_role(%s, %s, 'USAGE')",
                    (self.APP_INHERITED_ROLE, self.APP_GROUP_ROLE),
                ).fetchone()[0]
            )

    def test_non_owner_function_grantor_aborts_before_acl_changes(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            admin.execute(
                """
                CREATE FUNCTION public.sd_delegated_grantor_probe_ci()
                RETURNS text LANGUAGE sql SECURITY DEFINER
                SET search_path = pg_catalog
                AS $function$ SELECT current_user::text $function$
                """
            )
            function_oid = admin.execute(
                "SELECT pg_catalog.to_regprocedure(%s)::oid",
                ("public.sd_delegated_grantor_probe_ci()",),
            ).fetchone()[0]
            admin.execute(
                "GRANT EXECUTE ON FUNCTION public.sd_delegated_grantor_probe_ci() "
                "TO sd_delegated_grantor_ci WITH GRANT OPTION"
            )
            admin.execute(
                sql.SQL("SET ROLE {}").format(
                    sql.Identifier(self.DELEGATED_GRANTOR_ROLE)
                )
            )
            admin.execute(
                "GRANT EXECUTE ON FUNCTION public.sd_delegated_grantor_probe_ci() "
                "TO sd_app_direct_ci"
            )
            admin.execute("RESET ROLE")
            before = self._state(admin, function_oid)
            self.assertTrue(before[0])
            self.assertTrue(before[2])
            self.assertTrue(
                admin.execute(
                    "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
                    (self.APP_DIRECT_ROLE, function_oid),
                ).fetchone()[0]
            )

            with self.assertRaisesRegex(
                RuntimeError, "non-owner EXECUTE grantor requires separate review"
            ):
                self._apply_function_policy(
                    {function_oid}, [(function_oid, self.APP_DIRECT_ROLE)]
                )
            self.assertEqual(self._state(admin, function_oid), before)
            admin.execute("DROP FUNCTION public.sd_delegated_grantor_probe_ci()")

    def test_public_only_application_consumer_receives_explicit_regrant(self):
        oid = next(
            row["oid"] for row in self.candidate_rows_before
            if row["function_name"] == "sd_public_only_definer_probe_ci"
        )
        self.assertIn((oid, self.APP_PUBLIC_ROLE), self.function_regrants_added)
        with self._connect_as(self.APP_PUBLIC_ROLE) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT public.sd_public_only_definer_probe_ci()"
                ).fetchone()[0],
                "postgres:PUBLIC_PROBE_SECRET",
            )
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            self.assertFalse(
                admin.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_catalog.aclexplode(
                            COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                        ) AS acl
                        WHERE acl.grantee = 0
                          AND acl.privilege_type = 'EXECUTE'
                    )
                    FROM pg_catalog.pg_proc AS p
                    WHERE p.oid = %s
                    """,
                    (oid,),
                ).fetchone()[0]
            )
            self.assertTrue(
                admin.execute(
                    "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
                    (self.APP_PUBLIC_ROLE, oid),
                ).fetchone()[0]
            )

    def test_effective_privilege_matrix_and_unexpected_membership_are_denied(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            inventory = self._inventory(admin)
            app_rows = [
                row for row in inventory
                if row["schema_class"] == "APPLICATION"
                and row["attestor_schema_usage"]
            ]
            unapproved = [
                row for row in app_rows
                if not (
                    row["schema_name"] == self.WRAPPER_SCHEMA
                    and row["function_name"] == self.WRAPPER_NAME
                )
                and row["attestor_effective_execute"]
            ]
            self.assertEqual(unapproved, [])
            wrapper = [
                row for row in app_rows
                if row["schema_name"] == self.WRAPPER_SCHEMA
                and row["function_name"] == self.WRAPPER_NAME
            ]
            self.assertEqual(len(wrapper), 1)
            self.assertTrue(wrapper[0]["attestor_effective_execute"])
            self.assertFalse(wrapper[0]["public_execute"])
            self.assertEqual(wrapper[0]["owner_name"], "postgres")
            self.assertEqual(wrapper[0]["function_search_path"], "pg_catalog")
            self.assertTrue(wrapper[0]["security_definer"])
            for row in app_rows:
                self.assertEqual(row["function_search_path"], "pg_catalog")
                for entry in row["execute_acl_evidence"]:
                    if entry["grantee"] != row["owner_name"]:
                        self.assertFalse(entry["grantable"])

            membership_count = admin.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_auth_members AS m
                JOIN pg_catalog.pg_roles AS member ON member.oid = m.member
                WHERE member.rolname = %s
                """,
                (self.ATTESTOR_ROLE,),
            ).fetchone()[0]
            self.assertEqual(membership_count, 0)

        with self._connect_as(self.ATTESTOR_ROLE) as conn:
            wrapper_result = conn.execute(
                "SELECT attestation_private.cluster_system_identifier()"
            ).fetchone()[0]
            self.assertRegex(wrapper_result, r"^[0-9]{10,30}$")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    sql.SQL("SET ROLE {}").format(sql.Identifier(self.APP_GROUP_ROLE))
                )

    def _assert_runtime_guard_rejects(self, conn, expected_role, category):
        conn.execute("BEGIN READ ONLY")
        try:
            with self.assertRaises(runtime_probe.AttestationFailure) as failure:
                runtime_probe.verify_runtime_identity(conn, expected_role=expected_role)
            self.assertEqual(failure.exception.category, category)
        finally:
            conn.execute("ROLLBACK")

    def test_runtime_guard_accepts_only_dedicated_attestor_and_approved_wrapper(self):
        with self._connect_as(self.ATTESTOR_ROLE) as conn:
            conn.execute("BEGIN READ ONLY")
            try:
                runtime_probe.verify_runtime_identity(
                    conn, expected_role=self.ATTESTOR_ROLE
                )
                cluster_identifier = conn.execute(
                    "SELECT attestation_private.cluster_system_identifier()"
                ).fetchone()[0]
                self.assertRegex(cluster_identifier, r"^[0-9]{10,30}$")
            finally:
                conn.execute("ROLLBACK")

    def test_runtime_guard_rejects_postgres_migration_identity_even_when_wrapper_is_callable(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as migration_conn:
            self.assertTrue(
                migration_conn.execute(
                    "SELECT pg_catalog.has_function_privilege(current_user, "
                    "'attestation_private.cluster_system_identifier()', 'EXECUTE')"
                ).fetchone()[0]
            )
            self._assert_runtime_guard_rejects(
                migration_conn, self.ATTESTOR_ROLE, "runtime_identity_mismatch"
            )

    def test_runtime_guard_rejects_superuser_even_when_wrapper_is_callable(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as superuser_conn:
            self.assertTrue(
                superuser_conn.execute(
                    "SELECT pg_catalog.has_function_privilege(current_user, "
                    "'attestation_private.cluster_system_identifier()', 'EXECUTE')"
                ).fetchone()[0]
            )
            self._assert_runtime_guard_rejects(
                superuser_conn, "postgres", "runtime_role_attributes_unverified"
            )

    def test_runtime_guard_rejects_current_user_mismatch(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            admin.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(self.APP_DIRECT_ROLE),
                    sql.Identifier(self.ATTESTOR_ROLE),
                )
            )
        try:
            with self._connect_as(self.ATTESTOR_ROLE) as conn:
                conn.execute("BEGIN READ ONLY")
                conn.execute(
                    sql.SQL("SET LOCAL ROLE {}").format(
                        sql.Identifier(self.APP_DIRECT_ROLE)
                    )
                )
                with self.assertRaises(runtime_probe.AttestationFailure) as failure:
                    runtime_probe.verify_runtime_identity(
                        conn, expected_role=self.ATTESTOR_ROLE
                    )
                self.assertEqual(failure.exception.category, "runtime_identity_mismatch")
                conn.execute("ROLLBACK")
        finally:
            with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("REVOKE {} FROM {}").format(
                        sql.Identifier(self.APP_DIRECT_ROLE),
                        sql.Identifier(self.ATTESTOR_ROLE),
                    )
                )

    def test_runtime_guard_rejects_createrole_createdb_and_bypassrls(self):
        for role_name in (
            self.RUNTIME_CREATEROLE_ROLE,
            self.RUNTIME_CREATEDB_ROLE,
            self.RUNTIME_BYPASSRLS_ROLE,
        ):
            with self.subTest(role=role_name), self._connect_as(role_name) as conn:
                self._assert_runtime_guard_rejects(
                    conn, role_name, "runtime_role_attributes_unverified"
                )

    def test_runtime_guard_rejects_inherit_role_attribute(self):
        with self._connect_as(self.RUNTIME_INHERIT_ROLE) as conn:
            self._assert_runtime_guard_rejects(
                conn,
                self.RUNTIME_INHERIT_ROLE,
                "runtime_role_attributes_unverified",
            )

    def test_runtime_guard_rejects_unexpected_role_membership(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            admin.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(self.APP_GROUP_ROLE),
                    sql.Identifier(self.ATTESTOR_ROLE),
                )
            )
        try:
            with self._connect_as(self.ATTESTOR_ROLE) as conn:
                self._assert_runtime_guard_rejects(
                    conn, self.ATTESTOR_ROLE, "runtime_role_attributes_unverified"
                )
        finally:
            with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("REVOKE {} FROM {}").format(
                        sql.Identifier(self.APP_GROUP_ROLE),
                        sql.Identifier(self.ATTESTOR_ROLE),
                    )
                )

    def test_runtime_guard_rejects_public_schema_create(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            admin.execute(
                sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(
                    sql.Identifier(self.ATTESTOR_ROLE)
                )
            )
        try:
            with self._connect_as(self.ATTESTOR_ROLE) as conn:
                self._assert_runtime_guard_rejects(
                    conn,
                    self.ATTESTOR_ROLE,
                    "runtime_effective_privilege_guard_failed",
                )
        finally:
            with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(
                        sql.Identifier(self.ATTESTOR_ROLE)
                    )
                )

    def test_runtime_guard_rejects_direct_pg_control_system_execute(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            admin.execute(
                "GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() "
                "TO tia_attestor_function_ci"
            )
        try:
            with self._connect_as(self.ATTESTOR_ROLE) as conn:
                self._assert_runtime_guard_rejects(
                    conn, self.ATTESTOR_ROLE, "runtime_effective_privilege_guard_failed"
                )
        finally:
            with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
                admin.execute(
                    "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_control_system() "
                    "FROM tia_attestor_function_ci"
                )

    def test_runtime_guard_rejects_non_allowlisted_public_security_definer(self):
        function_name = "sd_runtime_unexpected_definer_ci"
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            if admin.execute(
                """
                SELECT 1 FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public' AND p.proname = %s
                """,
                (function_name,),
            ).fetchone():
                raise RuntimeError("runtime unexpected function fixture already exists")
            admin.execute(
                sql.SQL(
                    """
                    CREATE FUNCTION public.{}()
                    RETURNS text LANGUAGE plpgsql SECURITY DEFINER
                    SET search_path = pg_catalog
                    AS $function$
                    BEGIN
                        RAISE EXCEPTION 'non-allowlisted function must never execute';
                    END
                    $function$
                    """
                ).format(sql.Identifier(function_name))
            )
            self.assertTrue(
                admin.execute(
                    "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
                    (self.ATTESTOR_ROLE, f"public.{function_name}()"),
                ).fetchone()[0]
            )
        try:
            with self._connect_as(self.ATTESTOR_ROLE) as conn:
                self._assert_runtime_guard_rejects(
                    conn, self.ATTESTOR_ROLE, "unexpected_security_definer_surface"
                )
        finally:
            with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP FUNCTION IF EXISTS public.{}()").format(
                        sql.Identifier(function_name)
                    )
                )

    def test_runtime_guard_rejects_missing_wrapper_privileges(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            admin.execute(
                "REVOKE EXECUTE ON FUNCTION "
                "attestation_private.cluster_system_identifier() "
                "FROM tia_attestor_function_ci"
            )
        try:
            with self._connect_as(self.ATTESTOR_ROLE) as conn:
                self._assert_runtime_guard_rejects(
                    conn, self.ATTESTOR_ROLE, "runtime_effective_privilege_guard_failed"
                )
        finally:
            with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
                admin.execute(
                    "GRANT EXECUTE ON FUNCTION "
                    "attestation_private.cluster_system_identifier() "
                    "TO tia_attestor_function_ci"
                )

    def test_atomic_future_function_publish_and_application_access_paths(self):
        function_name = "future_atomic_publish_ci"
        function_identifier = sql.Identifier(self.UNCONFIGURED_SCHEMA, function_name)
        function_oid = self._atomic_create_future_function(
            self.UNCONFIGURED_SCHEMA,
            self.UNCONFIGURED_OWNER_ROLE,
            function_name,
            revoke_public=True,
            approved_roles=(
                self.APP_DIRECT_ROLE,
                self.APP_GROUP_ROLE,
                self.APP_PUBLIC_ROLE,
            ),
            expected_default_public=True,
        )
        try:
            with self._connect_as(self.ATTESTOR_ROLE) as attestor:
                self.assertFalse(
                    attestor.execute(
                        "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
                        (self.ATTESTOR_ROLE, function_oid),
                    ).fetchone()[0]
                )
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    attestor.execute(
                        sql.SQL("SELECT {}()").format(function_identifier)
                    )

            for role_name in (
                self.APP_DIRECT_ROLE,
                self.APP_INHERITED_ROLE,
                self.APP_PUBLIC_ROLE,
            ):
                with self._connect_as(role_name) as application:
                    self.assertEqual(
                        application.execute(
                            sql.SQL("SELECT {}()").format(function_identifier)
                        ).fetchone()[0],
                        self.UNCONFIGURED_OWNER_ROLE,
                    )
        finally:
            with psycopg.connect(self.admin_conninfo, autocommit=True) as cleanup:
                cleanup.execute(
                    sql.SQL("DROP FUNCTION IF EXISTS {}()").format(function_identifier)
                )

    def test_unconfigured_owner_atomic_verification_failure_rolls_back_everything(self):
        function_name = "future_unconfigured_rollback_ci"
        function_identifier = sql.Identifier(self.UNCONFIGURED_SCHEMA, function_name)
        function_identity = f"{self.UNCONFIGURED_SCHEMA}.{function_name}()"
        with self.assertRaisesRegex(
            RuntimeError, "approved application EXECUTE verification failed"
        ):
            self._atomic_create_future_function(
                self.UNCONFIGURED_SCHEMA,
                self.UNCONFIGURED_OWNER_ROLE,
                function_name,
                revoke_public=True,
                approved_roles=(self.APP_DIRECT_ROLE, self.APP_GROUP_ROLE),
                expected_default_public=True,
            )

        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            self.assertIsNone(
                admin.execute(
                    "SELECT pg_catalog.to_regprocedure(%s)::oid",
                    (function_identity,),
                ).fetchone()[0]
            )
        with self._connect_as(self.ATTESTOR_ROLE) as attestor:
            self.assertIsNone(
                attestor.execute(
                    "SELECT pg_catalog.to_regprocedure(%s)::oid",
                    (function_identity,),
                ).fetchone()[0]
            )
            with self.assertRaises(psycopg.errors.UndefinedFunction):
                attestor.execute(
                    sql.SQL("SELECT {}()").format(function_identifier)
                )

    def test_unprotected_future_deployment_is_rejected_before_commit(self):
        function_name = "future_unprotected_rejected_ci"
        function_identifier = sql.Identifier(self.UNCONFIGURED_SCHEMA, function_name)
        function_identity = f"{self.UNCONFIGURED_SCHEMA}.{function_name}()"
        with self.assertRaisesRegex(
            RuntimeError, "PUBLIC/attestor EXECUTE guard failed"
        ):
            self._atomic_create_future_function(
                self.UNCONFIGURED_SCHEMA,
                self.UNCONFIGURED_OWNER_ROLE,
                function_name,
                revoke_public=False,
                approved_roles=(
                    self.APP_DIRECT_ROLE,
                    self.APP_GROUP_ROLE,
                    self.APP_PUBLIC_ROLE,
                ),
                expected_default_public=True,
            )

        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            self.assertIsNone(
                admin.execute(
                    "SELECT pg_catalog.to_regprocedure(%s)::oid",
                    (function_identity,),
                ).fetchone()[0]
            )
        with self._connect_as(self.ATTESTOR_ROLE) as attestor:
            self.assertIsNone(
                attestor.execute(
                    "SELECT pg_catalog.to_regprocedure(%s)::oid",
                    (function_identity,),
                ).fetchone()[0]
            )
            with self.assertRaises(psycopg.errors.UndefinedFunction):
                attestor.execute(
                    sql.SQL("SELECT {}()").format(function_identifier)
                )

    def test_future_function_defaults_are_owner_specific_and_fail_closed(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            admin.execute(
                sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} "
                        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC").format(
                    sql.Identifier(self.FUTURE_OWNER_ROLE)
                )
            )
            admin.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
                    "GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
                ).format(
                    sql.Identifier(self.FUTURE_OWNER_ROLE),
                    sql.Identifier(self.FUTURE_SCHEMA),
                )
            )
            default_override = admin.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_default_acl AS d
                        CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) AS acl
                        WHERE d.defaclrole = (
                            SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s
                        )
                          AND d.defaclnamespace = 0
                          AND d.defaclobjtype = 'f'
                          AND acl.grantee = 0
                          AND acl.privilege_type = 'EXECUTE'
                    ),
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_default_acl AS d
                        CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) AS acl
                        WHERE d.defaclrole = (
                            SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s
                        )
                          AND d.defaclnamespace = (
                              SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = %s
                          )
                          AND d.defaclobjtype = 'f'
                          AND acl.grantee = 0
                          AND acl.privilege_type = 'EXECUTE'
                    )
                """,
                (self.FUTURE_OWNER_ROLE, self.FUTURE_OWNER_ROLE, self.FUTURE_SCHEMA),
            ).fetchone()
            self.assertEqual(default_override, (False, True))
            admin.execute(
                sql.SQL("SET ROLE {}").format(sql.Identifier(self.FUTURE_OWNER_ROLE))
            )
            admin.execute(
                sql.SQL(
                    """
                    CREATE FUNCTION {}.future_schema_override_ci()
                    RETURNS text LANGUAGE sql SECURITY DEFINER
                    SET search_path = pg_catalog
                    AS $function$ SELECT current_user::text $function$
                    """
                ).format(sql.Identifier(self.FUTURE_SCHEMA))
            )
            admin.execute("RESET ROLE")
            override_result = admin.execute(
                sql.SQL("SELECT {}.future_schema_override_ci()").format(
                    sql.Identifier(self.FUTURE_SCHEMA)
                )
            ).fetchone()[0]
            self.assertEqual(override_result, self.FUTURE_OWNER_ROLE)
            with self._connect_as(self.ATTESTOR_ROLE) as attestor:
                self.assertEqual(
                    attestor.execute(
                        sql.SQL("SELECT {}.future_schema_override_ci()").format(
                            sql.Identifier(self.FUTURE_SCHEMA)
                        )
                    ).fetchone()[0],
                    self.FUTURE_OWNER_ROLE,
                )
            self.assertTrue(
                admin.execute(
                    "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
                    (
                        self.ATTESTOR_ROLE,
                        self.FUTURE_SCHEMA + ".future_schema_override_ci()",
                    ),
                ).fetchone()[0]
            )
            self.assertTrue(
                admin.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_catalog.aclexplode(
                            COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                        ) AS acl
                        WHERE acl.grantee = 0
                          AND acl.privilege_type = 'EXECUTE'
                    )
                    FROM pg_catalog.pg_proc AS p
                    WHERE p.oid = pg_catalog.to_regprocedure(%s)::oid
                    """,
                    (self.FUTURE_SCHEMA + ".future_schema_override_ci()",),
                ).fetchone()[0]
            )

            admin.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
                    "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
                ).format(
                    sql.Identifier(self.FUTURE_OWNER_ROLE),
                    sql.Identifier(self.FUTURE_SCHEMA),
                )
            )
            admin.execute(
                sql.SQL("SET ROLE {}").format(sql.Identifier(self.FUTURE_OWNER_ROLE))
            )
            admin.execute(
                sql.SQL(
                    """
                    CREATE FUNCTION {}.future_hardened_ci()
                    RETURNS text LANGUAGE sql SECURITY DEFINER
                    SET search_path = pg_catalog
                    AS $function$ SELECT current_user::text $function$
                    """
                ).format(sql.Identifier(self.FUTURE_SCHEMA))
            )
            admin.execute("RESET ROLE")
            safe_oid = admin.execute(
                "SELECT pg_catalog.to_regprocedure(%s)::oid",
                (self.FUTURE_SCHEMA + ".future_hardened_ci()",),
            ).fetchone()[0]
            safe_state = admin.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.aclexplode(
                            COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                        ) AS acl
                        WHERE acl.grantee = 0
                          AND acl.privilege_type = 'EXECUTE'
                    ),
                    pg_catalog.has_function_privilege(%s, p.oid, 'EXECUTE')
                FROM pg_catalog.pg_proc AS p
                WHERE p.oid = %s
                """,
                (self.ATTESTOR_ROLE, safe_oid),
            ).fetchone()
            self.assertEqual(safe_state, (False, False))
            admin.execute(
                sql.SQL("DROP FUNCTION {}.future_schema_override_ci()").format(
                    sql.Identifier(self.FUTURE_SCHEMA)
                )
            )

            admin.execute(
                sql.SQL("SET ROLE {}").format(
                    sql.Identifier(self.UNCONFIGURED_OWNER_ROLE)
                )
            )
            admin.execute(
                sql.SQL(
                    """
                    CREATE FUNCTION {}.future_unconfigured_ci()
                    RETURNS text LANGUAGE sql SECURITY DEFINER
                    SET search_path = pg_catalog
                    AS $function$ SELECT current_user::text $function$
                    """
                ).format(sql.Identifier(self.UNCONFIGURED_SCHEMA))
            )
            admin.execute("RESET ROLE")
            unconfigured_oid = admin.execute(
                """
                SELECT p.oid
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = %s AND p.proname = 'future_unconfigured_ci'
                """,
                (self.UNCONFIGURED_SCHEMA,),
            ).fetchone()[0]
            before = self._state(admin, unconfigured_oid)
            self.assertTrue(before[0])
            self.assertTrue(before[2])
            with self._connect_as(self.ATTESTOR_ROLE) as attestor:
                self.assertEqual(
                    attestor.execute(
                        sql.SQL("SELECT {}.future_unconfigured_ci()").format(
                            sql.Identifier(self.UNCONFIGURED_SCHEMA)
                        )
                    ).fetchone()[0],
                    self.UNCONFIGURED_OWNER_ROLE,
                )

            with self.assertRaises(RuntimeError):
                self._apply_function_policy({unconfigured_oid}, [])
            self.assertEqual(self._state(admin, unconfigured_oid), before)
            admin.execute(
                sql.SQL("DROP FUNCTION {}.future_unconfigured_ci()").format(
                    sql.Identifier(self.UNCONFIGURED_SCHEMA)
                )
            )

    def test_system_and_extension_effective_attestor_exposure_aborts(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            admin.execute(
                """
                CREATE FUNCTION pg_catalog.sd_system_exposure_probe_ci()
                RETURNS text
                LANGUAGE sql
                SECURITY DEFINER
                SET search_path = pg_catalog
                AS $function$ SELECT current_user::text $function$
                """
            )
            admin.execute(
                "GRANT EXECUTE ON FUNCTION public.sd_extension_member_probe_ci() TO PUBLIC"
            )
            try:
                inventory = self._inventory(admin)
                system_row = next(
                    row for row in inventory
                    if row["schema_name"] == "pg_catalog"
                    and row["function_name"] == "sd_system_exposure_probe_ci"
                )
                extension_row = next(
                    row for row in inventory
                    if row["schema_name"] == "public"
                    and row["function_name"] == "sd_extension_member_probe_ci"
                )
                self.assertEqual(system_row["schema_class"], "SYSTEM")
                self.assertEqual(extension_row["schema_class"], "EXTENSION")
                for row in (system_row, extension_row):
                    self.assertTrue(row["attestor_schema_usage"])
                    self.assertTrue(row["attestor_effective_execute"])

                system_before = self._state(admin, system_row["oid"])
                extension_before = self._state(admin, extension_row["oid"])
                with self.assertRaisesRegex(
                    RuntimeError, "system/extension schema; DBA review required"
                ):
                    self._apply_function_policy(
                        self.reviewed_oids,
                        self.approved_regrants,
                    )
                self.assertEqual(self._state(admin, system_row["oid"]), system_before)
                self.assertEqual(
                    self._state(admin, extension_row["oid"]),
                    extension_before,
                )
            finally:
                admin.execute(
                    "REVOKE EXECUTE ON FUNCTION public.sd_extension_member_probe_ci() "
                    "FROM PUBLIC"
                )
                admin.execute(
                    "DROP FUNCTION IF EXISTS pg_catalog.sd_system_exposure_probe_ci()"
                )

    def test_inventory_separates_system_and_extension_schemas(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            pg_catalog = admin.execute(
                """
                SELECT
                    CASE
                        WHEN n.nspname IN ('pg_catalog', 'information_schema')
                          OR n.nspname LIKE 'pg_toast%'
                        THEN 'SYSTEM'
                        WHEN EXISTS (
                            SELECT 1
                            FROM pg_catalog.pg_extension AS e
                            WHERE e.extnamespace = n.oid
                        )
                        THEN 'EXTENSION'
                        ELSE 'APPLICATION'
                    END,
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_extension AS e
                        WHERE e.extnamespace = n.oid
                    )
                FROM pg_catalog.pg_namespace AS n
                WHERE n.nspname = 'pg_catalog'
                """
            ).fetchone()
            self.assertEqual(pg_catalog, ("SYSTEM", True))
            inventory = self._inventory(admin)
            self.assertTrue(
                any(
                    row["schema_class"] == "APPLICATION"
                    and row["schema_name"] == self.OPS_SCHEMA
                    for row in inventory
                )
            )
            public_application_function = next(
                row for row in inventory
                if row["schema_name"] == "public"
                and row["function_name"] == "sd_public_definer_probe_ci"
            )
            self.assertEqual(public_application_function["schema_class"], "APPLICATION")
            self.assertTrue(public_application_function["extension_owned_schema"])
            extension_function = next(
                row for row in inventory
                if row["schema_name"] == "public"
                and row["function_name"] == "sd_extension_member_probe_ci"
            )
            self.assertEqual(extension_function["schema_class"], "EXTENSION")
            self.assertFalse(extension_function["attestor_effective_execute"])

    def test_unreviewed_function_inventory_aborts_without_acl_changes(self):
        self.assertIn("consumer inventory is incomplete", self.fail_closed_error)
        self.assertTrue(self.fail_closed_snapshot_verified)

    def test_manifest_rollback_restores_original_effective_paths(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            with admin.transaction():
                for function_oid, role_name in self.function_regrants_added:
                    row = next(
                        item for item in self.candidate_rows_before
                        if item["oid"] == function_oid
                    )
                    admin.execute(
                        sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM {}").format(
                            self._function_identifier(row),
                            sql.Identifier(role_name),
                        )
                    )
                for function_oid in self.public_revoked:
                    row = next(
                        item for item in self.candidate_rows_before
                        if item["oid"] == function_oid
                    )
                    admin.execute(
                        sql.SQL("GRANT EXECUTE ON FUNCTION {} TO PUBLIC").format(
                            self._function_identifier(row)
                        )
                    )
                for function_oid in self.direct_revoked:
                    row = next(
                        item for item in self.candidate_rows_before
                        if item["oid"] == function_oid
                    )
                    admin.execute(
                        sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
                            self._function_identifier(row),
                            sql.Identifier(self.ATTESTOR_ROLE),
                        )
                    )

            for row in self.candidate_rows_before:
                before = self.pre_policy_states[row["oid"]]
                after = self._state(admin, row["oid"])
                self.assertEqual(after, before)

        (
            self.public_revoked,
            self.direct_revoked,
            self.function_regrants_added,
        ) = self._apply_function_policy(
            self.reviewed_oids, self.approved_regrants
        )
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            for row in self.candidate_rows_before:
                self.assertFalse(self._state(admin, row["oid"])[2])


class DatabaseConnectIsolationPostgres176Tests(unittest.TestCase):
    """Verify cluster-wide CONNECT isolation without changing application access."""

    TARGET_DATABASE = "attestation_ci"
    GUARD_DATABASE = "attestor_drop_guard_ci"
    UNEXPECTED_DATABASE = "attestor_must_not_create_ci"
    FUTURE_UNGUARDED_DATABASE = "attestor_future_unprotected_ci"
    FUTURE_GUARDED_DATABASE = "attestor_future_guarded_ci"
    ATTESTOR_ROLE = "tia_connect_attestor_ci"
    APP_GROUP_ROLE = "tia_connect_app_group_ci"
    APP_RUNTIME_ROLE = "tia_connect_app_runtime_ci"
    MIGRATION_ROLE = "tia_connect_migration_ci"
    LEGACY_APP_ROLE = "tia_connect_legacy_app_ci"

    @classmethod
    def _has_database_privilege(cls, conn, role_name, database_name):
        return conn.execute(
            "SELECT pg_catalog.has_database_privilege(%s, %s, 'CONNECT')",
            (role_name, database_name),
        ).fetchone()[0]

    @classmethod
    def _has_acl_grant(cls, conn, role_name, database_name):
        return conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_database AS d
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        d.datacl,
                        pg_catalog.acldefault('d', d.datdba)
                    )
                ) AS acl
                WHERE d.datname = %s
                  AND acl.grantee = (
                      SELECT oid
                      FROM pg_catalog.pg_roles
                      WHERE rolname = %s
                  )
                  AND acl.privilege_type = 'CONNECT'
            )
            """,
            (database_name, role_name),
        ).fetchone()[0]

    @classmethod
    def _public_connect(cls, conn, database_name):
        return conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_database AS d
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        d.datacl,
                        pg_catalog.acldefault('d', d.datdba)
                    )
                ) AS acl
                WHERE d.datname = %s
                  AND acl.grantee = 0
                  AND acl.privilege_type = 'CONNECT'
            )
            """,
            (database_name,),
        ).fetchone()[0]

    @classmethod
    def _cleanup(cls):
        if not getattr(cls, "admin_conninfo", None):
            return
        try:
            with psycopg.connect(cls.admin_conninfo, autocommit=True) as admin:
                role_names = (
                    cls.ATTESTOR_ROLE,
                    cls.APP_GROUP_ROLE,
                    cls.APP_RUNTIME_ROLE,
                    cls.MIGRATION_ROLE,
                    cls.LEGACY_APP_ROLE,
                )
                existing_roles = {
                    row[0]
                    for row in admin.execute(
                        """
                        SELECT rolname
                        FROM pg_catalog.pg_roles
                        WHERE rolname = ANY(%s)
                        """,
                        (list(role_names),),
                    ).fetchall()
                }

                if getattr(cls, "database_inventory", None):
                    for database_name, _datallowconn in cls.database_inventory:
                        if database_name == cls.TARGET_DATABASE:
                            continue
                        for role_name in role_names:
                            if role_name not in existing_roles:
                                continue
                            admin.execute(
                                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                                    sql.Identifier(database_name),
                                    sql.Identifier(role_name),
                                )
                            )
                        if cls.public_connect_before.get(database_name, False):
                            admin.execute(
                                sql.SQL("GRANT CONNECT ON DATABASE {} TO PUBLIC").format(
                                    sql.Identifier(database_name)
                                )
                            )

                if cls.ATTESTOR_ROLE in existing_roles:
                    admin.execute(
                        sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                            sql.Identifier(cls.TARGET_DATABASE),
                            sql.Identifier(cls.ATTESTOR_ROLE),
                        )
                    )
                if {
                    cls.APP_GROUP_ROLE,
                    cls.APP_RUNTIME_ROLE,
                }.issubset(existing_roles):
                    admin.execute(
                        sql.SQL("REVOKE {} FROM {}").format(
                            sql.Identifier(cls.APP_GROUP_ROLE),
                            sql.Identifier(cls.APP_RUNTIME_ROLE),
                        )
                    )
                for database_name in reversed(
                    getattr(cls, "created_databases", [])
                ):
                    admin.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                            sql.Identifier(database_name)
                        )
                    )
                for role_name in reversed(getattr(cls, "created_roles", [])):
                    if role_name in existing_roles:
                        admin.execute(
                            sql.SQL("DROP ROLE IF EXISTS {}").format(
                                sql.Identifier(role_name)
                            )
                        )
        except psycopg.Error:
            # Preserve the original test failure; the disposable CI cluster is destroyed.
            pass

    @classmethod
    def setUpClass(cls):
        raw_url = os.environ.get("ATTESTATION_TEST_DATABASE_URL")
        if not raw_url:
            raise unittest.SkipTest("ATTESTATION_TEST_DATABASE_URL is not configured")
        url = make_url(raw_url)
        if (
            url.host not in {"127.0.0.1", "localhost"}
            or url.database != cls.TARGET_DATABASE
            or url.username != "postgres"
        ):
            raise RuntimeError(
                "CONNECT isolation test refuses a non-local attestation_ci database"
            )

        cls.admin_conninfo = raw_url
        cls.base_url = url
        cls.created_roles = []
        cls.created_databases = []
        cls.database_inventory = []
        cls.public_connect_before = {}

        try:
            with psycopg.connect(raw_url, autocommit=True) as admin:
                version_num = int(
                    admin.execute("SHOW server_version_num").fetchone()[0]
                )
                if version_num != 170006:
                    raise RuntimeError(
                        "expected PostgreSQL 17.6, "
                        f"got server_version_num={version_num}"
                    )

                existing_guard_databases = admin.execute(
                    """
                    SELECT datname
                    FROM pg_catalog.pg_database
                    WHERE datname = ANY(%s)
                    """,
                    ([
                        cls.GUARD_DATABASE,
                        cls.UNEXPECTED_DATABASE,
                        cls.FUTURE_UNGUARDED_DATABASE,
                        cls.FUTURE_GUARDED_DATABASE,
                    ],),
                ).fetchall()
                if existing_guard_databases:
                    raise RuntimeError(
                        "database guard fixtures already exist; refusing to reuse them"
                    )
                admin.execute(
                    sql.SQL("CREATE DATABASE {}").format(
                        sql.Identifier(cls.GUARD_DATABASE)
                    )
                )
                cls.created_databases.append(cls.GUARD_DATABASE)

                rows = admin.execute(
                    """
                    SELECT datname, datallowconn
                    FROM pg_catalog.pg_database
                    ORDER BY datname
                    """
                ).fetchall()
                cls.database_inventory = [
                    (str(database_name), bool(datallowconn))
                    for database_name, datallowconn in rows
                ]
                database_names = {
                    database_name
                    for database_name, _datallowconn in cls.database_inventory
                }
                if cls.TARGET_DATABASE not in database_names:
                    raise RuntimeError("the disposable target database is missing")

                non_target_databases = [
                    database_name
                    for database_name, _datallowconn in cls.database_inventory
                    if database_name != cls.TARGET_DATABASE
                ]
                if not non_target_databases:
                    raise RuntimeError(
                        "the PostgreSQL cluster has no non-target database to test"
                    )

                role_names = (
                    cls.ATTESTOR_ROLE,
                    cls.APP_GROUP_ROLE,
                    cls.APP_RUNTIME_ROLE,
                    cls.MIGRATION_ROLE,
                    cls.LEGACY_APP_ROLE,
                )
                existing = admin.execute(
                    """
                    SELECT rolname
                    FROM pg_catalog.pg_roles
                    WHERE rolname = ANY(%s)
                    """,
                    (list(role_names),),
                ).fetchall()
                if existing:
                    raise RuntimeError(
                        "test roles already exist; refusing to reuse them: "
                        + ", ".join(row[0] for row in existing)
                    )

                for database_name in non_target_databases:
                    public_connect = cls._public_connect(admin, database_name)
                    cls.public_connect_before[database_name] = public_connect
                    if not public_connect:
                        raise RuntimeError(
                            "expected stock PUBLIC CONNECT on non-target database "
                            f"{database_name!r}; refusing to infer its original ACL"
                        )

                passwords = {
                    cls.ATTESTOR_ROLE: secrets.token_urlsafe(32),
                    cls.APP_RUNTIME_ROLE: secrets.token_urlsafe(32),
                    cls.MIGRATION_ROLE: secrets.token_urlsafe(32),
                    cls.LEGACY_APP_ROLE: secrets.token_urlsafe(32),
                }
                cls.connection_urls = {
                    role_name: url.set(
                        username=role_name,
                        password=password,
                    )
                    for role_name, password in passwords.items()
                }

                create_login = """
                    CREATE ROLE {} LOGIN PASSWORD {}
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
                    NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2
                """
                for role_name in (
                    cls.ATTESTOR_ROLE,
                    cls.APP_RUNTIME_ROLE,
                    cls.MIGRATION_ROLE,
                    cls.LEGACY_APP_ROLE,
                ):
                    admin.execute(
                        sql.SQL(create_login).format(
                            sql.Identifier(role_name),
                            sql.Literal(passwords[role_name]),
                        )
                    )
                    cls.created_roles.append(role_name)

                admin.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                        sql.Identifier(cls.APP_GROUP_ROLE)
                    )
                )
                cls.created_roles.append(cls.APP_GROUP_ROLE)
                admin.execute(
                    sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET TRUE").format(
                        sql.Identifier(cls.APP_GROUP_ROLE),
                        sql.Identifier(cls.APP_RUNTIME_ROLE),
                    )
                )

                admin.execute(
                    sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(
                        sql.Identifier(cls.ATTESTOR_ROLE)
                    )
                )
                admin.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(cls.TARGET_DATABASE),
                        sql.Identifier(cls.ATTESTOR_ROLE),
                    )
                )

                # Establish three different pre-existing application access paths:
                # direct grant, inherited group grant, and PUBLIC-only access.
                for database_name, datallowconn in cls.database_inventory:
                    if database_name == cls.TARGET_DATABASE or not datallowconn:
                        continue
                    admin.execute(
                        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                            sql.Identifier(database_name),
                            sql.Identifier(cls.APP_GROUP_ROLE),
                        )
                    )
                    admin.execute(
                        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                            sql.Identifier(database_name),
                            sql.Identifier(cls.MIGRATION_ROLE),
                        )
                    )

                for database_name in non_target_databases:
                    if not cls._has_database_privilege(
                        admin, cls.ATTESTOR_ROLE, database_name
                    ):
                        raise RuntimeError(
                            f"expected PUBLIC to give attestor CONNECT on {database_name}"
                        )
                    if cls._has_acl_grant(admin, cls.ATTESTOR_ROLE, database_name):
                        raise RuntimeError(
                            f"unexpected direct attestor CONNECT grant on {database_name}"
                        )
                    if not cls._has_database_privilege(
                        admin, cls.APP_RUNTIME_ROLE, database_name
                    ):
                        raise RuntimeError(
                            f"runtime role lacks its pre-change CONNECT on {database_name}"
                        )
                    if not cls._has_database_privilege(
                        admin, cls.MIGRATION_ROLE, database_name
                    ):
                        raise RuntimeError(
                            f"migration role lacks its pre-change CONNECT on {database_name}"
                        )
                    if not cls._has_database_privilege(
                        admin, cls.LEGACY_APP_ROLE, database_name
                    ):
                        raise RuntimeError(
                            f"legacy application role lacks PUBLIC CONNECT on {database_name}"
                        )

                # Prove a direct REVOKE is not a DENY while PUBLIC still grants CONNECT.
                proof_database = next(
                    database_name
                    for database_name, datallowconn in cls.database_inventory
                    if database_name != cls.TARGET_DATABASE and datallowconn
                )
                admin.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(proof_database),
                        sql.Identifier(cls.ATTESTOR_ROLE),
                    )
                )
                admin.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                        sql.Identifier(proof_database),
                        sql.Identifier(cls.ATTESTOR_ROLE),
                    )
                )
                if not cls._has_database_privilege(
                    admin, cls.ATTESTOR_ROLE, proof_database
                ):
                    raise RuntimeError(
                        "direct REVOKE unexpectedly overrode PUBLIC CONNECT"
                    )

                # A missing service-owner regrant must abort the ACL transaction.
                try:
                    with admin.transaction():
                        admin.execute(
                            sql.SQL(
                                "REVOKE CONNECT ON DATABASE {} FROM PUBLIC"
                            ).format(sql.Identifier(proof_database))
                        )
                        if cls._has_database_privilege(
                            admin, cls.LEGACY_APP_ROLE, proof_database
                        ):
                            raise RuntimeError(
                                "expected PUBLIC-only application role to lose CONNECT"
                            )
                        raise RuntimeError("missing approved CONNECT regrant")
                except RuntimeError as exc:
                    if str(exc) != "missing approved CONNECT regrant":
                        raise
                else:
                    raise RuntimeError("incomplete CONNECT allowlist did not abort")
                if not cls._public_connect(admin, proof_database) or not cls._has_database_privilege(
                    admin, cls.LEGACY_APP_ROLE, proof_database
                ):
                    raise RuntimeError("incomplete CONNECT allowlist did not roll back")
                cls.fail_closed_rollback_verified = True

                # Execute the same fail-closed inventory/allowlist algorithm documented
                # in the DBA runbook. The attestor is excluded because Production SQL
                # creates it only after the non-target PUBLIC ACLs have been repaired.
                with admin.transaction():
                    admin.execute(
                        """
                        CREATE TEMP TABLE connect_before ON COMMIT DROP AS
                        SELECT d.datname AS database_name, r.rolname AS role_name
                        FROM pg_catalog.pg_database AS d
                        CROSS JOIN pg_catalog.pg_roles AS r
                        WHERE d.datname <> %s
                          AND d.datallowconn
                          AND r.rolcanlogin
                          AND r.rolname <> %s
                          AND pg_catalog.has_database_privilege(
                              r.rolname, d.datname, 'CONNECT'
                          )
                        """,
                        (cls.TARGET_DATABASE, cls.ATTESTOR_ROLE),
                    )
                    admin.execute(
                        """
                        CREATE TEMP TABLE public_connect_before ON COMMIT DROP AS
                        SELECT
                            d.datname AS database_name,
                            d.datacl::text AS stored_datacl,
                            d.datdba AS database_owner_oid
                        FROM pg_catalog.pg_database AS d
                        WHERE d.datname <> %s
                          AND EXISTS (
                              SELECT 1
                              FROM pg_catalog.aclexplode(
                                  COALESCE(
                                      d.datacl,
                                      pg_catalog.acldefault('d', d.datdba)
                                  )
                              ) AS acl
                              WHERE acl.grantee = 0
                                AND acl.privilege_type = 'CONNECT'
                          )
                        """,
                        (cls.TARGET_DATABASE,),
                    )
                    admin.execute(
                        """
                        CREATE TEMP TABLE approved_connect_regrant (
                            database_name name NOT NULL,
                            role_name name NOT NULL,
                            PRIMARY KEY (database_name, role_name)
                        ) ON COMMIT DROP
                        """
                    )
                    admin.execute(
                        """
                        CREATE TEMP TABLE connect_regrants_added (
                            database_name name NOT NULL,
                            role_name name NOT NULL,
                            PRIMARY KEY (database_name, role_name)
                        ) ON COMMIT DROP
                        """
                    )
                    for database_name, datallowconn in cls.database_inventory:
                        if database_name != cls.TARGET_DATABASE and datallowconn:
                            admin.execute(
                                """
                                INSERT INTO pg_temp.approved_connect_regrant
                                    (database_name, role_name)
                                VALUES (%s, %s)
                                """,
                                (database_name, cls.LEGACY_APP_ROLE),
                            )

                    admin.execute(
                        """
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
                                    COALESCE(
                                        d.datacl,
                                        pg_catalog.acldefault('d', d.datdba)
                                    )
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
                                    'non-target PUBLIC CONNECT has grant option or non-owner grantor';
                            END IF;

                            IF EXISTS (
                                SELECT 1
                                FROM pg_temp.approved_connect_regrant AS a
                                LEFT JOIN pg_catalog.pg_database AS d
                                  ON d.datname = a.database_name
                                LEFT JOIN pg_catalog.pg_roles AS r
                                  ON r.rolname = a.role_name
                                WHERE d.datname IS NULL
                                   OR d.datname = 'attestation_ci'
                                   OR NOT d.datallowconn
                                   OR r.oid IS NULL
                                   OR NOT pg_catalog.has_database_privilege(
                                       a.role_name, a.database_name, 'CONNECT'
                                   )
                            ) THEN
                                RAISE EXCEPTION
                                    'approved CONNECT regrant is not a current effective role/database grant';
                            END IF;

                            FOR v_database IN
                                SELECT database_name
                                FROM pg_temp.public_connect_before
                                ORDER BY database_name
                            LOOP
                                EXECUTE pg_catalog.format(
                                    'REVOKE CONNECT ON DATABASE %I FROM PUBLIC',
                                    v_database.database_name
                                );
                            END LOOP;

                            FOR v_grant IN
                                SELECT database_name, role_name
                                FROM pg_temp.approved_connect_regrant
                                WHERE NOT pg_catalog.has_database_privilege(
                                    role_name, database_name, 'CONNECT'
                                )
                                ORDER BY database_name, role_name
                            LOOP
                                EXECUTE pg_catalog.format(
                                    'GRANT CONNECT ON DATABASE %I TO %I',
                                    v_grant.database_name,
                                    v_grant.role_name
                                );
                                INSERT INTO pg_temp.connect_regrants_added
                                    (database_name, role_name)
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
                                    'post-repair effective CONNECT differs from pre-change login inventory';
                            END IF;

                            IF EXISTS (
                                SELECT 1
                                FROM pg_catalog.pg_database AS d
                                CROSS JOIN LATERAL pg_catalog.aclexplode(
                                    COALESCE(
                                        d.datacl,
                                        pg_catalog.acldefault('d', d.datdba)
                                    )
                                ) AS acl
                                WHERE d.datname <> 'attestation_ci'
                                  AND acl.grantee = 0
                                  AND acl.privilege_type = 'CONNECT'
                            ) THEN
                                RAISE EXCEPTION
                                    'PUBLIC CONNECT remains on a non-target database';
                            END IF;
                        END
                        $connect_isolation$;
                        """
                    )
                    cls.connect_regrants_added = admin.execute(
                        """
                        SELECT database_name, role_name
                        FROM pg_temp.connect_regrants_added
                        ORDER BY database_name, role_name
                        """
                    ).fetchall()
                    expected_regrants = {
                        (database_name, cls.LEGACY_APP_ROLE)
                        for database_name, datallowconn in cls.database_inventory
                        if database_name != cls.TARGET_DATABASE and datallowconn
                    }
                    if set(cls.connect_regrants_added) != expected_regrants:
                        raise RuntimeError(
                            "CONNECT regrant manifest differs from approved public-only roles"
                        )

                for database_name in non_target_databases:
                    if cls._public_connect(admin, database_name):
                        raise RuntimeError(
                            f"PUBLIC CONNECT remains on non-target database {database_name}"
                        )
                    if cls._has_database_privilege(
                        admin, cls.ATTESTOR_ROLE, database_name
                    ):
                        raise RuntimeError(
                            f"attestor retains effective CONNECT on {database_name}"
                        )
                    if cls._has_acl_grant(admin, cls.ATTESTOR_ROLE, database_name):
                        raise RuntimeError(
                            f"attestor has a direct CONNECT ACL on {database_name}"
                        )

                for database_name, datallowconn in cls.database_inventory:
                    if database_name == cls.TARGET_DATABASE or not datallowconn:
                        continue
                    for role_name in (
                        cls.APP_RUNTIME_ROLE,
                        cls.MIGRATION_ROLE,
                        cls.LEGACY_APP_ROLE,
                    ):
                        if not cls._has_database_privilege(
                            admin, role_name, database_name
                        ):
                            raise RuntimeError(
                                f"application role {role_name} lost CONNECT on {database_name}"
                            )
        except Exception:
            cls._cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    def _connect_as(self, role_name, database_name):
        conninfo = self.connection_urls[role_name].set(
            database=database_name
        ).render_as_string(hide_password=False)
        return psycopg.connect(conninfo, connect_timeout=3, autocommit=True)

    def test_incomplete_allowlist_is_rolled_back(self):
        self.assertTrue(self.fail_closed_rollback_verified)

    def test_attestor_has_effective_connect_only_on_the_target_database(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            for database_name, _datallowconn in self.database_inventory:
                with self.subTest(database=database_name):
                    expected = database_name == self.TARGET_DATABASE
                    self.assertEqual(
                        self._has_database_privilege(
                            admin, self.ATTESTOR_ROLE, database_name
                        ),
                        expected,
                    )

        with self._connect_as(self.ATTESTOR_ROLE, self.TARGET_DATABASE) as conn:
            self.assertEqual(
                conn.execute("SELECT current_database()").fetchone()[0],
                self.TARGET_DATABASE,
            )

        for database_name, datallowconn in self.database_inventory:
            if database_name == self.TARGET_DATABASE:
                continue
            with self.subTest(denied_database=database_name):
                if datallowconn:
                    with self.assertRaises(psycopg.OperationalError) as denied:
                        self._connect_as(self.ATTESTOR_ROLE, database_name)
                    self.assertIn("permission denied for database", str(denied.exception))
                    self.assertIn("CONNECT privilege", str(denied.exception))
                else:
                    # template0 is independently non-connectable; the ACL check above
                    # still proves the attestor has no effective CONNECT privilege.
                    with self.assertRaises(psycopg.Error):
                        self._connect_as(self.ATTESTOR_ROLE, database_name)

    def test_future_database_creation_requires_a_pre_connect_acl_guard(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            try:
                self.created_databases.append(self.FUTURE_UNGUARDED_DATABASE)
                admin.execute(
                    sql.SQL("CREATE DATABASE {}").format(
                        sql.Identifier(self.FUTURE_UNGUARDED_DATABASE)
                    )
                )
                self.assertTrue(
                    self._has_database_privilege(
                        admin, self.ATTESTOR_ROLE, self.FUTURE_UNGUARDED_DATABASE
                    )
                )
                with self._connect_as(
                    self.ATTESTOR_ROLE, self.FUTURE_UNGUARDED_DATABASE
                ) as conn:
                    self.assertEqual(
                        conn.execute("SELECT current_database()").fetchone()[0],
                        self.FUTURE_UNGUARDED_DATABASE,
                    )
                admin.execute(
                    sql.SQL("DROP DATABASE {}").format(
                        sql.Identifier(self.FUTURE_UNGUARDED_DATABASE)
                    )
                )

                self.created_databases.append(self.FUTURE_GUARDED_DATABASE)
                admin.execute(
                    sql.SQL("CREATE DATABASE {} WITH ALLOW_CONNECTIONS false").format(
                        sql.Identifier(self.FUTURE_GUARDED_DATABASE)
                    )
                )
                admin.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                        sql.Identifier(self.FUTURE_GUARDED_DATABASE)
                    )
                )
                admin.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(self.FUTURE_GUARDED_DATABASE),
                        sql.Identifier(self.APP_GROUP_ROLE),
                    )
                )
                admin.execute(
                    sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(
                        sql.Identifier(self.FUTURE_GUARDED_DATABASE)
                    )
                )

                self.assertFalse(
                    self._has_database_privilege(
                        admin, self.ATTESTOR_ROLE, self.FUTURE_GUARDED_DATABASE
                    )
                )
                self.assertTrue(
                    self._has_database_privilege(
                        admin, self.APP_RUNTIME_ROLE, self.FUTURE_GUARDED_DATABASE
                    )
                )
                with self._connect_as(
                    self.APP_RUNTIME_ROLE, self.FUTURE_GUARDED_DATABASE
                ) as conn:
                    self.assertEqual(
                        conn.execute("SELECT current_database()").fetchone()[0],
                        self.FUTURE_GUARDED_DATABASE,
                    )
                with self.assertRaises(psycopg.OperationalError) as denied:
                    self._connect_as(
                        self.ATTESTOR_ROLE, self.FUTURE_GUARDED_DATABASE
                    )
                self.assertIn(
                    "permission denied for database", str(denied.exception)
                )
                self.assertIn("CONNECT privilege", str(denied.exception))
            finally:
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(self.FUTURE_UNGUARDED_DATABASE)
                    )
                )
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(self.FUTURE_GUARDED_DATABASE)
                    )
                )

    def test_public_is_removed_but_application_access_paths_are_preserved(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as admin:
            for database_name, datallowconn in self.database_inventory:
                if database_name == self.TARGET_DATABASE:
                    continue
                with self.subTest(database=database_name):
                    self.assertFalse(self._public_connect(admin, database_name))
                    self.assertFalse(
                        self._has_database_privilege(
                            admin, self.ATTESTOR_ROLE, database_name
                        )
                    )
                    if not datallowconn:
                        continue

                    self.assertTrue(
                        self._has_database_privilege(
                            admin, self.APP_RUNTIME_ROLE, database_name
                        )
                    )
                    self.assertFalse(
                        self._has_acl_grant(
                            admin, self.APP_RUNTIME_ROLE, database_name
                        )
                    )
                    self.assertTrue(
                        admin.execute(
                            "SELECT pg_catalog.pg_has_role(%s, %s, 'USAGE')",
                            (self.APP_RUNTIME_ROLE, self.APP_GROUP_ROLE),
                        ).fetchone()[0]
                    )
                    self.assertTrue(
                        self._has_acl_grant(
                            admin, self.MIGRATION_ROLE, database_name
                        )
                    )
                    self.assertTrue(
                        self._has_acl_grant(
                            admin, self.LEGACY_APP_ROLE, database_name
                        )
                    )

        for role_name in (
            self.APP_RUNTIME_ROLE,
            self.MIGRATION_ROLE,
            self.LEGACY_APP_ROLE,
        ):
            for database_name, datallowconn in self.database_inventory:
                if database_name == self.TARGET_DATABASE or not datallowconn:
                    continue
                with (
                    self.subTest(role=role_name, database=database_name),
                    self._connect_as(role_name, database_name) as conn,
                ):
                    self.assertEqual(
                        conn.execute("SELECT current_database()").fetchone()[0],
                        database_name,
                    )

    def test_attestor_has_no_role_escalation_path(self):
        with self._connect_as(self.ATTESTOR_ROLE, self.TARGET_DATABASE) as conn:
            # Disable the read-only GUC so CREATE/DROP denials prove role privileges,
            # not the session default.
            conn.execute("SET default_transaction_read_only = off")
            attrs = conn.execute(
                """
                SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
                       rolreplication, rolbypassrls, rolinherit
                FROM pg_catalog.pg_roles
                WHERE rolname = current_user
                """
            ).fetchone()
            self.assertEqual(attrs, (True, False, False, False, False, False, False))

            self.assertEqual(
                conn.execute(
                    """
                    SELECT count(*)
                    FROM pg_catalog.pg_auth_members AS m
                    JOIN pg_catalog.pg_roles AS member ON member.oid = m.member
                    WHERE member.rolname = current_user
                    """
                ).fetchone()[0],
                0,
            )
            self.assertFalse(
                conn.execute(
                    "SELECT pg_catalog.pg_has_role(current_user, 'pg_monitor', 'MEMBER')"
                ).fetchone()[0]
            )
            for role_name in (
                "postgres",
                self.APP_GROUP_ROLE,
                self.APP_RUNTIME_ROLE,
                self.MIGRATION_ROLE,
                self.LEGACY_APP_ROLE,
            ):
                with (
                    self.subTest(set_role=role_name),
                    self.assertRaises(psycopg.errors.InsufficientPrivilege),
                ):
                    conn.execute(
                        sql.SQL("SET ROLE {}").format(sql.Identifier(role_name))
                    )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SET SESSION AUTHORIZATION postgres")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    sql.SQL("DROP DATABASE {}").format(
                        sql.Identifier(self.GUARD_DATABASE)
                    )
                )
            self.created_databases.append(self.UNEXPECTED_DATABASE)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    sql.SQL("CREATE DATABASE {}").format(
                        sql.Identifier(self.UNEXPECTED_DATABASE)
                    )
                )


if __name__ == "__main__":
    unittest.main()
