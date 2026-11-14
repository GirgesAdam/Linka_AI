"""Offline fail-closed tests; never supply production secrets or connect to a database."""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

MODULE_FILE = Path(__file__).resolve().parents[1] / "production_migration_0098_credential_probe.py"
spec = importlib.util.spec_from_file_location("probe0098", MODULE_FILE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Result:
    def __init__(self, one=None, all_rows=None):
        self.one = one
        self.all_rows = all_rows

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all_rows


class FakeConnection:
    def __init__(self, readonly="on", revision=probe.EXPECTED_REVISION,
                 tls=True, bad_query=False, rollback_fails=False):
        self.readonly = readonly
        self.revision = revision
        self.tls = tls
        self.bad_query = bad_query
        self.rollback_fails = rollback_fails
        self.queries = []

    def execute(self, sql):
        self.queries.append(sql)
        if sql == "ROLLBACK" and self.rollback_fails:
            raise RuntimeError("simulated secret-bearing rollback exception")
        if self.bad_query and sql.startswith("SELECT"):
            raise RuntimeError("simulated secret-bearing connection exception")
        if "server_version_num" in sql:
            return Result(("postgres", 170006, self.readonly, False,
                           "postgres.ycuxjlkhnubztgqmhtom"))
        if "pg_database" in sql:
            return Result(("5",))
        if "pg_namespace" in sql:
            return Result(("public",))
        if "alembic_version" in sql:
            return Result([(self.revision,)][0], [(self.revision,)])
        if "pg_stat_ssl" in sql:
            return Result((self.tls, "TLSv1.3"))
        return Result()


class CredentialProbeTests(unittest.TestCase):
    def test_source_guard_requires_main_and_first_attempt(self):
        with patch.dict("os.environ", {"GITHUB_REF": "refs/heads/feature",
                                       "GITHUB_RUN_ATTEMPT": "1"}, clear=True):
            with self.assertRaisesRegex(probe.ProbeFailure, "SOURCE_GUARD_FAILED"):
                probe.source_guard(Path("."))

    def test_expected_hash_rejects_drift(self):
        with self.assertRaisesRegex(probe.ProbeFailure, "MIGRATION_HASH_MISMATCH"):
            probe.check_migration_hash(b"unapproved migration change")

    def test_missing_ca_is_rejected_before_file_creation(self):
        with self.assertRaisesRegex(probe.ProbeFailure, "CA_CERT_MISSING"):
            probe.ca_file(None, "", Path("."))

    def test_readonly_probe_is_rollback_only(self):
        c = FakeConnection()
        probe.inspect_readonly(c)
        self.assertEqual(c.queries[0], "BEGIN READ ONLY")
        self.assertEqual(c.queries[-1], "ROLLBACK")
        self.assertFalse(any(q.strip().upper().startswith(("UPDATE ", "INSERT ", "CREATE ", "ALTER ", "DROP ", "DELETE "))
                             for q in c.queries))

    def test_wrong_revision_fails_and_rolls_back(self):
        c = FakeConnection(revision="0098_doctor_laser_weekday_exclusions")
        with self.assertRaisesRegex(probe.ProbeFailure, "REVISION_MISMATCH"):
            probe.inspect_readonly(c)
        self.assertEqual(c.queries[-1], "ROLLBACK")

    def test_readonly_off_fails_and_rolls_back(self):
        c = FakeConnection(readonly="off")
        with self.assertRaisesRegex(probe.ProbeFailure, "READ_ONLY_NOT_CONFIRMED"):
            probe.inspect_readonly(c)
        self.assertEqual(c.queries[-1], "ROLLBACK")

    def test_tls_missing_fails_and_rolls_back(self):
        c = FakeConnection(tls=False)
        with self.assertRaisesRegex(probe.ProbeFailure, "TLS_NOT_CONFIRMED"):
            probe.inspect_readonly(c)
        self.assertEqual(c.queries[-1], "ROLLBACK")

    def test_exception_during_select_rolls_back(self):
        c = FakeConnection(bad_query=True)
        with self.assertRaises(RuntimeError):
            probe.inspect_readonly(c)
        self.assertEqual(c.queries[-1], "ROLLBACK")

    def test_rollback_failure_is_fail_closed(self):
        c = FakeConnection(rollback_fails=True)
        with self.assertRaisesRegex(probe.ProbeFailure, "ROLLBACK_FAILED"):
            probe.inspect_readonly(c)

    def test_main_does_not_log_unknown_exception(self):
        stderr = io.StringIO()
        with patch.object(probe, "probe", side_effect=RuntimeError("password top-secret")), \
                patch.dict("os.environ", {"GITHUB_WORKSPACE": "."}, clear=True), \
                patch.object(sys, "argv", ["script", "probe"]), redirect_stderr(stderr):
            self.assertEqual(probe.main(), 1)
        self.assertEqual(stderr.getvalue().strip(),
                         "::error::0098_COMPATIBILITY_DATABASE_CONNECTION_OR_QUERY_FAILED")


if __name__ == "__main__":
    unittest.main()
