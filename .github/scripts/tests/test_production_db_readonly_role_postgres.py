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
