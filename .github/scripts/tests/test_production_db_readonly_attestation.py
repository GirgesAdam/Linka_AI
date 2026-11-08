from __future__ import annotations

# isort: skip_file

import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace
from unittest.mock import patch


module_path = Path(__file__).resolve().parents[1] / "production_db_readonly_attestation.py"
probe = SimpleNamespace(**run_path(str(module_path)))


RAW_URL = (
    "postgresql+psycopg://attestor:do-not-leak@example-1-eu-west-3.pooler.supabase.com:"
    "5432/postgres?sslmode=disable"
)


class FakeCursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(
        self,
        *,
        system_id=None,
        tls=True,
        fail_system_id=False,
        database_oid="5",
        is_replica=False,
        session_user="tia_production_attestor",
        current_user=None,
        can_login=True,
        is_superuser=False,
        can_create_db=False,
        can_create_role=False,
        can_replicate=False,
        bypass_rls=False,
        inherit_roles=False,
        membership_count=0,
        default_transaction_read_only="on",
        public_schema_create=False,
        control_effective_execute=False,
        control_direct_execute=False,
        wrapper_schema_usage=True,
        wrapper_effective_execute=True,
        wrapper_public_execute=False,
        wrapper_security_definer=True,
        wrapper_owner="postgres",
        wrapper_argument_count=0,
        wrapper_config=None,
        security_definer_rows=None,
    ):
        self.system_id = system_id or probe.REFERENCE["cluster_system_id"]
        self.tls = tls
        self.fail_system_id = fail_system_id
        self.database_oid = database_oid
        self.is_replica = is_replica
        self.session_user = session_user
        self.current_user = current_user or session_user
        self.role_attributes = (
            can_login,
            is_superuser,
            can_create_db,
            can_create_role,
            can_replicate,
            bypass_rls,
            inherit_roles,
            membership_count,
            default_transaction_read_only,
            public_schema_create,
            control_effective_execute,
            control_direct_execute,
            wrapper_schema_usage,
            wrapper_effective_execute,
            wrapper_public_execute,
            wrapper_security_definer,
            wrapper_owner,
            wrapper_argument_count,
            wrapper_config if wrapper_config is not None else ["search_path=pg_catalog"],
        )
        self.security_definer_rows = (
            security_definer_rows
            if security_definer_rows is not None
            else [("attestation_private", "cluster_system_identifier", 0, "")]
        )
        self.wrapper_invocations = 0
        self.info = SimpleNamespace(backend_pid=4242)
        self.queries = []
        self.closed = False

    def execute(self, query):
        normalized = " ".join(query.split())
        self.queries.append(normalized)

        if normalized == "BEGIN READ ONLY":
            return FakeCursor()
        if normalized.startswith("SET LOCAL statement_timeout"):
            return FakeCursor()
        if normalized.startswith(
            "SELECT session_user::text, current_user::text, attestor_role.rolcanlogin"
        ):
            return FakeCursor(
                (
                    self.session_user,
                    self.current_user,
                    *self.role_attributes,
                )
            )
        if normalized.startswith("SELECT namespace.nspname, procedure.proname"):
            return FakeCursor(rows=self.security_definer_rows)
        if normalized.startswith("SELECT current_database()"):
            return FakeCursor(
                (
                    "postgres",
                    "public",
                    '"$user", public',
                    "17.6",
                    self.database_oid,
                    4242,
                    self.is_replica,
                    "on",
                )
            )
        if normalized.startswith("SELECT attestation_private.cluster_system_identifier()"):
            self.wrapper_invocations += 1
            if self.fail_system_id:
                raise probe.psycopg.errors.InsufficientPrivilege("do-not-leak")
            return FakeCursor((self.system_id,))
        if normalized.startswith("SELECT version_num"):
            return FakeCursor(rows=[("0096_patient_customer_id",)])
        if normalized.startswith("SELECT ssl, version, cipher, bits"):
            if self.tls:
                return FakeCursor((True, "TLSv1.3", "TLS_AES_256_GCM_SHA384", 256))
            return FakeCursor((False, None, None, None))
        if normalized.startswith("SELECT pg_backend_pid(), current_setting"):
            return FakeCursor((4242, "on"))
        if normalized == "ROLLBACK":
            return FakeCursor()
        raise AssertionError(f"Unexpected SQL in test: {normalized}")

    def close(self):
        self.closed = True


