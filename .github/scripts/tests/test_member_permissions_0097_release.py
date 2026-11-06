"""Deterministic release guard tests; the optional DB suite uses isolated PG only."""
import importlib.util
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "member_permissions_0097_release",
    HERE / "member_permissions_0097_release.py",
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class GuardTests(unittest.TestCase):
    def test_exact_ddl_allowlist(self):
        self.assertTrue(release.allowed_ddl(
            "ALTER TABLE workspace_members ADD COLUMN page_permissions JSONB"
        ))
        self.assertTrue(release.allowed_ddl(
            "ALTER TABLE workspace_members ADD COLUMN permissions_version "
            "INTEGER DEFAULT 0 NOT NULL"
        ))
        for sql in (
            "DROP TABLE workspace_members",
            "ALTER TABLE workspace_members DROP COLUMN role",
            "ALTER TABLE workspace_members ADD COLUMN debug TEXT",
            "ALTER TABLE patients ADD COLUMN page_permissions JSONB",
            "ALTER TABLE workspace_members ADD COLUMN page_permissions JSONB; DROP TABLE users",
        ):
            with self.subTest(sql=sql):
                self.assertFalse(release.allowed_ddl(sql))

    def test_version_update_only(self):
        sql = (
            "UPDATE alembic_version SET version_num=%(version_num)s "
            "WHERE alembic_version.version_num = %(version_num_1)s"
        )
        self.assertTrue(release.version_write(sql, {
            "version_num": release.TO_REV,
            "version_num_1": release.FROM_REV,
        }))
        rendered = (
            f"UPDATE alembic_version SET version_num='{release.TO_REV}' "
            f"WHERE alembic_version.version_num = '{release.FROM_REV}'"
        )
        self.assertTrue(release.version_write(rendered, {}))
        self.assertFalse(release.version_write(
            rendered.replace(release.TO_REV, "9999_unapproved"), {}
        ))
        self.assertFalse(release.version_write(sql, {
            "version_num": "0100_wrong", "version_num_1": release.FROM_REV
        }))
        self.assertFalse(release.version_write(
            "UPDATE patients SET role=%(role)s WHERE id=%(id)s",
            (release.TO_REV, release.FROM_REV),
        ))

    def make_freeze(self, *, start_offset=-2, end_offset=8,
                    status="ACTIVE", workflow_sha="a" * 40):
        now = datetime.now(UTC)
        body = {
            "protocol": release.PROTOCOL, "status": status,
            "pr": 306, "operator": release.OWNER,
            "permissions_sha": release.APPROVED_SHA,
            "workflow_sha": workflow_sha,
            "project_ref": release.PROJECT_REF,
            "database": "postgres", "migration": release.TO_REV,
            "freeze_id": "LINKA-306-0097-TEST12345",
            "start_utc": (now + timedelta(minutes=start_offset)).isoformat(),
            "end_utc": (now + timedelta(minutes=end_offset)).isoformat(),
            "coordination": {
                "github_merges": "FROZEN",
                "github_migrations": "FROZEN",
                "railway_deploys": "FROZEN",
                "other_migration_runners": "FROZEN",
                "clinics_remain_online": True,
            },
        }
        return body, now

    def test_freeze_accepted(self):
        freeze, now = self.make_freeze()
        result = release.freeze_contract(
            json.dumps(freeze), release.OWNER, now.isoformat(),
            workflow_sha="a" * 40, now=now
        )
        self.assertEqual(result["status"], "ACTIVE")

    def test_freeze_missing_expired_wrong_signer_wrong_sha(self):
        for change in ("expired", "missing", "owner", "sha", "inactive",
                       "coordination"):
            freeze, now = self.make_freeze()
            owner = release.OWNER
            if change == "expired":
                freeze["end_utc"] = (now-timedelta(minutes=1)).isoformat()
            elif change == "missing":
                freeze["start_utc"] = ""
            elif change == "owner":
                owner = "unknown"
            elif change == "sha":
                freeze["permissions_sha"] = "0"*40
            elif change == "inactive":
                freeze["status"] = "CLOSED"
            else:
                freeze["coordination"]["github_migrations"] = "UNCONFIRMED"
            with self.subTest(reason=change), self.assertRaises(release.Blocked):
                release.freeze_contract(
                    json.dumps(freeze), owner, now.isoformat(),
                    workflow_sha="a"*40, now=now
                )

    def test_wrong_target_denied(self):
        for target in (
            "postgresql+psycopg://postgres.wrong:private@aws-0-xx.pooler.supabase.com:5432/postgres",
            "postgresql+psycopg://postgres:private@db.wrong.supabase.co:5432/postgres",
            "postgresql+psycopg://postgres:private@127.0.0.1:5432/postgres",
            "postgresql+psycopg://postgres.release:private@aws-0-xx.pooler.supabase.com:6543/postgres",
            "postgresql+psycopg://postgres.release:private@aws-0-xx.pooler.supabase.com:5432/other",
            "",
        ):
            with self.subTest(target=bool(target)), self.assertRaises(release.Blocked):
                release.target_url(target)

    def test_required_reviewers_fail_closed(self):
        _, now = self.make_freeze()
        sha = "a" * 40
        class MockGitHub:
            def get(self, path):
                if path.endswith("/branches/main"):
                    return {"commit": {"sha": sha}}
                if path.endswith("/pulls/306"):
                    return {"state": "open", "draft": True,
                            "merged_at": None,
                            "head": {"sha": release.APPROVED_SHA}}
                if path.endswith("/environments/production"):
                    return {"protection_rules": []}
                raise AssertionError(path)
        original = {k: os.environ.get(k) for k in (
            "GITHUB_ACTIONS", "GITHUB_EVENT_NAME", "GITHUB_REF",
            "GITHUB_REPOSITORY")}
        try:
            os.environ.update({
                "GITHUB_ACTIONS": "true",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_REPOSITORY": release.REPOSITORY,
            })
            with self.assertRaisesRegex(release.Blocked, "ENV_REQUIRED_REVIEWERS_MISSING"):
                release.verify_release_controls(
                    MockGitHub(), freeze_comment_id=1,
                    workflow_sha=sha, run_id=123,
                    attempt=1, now=now
                )
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_run_attempt_guard(self):
        with self.assertRaisesRegex(release.Blocked, "NOT_MANUAL_MAIN_RELEASE|RERUN_FORBIDDEN"):
            release.verify_release_controls(
                None, freeze_comment_id=1, workflow_sha="a"*40,
                run_id=123, attempt=2, now=datetime.now(UTC)
            )


class ControlIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(UTC)
        self.sha = "a"*40
        self.freeze_id = "LINKA-306-0097-TESTABCD1234"
        self.body = {
            "protocol": release.PROTOCOL, "status": "ACTIVE",
            "pr": 306, "operator": release.OWNER,
            "permissions_sha": release.APPROVED_SHA,
            "workflow_sha": self.sha, "project_ref": release.PROJECT_REF,
            "database": "postgres", "migration": release.TO_REV,
            "freeze_id": self.freeze_id,
            "start_utc": (self.now-timedelta(minutes=2)).isoformat(),
            "end_utc": (self.now+timedelta(minutes=8)).isoformat(),
            "coordination": {
                "github_merges": "FROZEN",
                "github_migrations": "FROZEN",
                "railway_deploys": "FROZEN",
                "other_migration_runners": "FROZEN",
                "clinics_remain_online": True,
            },
        }
        self.ctx = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": release.REPOSITORY,
        }

    def mock(self, *, replay=False, old_migration=False, revoked=False):
        parent = self

        class API:
            def get(self, path):
                if path.endswith("/branches/main"):
                    return {"commit": {"sha": parent.sha}}
                if path.endswith("/pulls/306"):
                    return {"state": "open", "draft": True,
                            "merged_at": None,
                            "head": {"sha": release.APPROVED_SHA}}
                if path.endswith("/environments/production"):
                    return {"protection_rules": [
                        {"type": "required_reviewers",
                         "reviewers": [{"type": "User", "id": 1}]}
                    ]}
                if ("/actions/workflows/" in path
                        and "/runs?" in path):
                    runs = [{"id": 123, "event": "workflow_dispatch"}]
                    if replay:
                        runs.append({"id": 122, "event": "workflow_dispatch"})
                    return {"total_count": len(runs), "workflow_runs": runs}
                if path.endswith("/actions/runs?per_page=100"):
                    return {"workflow_runs": [{
                        "name": "Database Migration",
                        "path": ".github/workflows/database-migrate.yml",
                        "status": "in_progress" if old_migration else "completed",
                    }]}
                if path.endswith("/issues/comments/77"):
                    return {"body": json.dumps(parent.body),
                            "user": {"login": release.OWNER},
                            "created_at": parent.now.isoformat()}
                if path.endswith("/issues/306/comments?per_page=100"):
                    if revoked:
                        return [{
                            "created_at": (parent.now
                                           + timedelta(seconds=1)).isoformat(),
                            "body": parent.freeze_id + " FREEZE CLOSED",
                        }]
                    return []
                raise AssertionError(path)

        return API()

    def check(self, api):
        from unittest.mock import patch
        with patch.dict(os.environ, self.ctx):
            return release.verify_release_controls(
                api, freeze_comment_id=77,
                workflow_sha=self.sha, run_id=123, attempt=1,
                now=self.now,
            )

    def test_controls_success(self):
        self.assertEqual(self.check(self.mock())["freeze_id"], self.freeze_id)

    def test_previous_run_rejected(self):
        with self.assertRaisesRegex(
            release.Blocked, "PRIOR_OR_UNREGISTERED_RELEASE_RUN"
        ):
            self.check(self.mock(replay=True))

    def test_conflicting_old_workflow_rejected(self):
        with self.assertRaisesRegex(
            release.Blocked, "OTHER_DATABASE_MIGRATION_ACTIVE"
        ):
            self.check(self.mock(old_migration=True))

    def test_revoked_freeze_rejected(self):
        with self.assertRaisesRegex(release.Blocked, "FREEZE_CLOSED"):
            self.check(self.mock(revoked=True))


