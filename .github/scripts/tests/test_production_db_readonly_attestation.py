from __future__ import annotations

# isort: skip_file

import json
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
    ):
        self.system_id = system_id or probe.REFERENCE["cluster_system_id"]
        self.tls = tls
        self.fail_system_id = fail_system_id
        self.database_oid = database_oid
        self.is_replica = is_replica
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
        if normalized.startswith("SELECT system_identifier::text"):
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

    def _run_main_with_connection(self, connection):
        output = StringIO()
        with (
            patch.object(probe.psycopg, "connect", return_value=connection),
            patch.dict("os.environ", {"MIGRATION_DATABASE_URL": RAW_URL}),
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
        self.assertTrue(result["rollback_completed"])
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

    def test_read_replica_is_unverified_and_cannot_be_same_database(self):
        connection = FakeConnection(is_replica=True)
        result, connect = self._run_with_connection(connection)

        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], "connected_to_read_replica")
        self.assertNotIn("verdict", result)
        self.assertNotIn("cluster_system_id", result)
        connect.assert_called_once()
        self.assertEqual(connection.queries.count("ROLLBACK"), 1)
        self.assertFalse(any("pg_control_system()" in query for query in connection.queries))
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
        self.assertFalse(any("pg_control_system()" in query for query in connection.queries))
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

    def test_missing_secret_is_unverified_without_connecting(self):
        with patch.object(probe.psycopg, "connect") as connect:
            result = probe.run_attestation("")
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["failure_category"], "migration_database_url_missing")
        connect.assert_not_called()


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
        self.assertIn("name: production", workflow)
        self.assertIn("secrets.MIGRATION_DATABASE_URL", workflow)
        self.assertEqual(workflow.count("secrets.MIGRATION_DATABASE_URL"), 1)
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
