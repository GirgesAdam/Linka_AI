"""Deterministic negative regressions plus optional isolated PostgreSQL 17 integration."""
import importlib.util
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

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
            "ALTER TABLE public.workspace_members ADD COLUMN permissions_version "
            "INTEGER DEFAULT 0 NOT NULL"
        ))
        for sql in (
            "DROP TABLE workspace_members",
            "ALTER TABLE workspace_members DROP COLUMN role",
            "ALTER TABLE workspace_members ADD COLUMN debug TEXT",
            "ALTER TABLE patients ADD COLUMN page_permissions JSONB",
            "ALTER TABLE workspace_members ADD COLUMN page_permissions JSONB; DROP TABLE users",
            "ALTER TABLE private.workspace_members ADD COLUMN page_permissions JSONB",
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
            "version_num": "0100_wrong",
            "version_num_1": release.FROM_REV,
        }))
        self.assertFalse(release.version_write(
            "UPDATE patients SET role=%(role)s WHERE id=%(id)s",
            (release.TO_REV, release.FROM_REV),
        ))

    def test_read_sql_blocks_side_effect_functions_and_ctes(self):
        self.assertTrue(release.safe_read("SELECT current_schema()"))
        self.assertTrue(release.safe_read("SHOW statement_timeout"))
        for sql in (
            "WITH changed AS (DELETE FROM users RETURNING id) SELECT * FROM changed",
            "SELECT set_config('search_path', 'private', false)",
            "SELECT nextval('sensitive_sequence')",
            "SELECT pg_advisory_unlock_all()",
            "SELECT pg_terminate_backend(123)",
            "SELECT pg_read_file('/etc/passwd')",
            "UPDATE alembic_version SET version_num='other'",
        ):
            with self.subTest(sql=sql):
                self.assertFalse(release.safe_read(sql))

    def test_production_target_requires_tls_verify_full(self):
        accepted = (
            "postgresql+psycopg://postgres:private@"
            "db.ycuxjlkhnubztgqmhtom.supabase.co:5432/postgres?sslmode=verify-full"
        )
        self.assertEqual(release.target_url(accepted), accepted)
        pooler = (
            "postgresql+psycopg://postgres.ycuxjlkhnubztgqmhtom:private@"
            "aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=verify-full"
        )
        self.assertEqual(release.target_url(pooler), pooler)
        rejected = (
            "postgresql+psycopg://postgres:private@"
            "db.ycuxjlkhnubztgqmhtom.supabase.co:5432/postgres",
            "postgresql+psycopg://postgres:private@"
            "db.ycuxjlkhnubztgqmhtom.supabase.co:5432/postgres?sslmode=require",
            "postgresql+psycopg://postgres:private@"
            "db.ycuxjlkhnubztgqmhtom.supabase.co:5432/postgres?sslmode=disable",
            "postgresql+psycopg://postgres:private@db.wrong.supabase.co:5432/postgres?sslmode=verify-full",
            "postgresql+psycopg://postgres:private@db.ycuxjlkhnubztgqmhtom.supabase.co:6543/postgres?sslmode=verify-full",
            "postgresql+psycopg://postgres:private@db.ycuxjlkhnubztgqmhtom.supabase.co:5432/other?sslmode=verify-full",
            "postgresql+psycopg://postgres:private@127.0.0.1:5432/postgres?sslmode=verify-full",
            "postgresql://postgres:private@db.ycuxjlkhnubztgqmhtom.supabase.co:5432/postgres?sslmode=verify-full",
            "",
        )
        for raw in rejected:
            with self.subTest(target=bool(raw), raw=raw.split("@")[-1][:48]):
                with self.assertRaises(release.Blocked):
                    release.target_url(raw)

    def test_isolated_target_is_local_only(self):
        local = "postgresql+psycopg://postgres:private@127.0.0.1:55484/postgres"
        self.assertEqual(release.target_url(local, isolated=True), local)
        for raw in (
            "postgresql+psycopg://postgres:private@db.example.com:5432/postgres",
            "postgresql+psycopg://postgres:private@localhost/postgres",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(release.Blocked):
                    release.target_url(raw, isolated=True)

    def test_session_contract_accepts_only_public_single_tls_session(self):
        valid = (
            "postgres", "public", "public", "5s", "60s",
            123, "456", True, "TLSv1.3",
        )
        self.assertEqual(
            release.validate_session_values(valid, require_tls=True),
            (123, "456"),
        )
        invalid_rows = (
            ("other", "public", "public", "5s", "60s", 123, "456", True, "TLSv1.3"),
            ("postgres", "private", "public", "5s", "60s", 123, "456", True, "TLSv1.3"),
            ("postgres", "public", "public, private", "5s", "60s", 123, "456", True, "TLSv1.3"),
            ("postgres", "public", "public", "10s", "60s", 123, "456", True, "TLSv1.3"),
            ("postgres", "public", "public", "5s", "2min", 123, "456", True, "TLSv1.3"),
            ("postgres", "public", "public", "5s", "60s", 123, "bad", True, "TLSv1.3"),
            ("postgres", "public", "public", "5s", "60s", 123, "456", False, None),
        )
        for row in invalid_rows:
            with self.subTest(row=row):
                with self.assertRaises(release.Blocked):
                    release.validate_session_values(row, require_tls=True)
        with self.assertRaisesRegex(release.Blocked, "BACKEND_PID_CHANGED"):
            release.validate_session_values(valid, require_tls=False, expected_pid=124)
        with self.assertRaisesRegex(release.Blocked, "TRANSACTION_ID_CHANGED"):
            release.validate_session_values(valid, require_tls=False, expected_xid="457")
        # Local isolated PostgreSQL is intentionally allowed to run without TLS.
        local = valid[:7] + (False, None)
        self.assertEqual(
            release.validate_session_values(local, require_tls=False),
            (123, "456"),
        )

    def test_transaction_guard_rejects_connection_pid_and_xid_changes(self):
        guard = release.TransactionGuard(write=True, require_tls=False)
        connection = object()
        guard.connection = connection
        guard.backend_pid = 123
        guard.transaction_id = "456"
        guard.assert_identity(connection, 123, "456")
        for conn, pid, xid, reason in (
            (object(), 123, "456", "BACKEND_CONNECTION_CHANGED"),
            (connection, 124, "456", "BACKEND_PID_CHANGED"),
            (connection, 123, "457", "TRANSACTION_ID_CHANGED"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(release.Blocked, reason):
                guard.assert_identity(conn, pid, xid)

    def make_freeze(self, *, start_offset=-2, end_offset=8, status="ACTIVE"):
        now = datetime.now(UTC)
        body = {
            "protocol": release.PROTOCOL,
            "status": status,
            "pr": 306,
            "operator": release.OWNER,
            "permissions_sha": release.APPROVED_SHA,
            "workflow_sha": "a" * 40,
            "project_ref": release.PROJECT_REF,
            "database": "postgres",
            "migration": release.TO_REV,
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

    def test_freeze_accepted_when_signed_active_and_immutable(self):
        freeze, now = self.make_freeze()
        result = release.freeze_contract(
            json.dumps(freeze),
            release.OWNER,
            now.isoformat(),
            now.isoformat(),
            workflow_sha="a" * 40,
            now=now,
        )
        self.assertEqual(result["status"], "ACTIVE")

    def test_freeze_rejects_wrong_signer_expiry_duration_scope_and_edit(self):
        for change in (
            "expired", "missing", "owner", "sha", "inactive",
            "coordination", "duration", "edited",
        ):
            freeze, now = self.make_freeze()
            owner = release.OWNER
            updated = now.isoformat()
            if change == "expired":
                freeze["end_utc"] = (now - timedelta(minutes=1)).isoformat()
            elif change == "missing":
                freeze["start_utc"] = ""
            elif change == "owner":
                owner = "unknown"
            elif change == "sha":
                freeze["permissions_sha"] = "0" * 40
            elif change == "inactive":
                freeze["status"] = "CLOSED"
            elif change == "coordination":
                freeze["coordination"]["railway_deploys"] = "UNCONFIRMED"
            elif change == "duration":
                freeze["end_utc"] = (now + timedelta(minutes=31)).isoformat()
            else:
                updated = (now + timedelta(seconds=1)).isoformat()
            with self.subTest(reason=change), self.assertRaises(release.Blocked):
                release.freeze_contract(
                    json.dumps(freeze),
                    owner,
                    now.isoformat(),
                    updated,
                    workflow_sha="a" * 40,
                    now=now,
                )

    def test_freeze_history_rejects_duplicate_id_and_edited_comment(self):
        freeze, now = self.make_freeze()
        body = json.dumps(freeze)
        note = {
            "id": 77,
            "body": body,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        release.verify_freeze_history(note, [note], freeze["freeze_id"])
        duplicate = dict(note, id=78, body=body + "\nFREEZE CLOSED")
        with self.assertRaisesRegex(release.Blocked, "FREEZE_ID_NOT_UNIQUE"):
            release.verify_freeze_history(note, [note, duplicate], freeze["freeze_id"])
        edited = dict(note, updated_at=(now + timedelta(seconds=1)).isoformat())
        with self.assertRaisesRegex(release.Blocked, "FREEZE_COMMENT_HISTORY_MISMATCH"):
            release.verify_freeze_history(note, [edited], freeze["freeze_id"])

    def test_environment_requires_real_reviewer_identity(self):
        release.verify_environment_reviewers({
            "name": "production",
            "protection_rules": [{
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "User", "reviewer": {"id": 123}}],
            }],
        })
        with self.assertRaisesRegex(
            release.Blocked, "ENV_SELF_REVIEW_NOT_PREVENTED"
        ):
            release.verify_environment_reviewers({
                "name": "production",
                "protection_rules": [{
                    "type": "required_reviewers",
                    "prevent_self_review": False,
                    "reviewers": [{"type": "User", "reviewer": {"id": 123}}],
                }],
            })
        with self.assertRaisesRegex(
            release.Blocked, "ENV_SELF_REVIEW_NOT_PREVENTED"
        ):
            release.verify_environment_reviewers({
                "name": "production",
                "protection_rules": [
                    {
                        "type": "required_reviewers",
                        "prevent_self_review": True,
                        "reviewers": [{"type": "User", "reviewer": {"id": 123}}],
                    },
                    {
                        "type": "required_reviewers",
                        "prevent_self_review": False,
                        "reviewers": [{"type": "Team", "reviewer": {"id": 456}}],
                    },
                ],
            })
        for environment in (
            {},
            {"name": "production", "protection_rules": []},
            {"name": "production", "protection_rules": [{"type": "required_reviewers", "reviewers": []}]},
            {"name": "production", "protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}]},
            {"name": "production", "protection_rules": [{
                "type": "required_reviewers",
                "prevent_self_review": False,
                "reviewers": [{"type": "User", "reviewer": {"id": 123}}],
            }]},
        ):
            with self.subTest(environment=environment), self.assertRaises(release.Blocked):
                release.verify_environment_reviewers(environment)

    def test_environment_branch_policy_must_be_exactly_main(self):
        release.verify_environment_branch_policy(
            {
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                }
            },
            [{"type": "branch", "name": "main"}],
        )
        invalid = (
            ({}, [{"type": "branch", "name": "main"}]),
            (
                {"deployment_branch_policy": {
                    "protected_branches": True,
                    "custom_branch_policies": False,
                }},
                [{"type": "branch", "name": "main"}],
            ),
            (
                {"deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                }},
                [{"type": "branch", "name": "*"}],
            ),
            (
                {"deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                }},
                [{"type": "branch", "name": "main"}, {"type": "branch", "name": "release/*"}],
            ),
        )
        for environment, policies in invalid:
            with self.subTest(environment=environment, policies=policies):
                with self.assertRaises(release.Blocked):
                    release.verify_environment_branch_policy(environment, policies)

    def test_railway_database_target_attestation_is_hard_blocked(self):
        self.assertFalse(release.RAILWAY_DATABASE_TARGET_ATTESTED)
        with self.assertRaisesRegex(
            release.Blocked, "PRODUCTION_RAILWAY_TARGET_UNVERIFIED"
        ):
            release.verify_railway_database_target()

    def test_railway_single_writer_is_hard_blocked_without_verified_control(self):
        self.assertFalse(release.RAILWAY_SINGLE_WRITER_PROOF_VERIFIED)
        with self.assertRaisesRegex(
            release.Blocked, "PRODUCTION_SINGLE_WRITER_UNVERIFIED"
        ):
            release.verify_railway_single_writer()

    def test_run_attempt_guard(self):
        with patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": release.REPOSITORY,
        }):
            with self.assertRaisesRegex(release.Blocked, "RERUN_FORBIDDEN"):
                release.verify_release_controls(
                    None,
                    freeze_comment_id=1,
                    workflow_sha="a" * 40,
                    run_id=123,
                    attempt=2,
                    now=datetime.now(UTC),
                )


class PaginationTests(unittest.TestCase):
    def test_get_all_reads_every_page_and_checks_total_count(self):
        class Pages:
            def __init__(self):
                self.pages = []

            def get(self, endpoint):
                query = parse_qs(urlsplit(endpoint).query)
                page = int(query["page"][0])
                self.pages.append(page)
                if page == 1:
                    return {"total_count": 101, "workflow_runs": [{"id": i} for i in range(100)]}
                if page == 2:
                    return {"total_count": 101, "workflow_runs": [{"id": 100}]}
                return {"total_count": 101, "workflow_runs": []}

        api = Pages()
        values = release.GitHubReader.get_all(
            api, "repos/example/actions/runs", "workflow_runs"
        )
        self.assertEqual(len(values), 101)
        self.assertEqual(api.pages, [1, 2])

    def test_get_all_rejects_truncated_history(self):
        class Truncated:
            def get(self, endpoint):
                return {"total_count": 101, "workflow_runs": [{"id": 1}]}

        with self.assertRaisesRegex(release.Blocked, "GITHUB_HISTORY_TRUNCATED"):
            release.GitHubReader.get_all(
                Truncated(), "repos/example/actions/runs", "workflow_runs"
            )


class ControlIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(UTC)
        self.sha = "a" * 40
        self.freeze_id = "LINKA-306-0097-TESTABCD1234"
        self.body = {
            "protocol": release.PROTOCOL,
            "status": "ACTIVE",
            "pr": 306,
            "operator": release.OWNER,
            "permissions_sha": release.APPROVED_SHA,
            "workflow_sha": self.sha,
            "project_ref": release.PROJECT_REF,
            "database": "postgres",
            "migration": release.TO_REV,
            "freeze_id": self.freeze_id,
            "start_utc": (self.now - timedelta(minutes=2)).isoformat(),
            "end_utc": (self.now + timedelta(minutes=8)).isoformat(),
            "coordination": {
                "github_merges": "FROZEN",
                "github_migrations": "FROZEN",
                "railway_deploys": "FROZEN",
                "other_migration_runners": "FROZEN",
                "clinics_remain_online": True,
            },
        }
        self.note = {
            "id": 77,
            "body": json.dumps(self.body),
            "user": {"login": release.OWNER},
            "created_at": self.now.isoformat(),
            "updated_at": self.now.isoformat(),
            "issue_url": "https://api.github.com/repos/GirgesAdam/Linka_AI/issues/306",
        }
        self.ctx = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": release.REPOSITORY,
        }

    def mock(self, *, replay=False, active_writer=None, env_rules=True,
             env_error=False, edited=False, duplicate=False, main_protected=True,
             branch_policy=True, branch_policy_error=False):
        parent = self

        class API:
            def get(self, endpoint):
                path = endpoint.split("?", 1)[0]
                if path.endswith("/branches/main"):
                    return {
                        "commit": {"sha": parent.sha},
                        "protected": parent.main_protected,
                        "protection": {"enabled": parent.main_protected},
                    }
                if path.endswith("/pulls/306"):
                    return {
                        "state": "open",
                        "draft": True,
                        "merged_at": None,
                        "head": {"sha": release.APPROVED_SHA},
                    }
                if path.endswith("/environments/production"):
                    if env_error:
                        raise release.Blocked("GITHUB_GUARD_UNAVAILABLE")
                    return {
                        "name": "production",
                        "deployment_branch_policy": {
                            "protected_branches": False,
                            "custom_branch_policies": True,
                        },
                        "protection_rules": (
                            [{
                                "type": "required_reviewers",
                                "prevent_self_review": True,
                                "reviewers": [{"type": "User", "reviewer": {"id": 1}}],
                            }] if env_rules else []
                        ),
                    }
                if path.endswith("/environments/production/deployment-branch-policies"):
                    if branch_policy_error:
                        raise release.Blocked("GITHUB_GUARD_UNAVAILABLE")
                    return {"branch_policies": (
                        [{"type": "branch", "name": "main"}]
                        if branch_policy
                        else [{"type": "branch", "name": "*"}]
                    )}
                if path.endswith(f"/actions/workflows/{release.WORKFLOW}/runs"):
                    runs = [{"id": 123, "event": "workflow_dispatch"}]
                    if replay:
                        runs.append({"id": 122, "event": "workflow_dispatch"})
                    return {"total_count": len(runs), "workflow_runs": runs}
                if path.endswith("/actions/workflows/apply-0068-production.yml/runs"):
                    status = "in_progress" if active_writer == "apply-0068-production.yml" else "completed"
                    return {"total_count": 1, "workflow_runs": [{"status": status}]}
                if path.endswith("/actions/workflows/database-migrate.yml/runs"):
                    status = "in_progress" if active_writer == "database-migrate.yml" else "completed"
                    return {"total_count": 1, "workflow_runs": [{"status": status}]}
                if path.endswith("/issues/comments/77"):
                    note = dict(parent.note)
                    if edited:
                        note["updated_at"] = (parent.now + timedelta(seconds=1)).isoformat()
                    return note
                if path.endswith("/issues/306/comments"):
                    comments = [dict(parent.note)]
                    if edited:
                        comments[0]["updated_at"] = (
                            parent.now + timedelta(seconds=1)
                        ).isoformat()
                    if duplicate:
                        comments.append({
                            "id": 78,
                            "body": parent.freeze_id + " FREEZE CLOSED",
                            "created_at": (parent.now + timedelta(seconds=1)).isoformat(),
                            "updated_at": (parent.now + timedelta(seconds=1)).isoformat(),
                        })
                    return comments
                raise AssertionError(endpoint)

        return API()

    def check(self, api, *, enforce_railway=False):
        with patch.dict(os.environ, self.ctx):
            return release.verify_release_controls(
                api,
                freeze_comment_id=77,
                workflow_sha=self.sha,
                run_id=123,
                attempt=1,
                now=self.now,
                enforce_railway=enforce_railway,
            )

    def test_github_controls_pass_without_claiming_railway_proof(self):
        self.assertEqual(
            self.check(self.mock())["freeze_id"],
            self.freeze_id,
        )

    def test_default_controls_remain_blocked_without_railway_target_attestation(self):
        with patch.dict(os.environ, self.ctx):
            with self.assertRaisesRegex(
                release.Blocked, "PRODUCTION_RAILWAY_TARGET_UNVERIFIED"
            ):
                release.verify_release_controls(
                    self.mock(),
                    freeze_comment_id=77,
                    workflow_sha=self.sha,
                    run_id=123,
                    attempt=1,
                    now=self.now,
                )

    def test_previous_release_run_rejected(self):
        with self.assertRaisesRegex(
            release.Blocked, "PRIOR_OR_UNREGISTERED_RELEASE_RUN"
        ):
            self.check(self.mock(replay=True))

    def test_each_production_github_writer_is_checked(self):
        for workflow in release.MIGRATION_WRITER_WORKFLOWS:
            with self.subTest(workflow=workflow), self.assertRaisesRegex(
                release.Blocked, "OTHER_DATABASE_MIGRATION_ACTIVE"
            ):
                self.check(self.mock(active_writer=workflow))

    def test_environment_api_unavailable_fails_closed(self):
        with self.assertRaisesRegex(release.Blocked, "GITHUB_GUARD_UNAVAILABLE"):
            self.check(self.mock(env_error=True))

    def test_environment_without_required_reviewers_fails_closed(self):
        with self.assertRaisesRegex(release.Blocked, "ENV_REQUIRED_REVIEWERS_MISSING"):
            self.check(self.mock(env_rules=False))

    def test_environment_branch_policy_not_main_only_fails_closed(self):
        with self.assertRaisesRegex(release.Blocked, "ENV_BRANCH_POLICY_NOT_MAIN_ONLY"):
            self.check(self.mock(branch_policy=False))

    def test_environment_branch_policy_api_unavailable_fails_closed(self):
        with self.assertRaisesRegex(release.Blocked, "GITHUB_GUARD_UNAVAILABLE"):
            self.check(self.mock(branch_policy_error=True))

    def test_edited_freeze_and_duplicate_freeze_id_rejected(self):
        with self.assertRaisesRegex(release.Blocked, "FREEZE_COMMENT_EDITED"):
            self.check(self.mock(edited=True))
        with self.assertRaisesRegex(release.Blocked, "FREEZE_ID_NOT_UNIQUE"):
            self.check(self.mock(duplicate=True))

    def test_unprotected_main_fails_closed(self):
        with self.assertRaisesRegex(release.Blocked, "MAIN_BRANCH_PROTECTION_MISSING"):
            self.check(self.mock(main_protected=False))

    def test_main_moved_fails_closed(self):
        api = self.mock()
        api.get = lambda endpoint: (
            {
                "commit": {"sha": "b" * 40},
                "protected": True,
                "protection": {"enabled": True},
            }
            if endpoint.endswith("/branches/main")
            else self.mock().get(endpoint)
        )
        with self.assertRaisesRegex(release.Blocked, "MAIN_MOVED"):
            self.check(api)


@unittest.skipUnless(
    os.environ.get("LINKA_ISOLATED_0097_TEST") == "1",
    "DB integration requires an isolated PostgreSQL 17 instance seeded at 0096",
)
class LocalDatabaseTests(unittest.TestCase):
    def test_original_alembic_upgrade_uses_one_connection_pid_xid_and_exact_writes(self):
        uri = release.target_url(
            os.environ["LINKA_ISOLATED_DATABASE_URL"],
            isolated=True,
        )
        source = Path(os.environ["LINKA_APPROVED_SOURCE"])
        release.verify_checkout(source)
        self.assertEqual(
            release.isolated_or_prod_schema(
                uri,
                revision=release.FROM_REV,
                columns=0,
                require_tls=False,
            ),
            release.FROM_REV,
        )

        from sqlalchemy import engine_from_config, pool

        engine = engine_from_config(
            {"sqlalchemy.url": uri},
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        try:
            # Negative: a second SQLAlchemy transaction/connection is forbidden.
            multi = release.TransactionGuard(write=True, require_tls=False)
            with multi:
                with engine.connect() as conn:
                    transaction = conn.begin()
                    conn.exec_driver_sql("SELECT 1")
                    transaction.rollback()
                with self.assertRaisesRegex(release.Blocked, "MULTIPLE_TRANSACTIONS"):
                    with engine.begin() as conn:
                        conn.exec_driver_sql("SELECT 1")
            self.assertEqual(multi.transactions, 1)

            # Negative: commit cannot proceed before the exact write set is complete.
            early = release.TransactionGuard(write=True, require_tls=False)
            with early:
                with engine.connect() as conn:
                    transaction = conn.begin()
                    with self.assertRaisesRegex(release.Blocked, "INCOMPLETE_WRITE_SET"):
                        transaction.commit()
                    if conn.in_transaction():
                        conn.rollback()
            self.assertEqual(early.precommit_verifications, 0)

            # Negative: an unapproved write is rejected before PostgreSQL executes it.
            rejected = release.TransactionGuard(write=True, require_tls=False)
            with rejected:
                with engine.connect() as conn:
                    transaction = conn.begin()
                    self.assertEqual(
                        conn.exec_driver_sql("SHOW lock_timeout").scalar_one(),
                        "5s",
                    )
                    self.assertEqual(
                        conn.exec_driver_sql("SHOW statement_timeout").scalar_one(),
                        "1min",
                    )
                    with self.assertRaisesRegex(release.Blocked, "UNAPPROVED_SQL"):
                        conn.exec_driver_sql("CREATE TABLE not_allowed(x int)")
                    transaction.rollback()
        finally:
            engine.dispose()

        guard = release.TransactionGuard(write=True, require_tls=False)
        result = release.perform_upgrade(uri, source, guard)
        self.assertEqual(result["revision"], release.TO_REV)
        self.assertEqual(result["transactions"], 1)
        self.assertEqual(result["precommit_verifications"], 1)
        self.assertIsInstance(result["backend_pid"], int)
        self.assertTrue(result["transaction_id"].isdigit())
        self.assertEqual(result["ddl_count"], 2)
        self.assertEqual(result["version_writes"], 1)

        # Negative: a repeat attempt is rejected before a second DDL statement.
        guard2 = release.TransactionGuard(write=True, require_tls=False)
        with self.assertRaises(release.Blocked):
            release.perform_upgrade(uri, source, guard2)
        self.assertEqual(guard2.transactions, 1)
        self.assertEqual(guard2.ddls, [])


if __name__ == "__main__":
    unittest.main()