class ConnectionDetailsTests(unittest.TestCase):
    def test_sqlalchemy_url_parser_forces_full_tls_verification(self):
        conninfo, mode = probe.connection_details(RAW_URL)
        self.assertIn("sslmode=verify-full", conninfo)
        self.assertIn("sslrootcert=system", conninfo)
        self.assertIn("gssencmode=disable", conninfo)
        self.assertNotIn("sslmode=disable", conninfo)
        self.assertEqual(mode, "Supavisor session pooler")

    def test_connection_modes_do_not_expose_hosts(self):
        cases = (
            (
                "postgresql+psycopg://u:p@aws-1.pooler.supabase.com:6543/postgres",
                "Supavisor transaction pooler",
            ),
            (
                "postgresql+psycopg://u:p@aws-1.pooler.supabase.com:5432/postgres",
                "Supavisor session pooler",
            ),
            (
                "postgresql+psycopg://u:p@db.example.supabase.co:5432/postgres",
                "Supabase direct connection",
            ),
        )
        for raw_url, expected in cases:
            with self.subTest(expected=expected):
                _, actual = probe.connection_details(raw_url)
                self.assertEqual(actual, expected)

    def test_unsupported_driver_fails_before_connecting(self):
        with patch.object(probe.psycopg, "connect") as connect:
            result = probe.run_attestation(
                "postgresql+asyncpg://u:p@db.example.supabase.co/postgres"
            )
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], "unsupported_postgresql_driver")
        connect.assert_not_called()