@unittest.skipUnless(os.environ.get("LINKA_ISOLATED_0097_TEST") == "1",
                     "DB test opt-in; must run only on isolated PostgreSQL")
class LocalDatabaseTests(unittest.TestCase):
    def test_real_original_alembic_and_guards(self):
        uri = release.target_url(os.environ["LINKA_ISOLATED_DATABASE_URL"],
                                 isolated=True)
        source = Path(os.environ["LINKA_APPROVED_SOURCE"])
        release.verify_checkout(source)
        # Seeded fixture contains Alembic 0096 and workspace_members only.
        self.assertEqual(release.isolated_or_prod_schema(
            uri, revision=release.FROM_REV, columns=0
        ), release.FROM_REV)
        from sqlalchemy import engine_from_config, pool
        engine = engine_from_config(
            {"sqlalchemy.url": uri}, prefix="sqlalchemy.", poolclass=pool.NullPool
        )
        try:
            with release.TransactionGuard(write=True), engine.begin() as c:
                self.assertEqual(
                    c.exec_driver_sql("SHOW lock_timeout").scalar_one(), "5s"
                )
                self.assertEqual(
                    c.exec_driver_sql("SHOW statement_timeout").scalar_one(), "1min"
                )
                with self.assertRaises(release.Blocked):
                    c.exec_driver_sql("CREATE TABLE not_allowed(x int)")
        finally:
            engine.dispose()
        guard = release.TransactionGuard(write=True)
        result = release.perform_upgrade(uri, source, guard)
        self.assertEqual(result, {"revision": release.TO_REV,
                                  "ddl_count": 2, "version_writes": 1})
        # A new attempt must fail BEFORE any second DDL.
        guard2 = release.TransactionGuard(write=True)
        with self.assertRaises(release.Blocked):
            release.perform_upgrade(uri, source, guard2)
        self.assertEqual(guard2.ddls, [])


if __name__ == "__main__":
    unittest.main()