class RuntimeAttestationTests(unittest.TestCase):
    def _run_with_connection(self, connection):
        connect = patch.object(probe.psycopg, "connect", return_value=connection)
        mocked = connect.start()
        self.addCleanup(connect.stop)
        result = probe.run_attestation(RAW_URL)
        return result, mocked

    def _run_main_with_connection(self, connection, raw_url=RAW_URL):
        output = StringIO()
        with (
            patch.object(probe.psycopg, "connect", return_value=connection),
            patch.dict("os.environ", {"PRODUCTION_DB_READONLY_URL": raw_url}),
            redirect_stdout(output),
        ):
            exit_code = probe.main()
        return exit_code, json.loads(output.getvalue())

    def _assert_no_database_writes(self, connection):
        write_prefixes = (
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "MERGE ",
            "CREATE ",
            "ALTER ",
            "DROP ",
            "TRUNCATE ",
            "GRANT ",
            "REVOKE ",
            "CALL ",
            "DO ",
            "COPY ",
        )
        writes = [
            query
            for query in connection.queries
            if query.lstrip().upper().startswith(write_prefixes)
        ]
        self.assertEqual(writes, [])

    def test_success_uses_one_connection_one_read_only_transaction_and_rollback(self):
        connection = FakeConnection()
        result, connect = self._run_with_connection(connection)

        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["verdict"], "SAME_DATABASE")
        self.assertTrue(result["baseline_metadata_match"])
        self.assertEqual(result["connection_count"], 1)
        self.assertEqual(result["backend_pid"], 4242)
        self.assertTrue(result["single_transaction"])
        self.assertTrue(result["transaction_read_only"])
        self.assertTrue(result["runtime_identity_verified"])
        self.assertTrue(result["security_definer_surface_verified"])
        self.assertTrue(result["rollback_completed"])
        self.assertEqual(connection.wrapper_invocations, 1)
        identity_query_index = next(
            index for index, query in enumerate(connection.queries)
            if query.startswith("SELECT session_user::text")
        )
        wrapper_query_index = next(
            index for index, query in enumerate(connection.queries)
            if query.startswith("SELECT attestation_private.cluster_system_identifier()")
        )
        inventory_query_index = next(
            index for index, query in enumerate(connection.queries)
            if query.startswith("SELECT namespace.nspname, procedure.proname")
        )
        self.assertLess(identity_query_index, inventory_query_index)
        self.assertLess(inventory_query_index, wrapper_query_index)
        self.assertTrue(result["tls"]["enabled"])
        self.assertEqual(result["tls"]["certificate_verification"], "verify-full")
        self.assertEqual(result["connection_mode"], "Supavisor session pooler")
        connect.assert_called_once()
        self.assertEqual(connect.call_args.kwargs["autocommit"], True)
        self.assertEqual(connection.queries.count("BEGIN READ ONLY"), 1)
        self.assertEqual(connection.queries.count("ROLLBACK"), 1)
        self.assertFalse(any(query == "COMMIT" for query in connection.queries))
        self.assertTrue(connection.closed)
        self._assert_no_database_writes(connection)
        serialized = json.dumps(result)
        self.assertNotIn("do-not-leak", serialized)
        self.assertNotIn("attestor", serialized)
        self.assertNotIn("example-1-eu-west-3", serialized)

    def test_primary_connection_returns_zero_exit_code(self):
        connection = FakeConnection(is_replica=False)
        exit_code, result = self._run_main_with_connection(connection)

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["verdict"], "SAME_DATABASE")
        self.assertFalse(result["is_replica"])
        self.assertEqual(connection.queries.count("ROLLBACK"), 1)
        self.assertTrue(connection.closed)
        self._assert_no_database_writes(connection)

    def _assert_runtime_guard_rejects_without_wrapper(self, connection, category):
        result, connect = self._run_with_connection(connection)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], category)
        self.assertEqual(connection.wrapper_invocations, 0)
        self.assertFalse(
            any(
                query.startswith("SELECT attestation_private.cluster_system_identifier()")
                for query in connection.queries
            )
        )
        self.assertEqual(connection.queries.count("ROLLBACK"), 1)
        self.assertTrue(connection.closed)
        connect.assert_called_once()
        serialized = json.dumps(result)
        self.assertNotIn("tia_production_attestor", serialized)
        self.assertNotIn("postgres", serialized)
        self.assertNotIn("do-not-leak", serialized)

    def test_dedicated_attestor_identity_is_verified_before_wrapper(self):
        connection = FakeConnection()
        result, _ = self._run_with_connection(connection)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(connection.wrapper_invocations, 1)
        self.assertTrue(result["runtime_identity_verified"])

    def test_session_user_postgres_is_rejected_before_wrapper(self):
        self._assert_runtime_guard_rejects_without_wrapper(
            FakeConnection(session_user="postgres", current_user="postgres"),
            "runtime_identity_mismatch",
        )

    def test_current_user_mismatch_is_rejected_before_wrapper(self):
        self._assert_runtime_guard_rejects_without_wrapper(
            FakeConnection(current_user="unexpected_role"),
            "runtime_identity_mismatch",
        )

    def test_superuser_credentials_are_rejected_before_wrapper(self):
        self._assert_runtime_guard_rejects_without_wrapper(
            FakeConnection(
                session_user="tia_production_attestor",
                current_user="tia_production_attestor",
                is_superuser=True,
            ),
            "runtime_role_attributes_unverified",
        )

    def test_createrole_createdb_and_bypassrls_credentials_are_rejected(self):
        cases = (
            ("NOLOGIN", {"can_login": False}),
            ("CREATEROLE", {"can_create_role": True}),
            ("CREATEDB", {"can_create_db": True}),
            ("REPLICATION", {"can_replicate": True}),
            ("BYPASSRLS", {"bypass_rls": True}),
        )
        for label, attributes in cases:
            with self.subTest(attribute=label):
                self._assert_runtime_guard_rejects_without_wrapper(
                    FakeConnection(**attributes),
                    "runtime_role_attributes_unverified",
                )

    def test_wrapper_security_definer_owner_arguments_and_search_path_are_enforced(self):
        cases = (
            ("not_security_definer", {"wrapper_security_definer": False}),
            ("wrong_owner", {"wrapper_owner": "other_owner"}),
            ("has_arguments", {"wrapper_argument_count": 1}),
            ("unsafe_search_path", {"wrapper_config": ["search_path=public"]}),
        )
        for label, metadata in cases:
            with self.subTest(wrapper_metadata=label):
                self._assert_runtime_guard_rejects_without_wrapper(
                    FakeConnection(**metadata),
                    "runtime_effective_privilege_guard_failed",
                )

    def test_unexpected_role_membership_is_rejected_before_wrapper(self):
        self._assert_runtime_guard_rejects_without_wrapper(
            FakeConnection(membership_count=1),
            "runtime_role_attributes_unverified",
        )

    def test_direct_pg_control_system_execute_is_rejected_before_wrapper(self):
        self._assert_runtime_guard_rejects_without_wrapper(
            FakeConnection(
                control_effective_execute=True,
                control_direct_execute=True,
            ),
            "runtime_effective_privilege_guard_failed",
        )

    def test_unexpected_security_definer_surface_is_rejected_without_invocation(self):
        self._assert_runtime_guard_rejects_without_wrapper(
            FakeConnection(
                security_definer_rows=[
                    ("attestation_private", "cluster_system_identifier", 0, ""),
                    ("public", "unexpected_definer", 0, ""),
                ]
            ),
            "unexpected_security_definer_surface",
        )

    def test_missing_wrapper_privileges_are_unverified_before_invocation(self):
        self._assert_runtime_guard_rejects_without_wrapper(
            FakeConnection(wrapper_effective_execute=False),
            "runtime_effective_privilege_guard_failed",
        )

    def test_migration_uri_misconfiguration_rejects_postgres_even_when_wrapper_is_callable(self):
        connection = FakeConnection(
            session_user="postgres",
            current_user="postgres",
            wrapper_schema_usage=True,
            wrapper_effective_execute=True,
        )
        migration_url = (
            "postgresql+psycopg://postgres:must-not-be-logged@"
            "db.example.supabase.co:5432/postgres"
        )
        exit_code, result = self._run_main_with_connection(
            connection, raw_url=migration_url
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], "runtime_identity_mismatch")
        self.assertEqual(connection.wrapper_invocations, 0)
        self.assertEqual(connection.queries.count("ROLLBACK"), 1)
        self.assertTrue(connection.closed)
        serialized = json.dumps(result)
        self.assertNotIn("postgres", serialized)
        self.assertNotIn("must-not-be-logged", serialized)
        self.assertNotIn("db.example.supabase.co", serialized)

    def test_default_transaction_read_only_off_is_rejected_before_wrapper(self):
        self._assert_runtime_guard_rejects_without_wrapper(
            FakeConnection(default_transaction_read_only="off"),
            "runtime_role_attributes_unverified",
        )

    def test_public_schema_create_is_rejected_before_wrapper(self):
        self._assert_runtime_guard_rejects_without_wrapper(
            FakeConnection(public_schema_create=True),
            "runtime_effective_privilege_guard_failed",
        )

    def test_wrapper_public_execute_is_rejected_before_wrapper(self):
        self._assert_runtime_guard_rejects_without_wrapper(
            FakeConnection(wrapper_public_execute=True),
            "runtime_effective_privilege_guard_failed",
        )

    def test_read_replica_is_unverified_and_cannot_be_same_database(self):
        connection = FakeConnection(is_replica=True)
        result, connect = self._run_with_connection(connection)

        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], "connected_to_read_replica")
        self.assertNotIn("verdict", result)
        self.assertNotIn("cluster_system_id", result)
        connect.assert_called_once()
        self.assertEqual(connection.queries.count("ROLLBACK"), 1)
        self.assertEqual(connection.wrapper_invocations, 0)
        self.assertFalse(
            any(
                query.startswith("SELECT pg_catalog.pg_control_system()")
                for query in connection.queries
            )
        )
        self.assertTrue(connection.closed)
        self._assert_no_database_writes(connection)
        serialized = json.dumps(result)
        self.assertNotIn("do-not-leak", serialized)
        self.assertNotIn("attestor", serialized)
        self.assertNotIn("example-1-eu-west-3", serialized)

    def test_unknown_replica_state_is_unverified_and_rolls_back(self):
        connection = FakeConnection(is_replica=None)
        result, connect = self._run_with_connection(connection)

        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], "replica_status_unavailable")
        self.assertNotIn("verdict", result)
        connect.assert_called_once()
        self.assertEqual(connection.queries.count("ROLLBACK"), 1)
        self.assertEqual(connection.wrapper_invocations, 0)
        self.assertFalse(
            any(
                query.startswith("SELECT pg_catalog.pg_control_system()")
                for query in connection.queries
            )
        )
        self.assertTrue(connection.closed)
        self._assert_no_database_writes(connection)

    def test_read_replica_main_returns_nonzero_and_rolls_back(self):
        connection = FakeConnection(is_replica=True)
        exit_code, result = self._run_main_with_connection(connection)

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], "connected_to_read_replica")
        self.assertNotIn("verdict", result)
        self.assertEqual(connection.queries.count("ROLLBACK"), 1)
        self.assertTrue(connection.closed)
        self._assert_no_database_writes(connection)
        serialized = json.dumps(result)
        self.assertNotIn("do-not-leak", serialized)
        self.assertNotIn("attestor", serialized)
        self.assertNotIn("example-1-eu-west-3", serialized)

    def test_tls_failure_is_unverified_and_transaction_is_rolled_back(self):
        connection = FakeConnection(tls=False)
        result, connect = self._run_with_connection(connection)

        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], "tls_not_verified")
        connect.assert_called_once()
        self.assertEqual(connection.queries.count("ROLLBACK"), 1)
        self.assertTrue(connection.closed)
        self._assert_no_database_writes(connection)

    def test_cluster_system_id_permission_failure_does_not_retry_or_escalate(self):
        connection = FakeConnection(fail_system_id=True)
        result, connect = self._run_with_connection(connection)

        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], "database_permission_denied")
        self.assertEqual(result["error_type"], "InsufficientPrivilege")
        self.assertEqual(result["sqlstate"], "42501")
        connect.assert_called_once()
        self.assertEqual(connection.queries.count("ROLLBACK"), 1)
        self.assertTrue(connection.closed)
        self._assert_no_database_writes(connection)
        serialized = json.dumps(result)
        self.assertNotIn("do-not-leak", serialized)
        self.assertNotIn("attestor", serialized)
        self.assertNotIn("example-1-eu-west-3", serialized)

    def test_different_cluster_is_reported_as_different_database(self):
        connection = FakeConnection(system_id="1111111111111111111")
        result, _ = self._run_with_connection(connection)

        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["verdict"], "DIFFERENT_DATABASE")
        self.assertTrue(result["baseline_metadata_match"])

    def test_different_database_oid_is_reported_as_different_database(self):
        connection = FakeConnection(database_oid="6")
        result, _ = self._run_with_connection(connection)

        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["verdict"], "DIFFERENT_DATABASE")

    def test_missing_readonly_secret_is_unverified_without_connecting(self):
        with patch.object(probe.psycopg, "connect") as connect:
            result = probe.run_attestation("")
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], "production_db_readonly_url_missing")
        connect.assert_not_called()

    def test_migration_secret_is_never_used_as_attestation_fallback(self):
        with (
            patch.object(probe.psycopg, "connect") as connect,
            patch.dict("os.environ", {"MIGRATION_DATABASE_URL": RAW_URL}),
        ):
            os.environ.pop("PRODUCTION_DB_READONLY_URL", None)
            result = probe.run_attestation()
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], "production_db_readonly_url_missing")
        connect.assert_not_called()

    def test_readonly_secret_wins_when_both_credentials_are_present(self):
        connection = FakeConnection()
        migration_url = (
            "postgresql+psycopg://migration:must-not-be-used@migration.invalid:"
            "5432/postgres"
        )
        expected_conninfo, _ = probe.connection_details(RAW_URL)
        with (
            patch.object(probe.psycopg, "connect", return_value=connection) as connect,
            patch.dict(
                "os.environ",
                {
                    "PRODUCTION_DB_READONLY_URL": RAW_URL,
                    "MIGRATION_DATABASE_URL": migration_url,
                },
            ),
        ):
            result = probe.run_attestation()

        self.assertEqual(result["status"], "VERIFIED")
        connect.assert_called_once()
        self.assertEqual(connect.call_args.args[0], expected_conninfo)
        self.assertNotIn("migration.invalid", connect.call_args.args[0])
        self.assertNotIn("must-not-be-used", connect.call_args.args[0])


class WorkflowContractTests(unittest.TestCase):
    def test_runtime_workflow_is_dispatch_only_and_read_only(self):
        workflow_path = (
            Path(__file__).resolve().parents[2]
            / "workflows"
            / "production-db-readonly-attestation.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")
        trigger_block = workflow.split("permissions:", maxsplit=1)[0]

        self.assertIn("workflow_dispatch:", trigger_block)
        self.assertNotIn("\n  pull_request:", trigger_block)
        self.assertNotIn("\n  push:", trigger_block)
        self.assertNotIn("\n  schedule:", trigger_block)
        self.assertIn("name: production-attestation", workflow)
        self.assertIn("secrets.PRODUCTION_DB_READONLY_URL", workflow)
        self.assertEqual(workflow.count("secrets.PRODUCTION_DB_READONLY_URL"), 1)
        self.assertNotIn("secrets.MIGRATION_DATABASE_URL", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("alembic upgrade", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertNotIn("\n          DATABASE_URL:", workflow)

        action_refs = [
            line.strip().split("uses:", maxsplit=1)[1].strip()
            for line in workflow.splitlines()
            if "uses:" in line
        ]
        self.assertTrue(action_refs)
        for action_ref in action_refs:
            revision = action_ref.split("@", maxsplit=1)[1].split(" ", maxsplit=1)[0]
            self.assertEqual(len(revision), 40)
            self.assertTrue(all(character in "0123456789abcdef" for character in revision))


if __name__ == "__main__":
    unittest.main()
