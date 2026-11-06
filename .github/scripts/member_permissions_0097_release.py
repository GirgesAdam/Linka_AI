"""Fail-closed, one-time 0097 release guard.

The production execution path remains disabled until GitHub and Railway
migration writers are coordinated by a verifiable single-writer control.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, engine_from_config, event, pool
from sqlalchemy.engine import make_url

APPROVED_SHA = "594c920ca3655851e5c9c54f7d6fec3dc7e2d32a"
FROM_REV = "0096_patient_customer_id"
TO_REV = "0097_member_page_permissions"
PROJECT_REF = "ycuxjlkhnubztgqmhtom"
REPOSITORY = "GirgesAdam/Linka_AI"
OWNER = "GirgesAdam"
WORKFLOW = "member-permissions-0097-release.yml"
MIGRATION_WRITER_WORKFLOWS = (
    "apply-0068-production.yml",
    "database-migrate.yml",
)
PROTOCOL = "linka_0097_release_freeze_v1"
ACK = "EXECUTE_APPROVED_0097_ONLY"
RAILWAY_DATABASE_TARGET_ATTESTED = False
RAILWAY_SINGLE_WRITER_PROOF_VERIFIED = False
PAGE_SIZE = 100
MAX_API_PAGES = 1000

SESSION_SQL = """
SELECT current_database(),
       current_schema(),
       current_setting('search_path'),
       current_setting('lock_timeout'),
       current_setting('statement_timeout'),
       pg_backend_pid(),
       pg_current_xact_id()::text,
       COALESCE((SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()), false),
       (SELECT version FROM pg_stat_ssl WHERE pid = pg_backend_pid())
"""

ALLOWED_SESSION_SQL = {
    "set local lock_timeout = '5s'",
    "set local statement_timeout = '60s'",
    "set local search_path = public",
}


class Blocked(Exception):
    """Safe fixed code; never include driver errors or secrets."""


def ensure(condition: bool, reason: str) -> None:
    if not condition:
        raise Blocked(reason)


def utc(value: str) -> datetime:
    try:
        date = datetime.fromisoformat(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise Blocked("INVALID_FREEZE_TIME") from exc
    ensure(date.tzinfo is not None, "FREEZE_TIMEZONE_MISSING")
    return date.astimezone(UTC)


def freeze_contract(
    body: str,
    author: str,
    created: str,
    updated: str,
    *,
    workflow_sha: str,
    now: datetime,
) -> dict:
    ensure(author == OWNER, "FREEZE_SIGNER_MISMATCH")
    ensure(bool(created) and updated == created, "FREEZE_COMMENT_EDITED")
    try:
        data = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise Blocked("INVALID_FREEZE_FORMAT") from exc
    ensure(isinstance(data, dict), "INVALID_FREEZE_FORMAT")
    for key, value in {
        "protocol": PROTOCOL,
        "status": "ACTIVE",
        "pr": 306,
        "operator": OWNER,
        "permissions_sha": APPROVED_SHA,
        "workflow_sha": workflow_sha,
        "project_ref": PROJECT_REF,
        "database": "postgres",
        "migration": TO_REV,
    }.items():
        ensure(data.get(key) == value, "FREEZE_SCOPE_MISMATCH")

    conditions = data.get("coordination")
    ensure(isinstance(conditions, dict), "COORDINATION_MISSING")
    for key in (
        "github_merges",
        "github_migrations",
        "railway_deploys",
        "other_migration_runners",
    ):
        ensure(conditions.get(key) == "FROZEN", "COORDINATION_MISSING")
    ensure(conditions.get("clinics_remain_online") is True, "COORDINATION_MISSING")

    start, end = utc(data.get("start_utc")), utc(data.get("end_utc"))
    ensure(timedelta(0) < end - start <= timedelta(minutes=30), "FREEZE_DURATION_INVALID")
    ensure(utc(created) <= now and start <= now < end, "FREEZE_NOT_ACTIVE")
    ensure(
        isinstance(data.get("freeze_id"), str)
        and bool(re.fullmatch(r"LINKA-306-0097-[A-Z0-9_-]{8,72}", data["freeze_id"])),
        "FREEZE_ID_INVALID",
    )
    return data


def verify_freeze_history(note: dict, comments: list[dict], freeze_id: str) -> None:
    ensure(isinstance(comments, list), "FREEZE_HISTORY_UNVERIFIED")
    note_id = note.get("id")
    ensure(isinstance(note_id, int), "FREEZE_COMMENT_ID_INVALID")
    copies = [item for item in comments if item.get("id") == note_id]
    ensure(len(copies) == 1, "FREEZE_COMMENT_HISTORY_MISMATCH")
    stored = copies[0]
    for key in ("body", "created_at", "updated_at"):
        ensure(stored.get(key) == note.get(key), "FREEZE_COMMENT_HISTORY_MISMATCH")
    ensure(note.get("updated_at") == note.get("created_at"), "FREEZE_COMMENT_EDITED")

    references = [
        item for item in comments
        if freeze_id in (item.get("body") or "")
    ]
    ensure(
        len(references) == 1 and references[0].get("id") == note_id,
        "FREEZE_ID_NOT_UNIQUE",
    )


def target_url(raw: str, *, isolated: bool = False) -> str:
    ensure(bool(raw), "MIGRATION_SECRET_MISSING")
    try:
        url = make_url(raw)
    except (TypeError, ValueError) as exc:
        raise Blocked("INVALID_DATABASE_URL") from exc
    ensure(
        url.drivername == "postgresql+psycopg" and url.database == "postgres",
        "WRONG_DATABASE_OR_DRIVER",
    )
    if isolated:
        ensure(
            url.host in ("localhost", "127.0.0.1") and url.port is not None,
            "LOCAL_DATABASE_REQUIRED",
        )
        return raw

    host = (url.host or "").lower()
    valid_pooler = (
        host.endswith(".pooler.supabase.com")
        and url.username == f"postgres.{PROJECT_REF}"
        and url.port == 5432
    )
    valid_direct = (
        host == f"db.{PROJECT_REF}.supabase.co"
        and url.username == "postgres"
        and url.port == 5432
    )
    ensure(valid_pooler or valid_direct, "DATABASE_TARGET_UNVERIFIED")
    ensure(bool(url.password), "DATABASE_PASSWORD_ABSENT")
    ensure(
        str(url.query.get("sslmode", "")).lower() == "verify-full",
        "TLS_VERIFY_FULL_REQUIRED",
    )
    return raw


def normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().replace('"', "")).lower()


def allowed_ddl(sql: str) -> bool:
    sql = normalized(sql)
    return bool(
        re.fullmatch(
            r"alter table (?:public\.)?workspace_members add column "
            r"page_permissions jsonb",
            sql,
        )
        or re.fullmatch(
            r"alter table (?:public\.)?workspace_members add column "
            r"permissions_version integer default 0 not null",
            sql,
        )
    )


def version_write(sql: str, params: object) -> bool:
    sql = normalized(sql)
    exact_literal = (
        f"update alembic_version set version_num='{TO_REV}' "
        f"where alembic_version.version_num = '{FROM_REV}'"
    )
    if sql == exact_literal and not params:
        return True

    match = re.fullmatch(
        r"update (?:public\.)?alembic_version set version_num\s*=\s*"
        r"(?:%\([^)]+\)s|%s|\$[0-9]+|:[a-z_0-9]+) "
        r"where (?:public\.)?alembic_version\.version_num\s*=\s*"
        r"(?:%\([^)]+\)s|%s|\$[0-9]+|:[a-z_0-9]+)",
        sql,
    )
    if isinstance(params, dict):
        values = list(params.values())
    elif isinstance(params, (tuple, list)):
        values = list(params)
    else:
        return False
    return bool(match) and len(values) == 2 and TO_REV in values and FROM_REV in values


def safe_read(sql: str) -> bool:
    statement = normalized(sql)
    word = statement.split(" ", 1)[0] if statement else ""
    if word == "show":
        return True
    if word != "select":
        return False
    forbidden = re.compile(
        r"\b(?:insert|update|delete|merge|create|alter|drop|truncate|copy|call|do|"
        r"nextval|setval|set_config|pg_advisory_unlock\w*|pg_terminate_backend|"
        r"pg_cancel_backend|pg_notify|pg_reload_conf|pg_sleep|dblink\w*|lo_import|"
        r"lo_export|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file)\b"
    )
    return forbidden.search(statement) is None


def validate_session_values(
    values: tuple,
    *,
    require_tls: bool,
    expected_pid: int | None = None,
    expected_xid: str | None = None,
) -> tuple[int, str]:
    ensure(len(values) == 9, "SESSION_EVIDENCE_INCOMPLETE")
    database, schema, search_path, lock, statement, pid, xid, ssl, ssl_version = values
    ensure(database == "postgres", "WRONG_DATABASE")
    ensure(schema == "public" and search_path == "public", "PUBLIC_SCHEMA_REQUIRED")
    ensure(lock in ("5s", "5000ms"), "LOCK_TIMEOUT_MISSING")
    ensure(statement in ("1min", "60s", "60000ms"), "STATEMENT_TIMEOUT_MISSING")
    ensure(isinstance(pid, int) and pid > 0, "BACKEND_PID_INVALID")
    ensure(isinstance(xid, str) and xid.isdigit(), "TRANSACTION_ID_INVALID")
    if require_tls:
        ensure(ssl is True and ssl_version in ("TLSv1.2", "TLSv1.3"), "TLS_NOT_VERIFIED")
    if expected_pid is not None:
        ensure(pid == expected_pid, "BACKEND_PID_CHANGED")
    if expected_xid is not None:
        ensure(xid == expected_xid, "TRANSACTION_ID_CHANGED")
    return pid, xid


def verify_session(conn, *, require_tls: bool) -> tuple[int, str]:
    conn.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
    conn.exec_driver_sql("SET LOCAL statement_timeout = '60s'")
    conn.exec_driver_sql("SET LOCAL search_path = public")
    return validate_session_values(
        conn.exec_driver_sql(SESSION_SQL).one(),
        require_tls=require_tls,
    )


def check_schema(conn, revision: str, columns: int) -> str:
    ensure(
        conn.exec_driver_sql("SELECT current_schema()").scalar_one() == "public",
        "PUBLIC_SCHEMA_REQUIRED",
    )
    current = conn.exec_driver_sql(
        "SELECT version_num FROM public.alembic_version LIMIT 1"
    ).scalar_one()
    ensure(current == revision, "REVISION_MISMATCH")
    found = conn.exec_driver_sql(
        "SELECT column_name, udt_name, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'workspace_members' AND column_name IN "
        "('page_permissions', 'permissions_version')"
    ).all()
    ensure(len(found) == columns, "COLUMNS_MISMATCH")
    if columns == 2:
        values = {row[0]: row[1:] for row in found}
        ensure(
            values.get("page_permissions") == ("jsonb", "YES", None),
            "JSONB_COLUMN_MISMATCH",
        )
        version = values.get("permissions_version")
        ensure(
            version is not None
            and version[0] == "int4"
            and version[1] == "NO"
            and version[2] in ("0", "'0'::integer"),
            "VERSION_COLUMN_MISMATCH",
        )
    return current


class TransactionGuard:
    def __init__(self, *, write: bool = False, expires: datetime | None = None,
                 require_tls: bool = True):
        self.write = write
        self.expires = expires
        self.require_tls = require_tls
        self.ddls: list[str] = []
        self.version_updates = 0
        self.transactions = 0
        self.connection = None
        self.backend_pid: int | None = None
        self.transaction_id: str | None = None
        self.precommit_verifications = 0
        self.begin_handler = None
        self.query_handler = None
        self.commit_handler = None

    def assert_identity(self, conn, pid: int, xid: str) -> None:
        ensure(conn is self.connection, "BACKEND_CONNECTION_CHANGED")
        ensure(pid == self.backend_pid, "BACKEND_PID_CHANGED")
        ensure(xid == self.transaction_id, "TRANSACTION_ID_CHANGED")

    def __enter__(self):
        @event.listens_for(Engine, "begin")
        def begin(conn):
            ensure(self.transactions == 0, "MULTIPLE_TRANSACTIONS")
            self.transactions += 1
            self.connection = conn
            pid, xid = verify_session(conn, require_tls=self.require_tls)
            self.backend_pid = pid
            self.transaction_id = xid
            if self.write:
                available = conn.exec_driver_sql(
                    "SELECT pg_try_advisory_xact_lock(%s, %s)", (306, 97)
                ).scalar_one()
                ensure(available is True, "MIGRATION_ALREADY_RUNNING")
                check_schema(conn, FROM_REV, 0)

        @event.listens_for(Engine, "before_cursor_execute")
        def before_sql(conn, cursor, sql, params, context, executemany):
            statement = normalized(sql)
            if not statement:
                raise Blocked("EMPTY_SQL_REJECTED")
            word = statement.split(" ", 1)[0]
            if safe_read(statement):
                return
            if word == "set":
                ensure(statement in ALLOWED_SESSION_SQL, "UNAPPROVED_SESSION_CHANGE")
                return

            ensure(self.write, "WRITES_DISABLED")
            ensure(self.transactions == 1, "MULTIPLE_TRANSACTIONS")
            ensure(self.expires is None or datetime.now(UTC) < self.expires,
                   "FREEZE_EXPIRED_DURING_DDL")
            ensure(conn.in_transaction(), "UNGUARDED_TRANSACTION")
            ensure(self.connection is not None, "UNGUARDED_TRANSACTION")

            values = conn.exec_driver_sql(SESSION_SQL).one()
            pid, xid = validate_session_values(
                values,
                require_tls=self.require_tls,
                expected_pid=self.backend_pid,
                expected_xid=self.transaction_id,
            )
            self.assert_identity(conn, pid, xid)

            if word == "alter":
                ensure(allowed_ddl(statement), "UNAPPROVED_DDL")
                ensure(len(self.ddls) < 2, "EXTRA_DDL")
                self.ddls.append(statement)
                return
            if word == "update":
                ensure(version_write(statement, params), "UNAPPROVED_VERSION_UPDATE")
                self.version_updates += 1
                ensure(self.version_updates <= 1, "EXTRA_VERSION_UPDATE")
                return
            raise Blocked("UNAPPROVED_SQL")

        @event.listens_for(Engine, "commit")
        def before_commit(conn):
            ensure(self.write, "WRITES_DISABLED")
            ensure(self.transactions == 1, "TRANSACTION_COUNT_MISMATCH")
            ensure(conn is self.connection, "BACKEND_CONNECTION_CHANGED")
            ensure(
                self.expires is None or datetime.now(UTC) < self.expires,
                "FREEZE_EXPIRED_BEFORE_COMMIT",
            )
            pid, xid = validate_session_values(
                conn.exec_driver_sql(SESSION_SQL).one(),
                require_tls=self.require_tls,
                expected_pid=self.backend_pid,
                expected_xid=self.transaction_id,
            )
            self.assert_identity(conn, pid, xid)
            self.verify()
            self.precommit_verifications += 1

        self.begin_handler, self.query_handler, self.commit_handler = (
            begin, before_sql, before_commit
        )
        return self

    def __exit__(self, exc_type, exc, traceback):
        event.remove(Engine, "begin", self.begin_handler)
        event.remove(Engine, "before_cursor_execute", self.query_handler)
        event.remove(Engine, "commit", self.commit_handler)

    def verify(self) -> None:
        ensure(self.transactions == 1, "TRANSACTION_COUNT_MISMATCH")
        ensure(self.connection is not None, "BACKEND_CONNECTION_MISSING")
        ensure(self.backend_pid is not None, "BACKEND_PID_MISSING")
        ensure(self.transaction_id is not None, "TRANSACTION_ID_MISSING")
        ensure(len(self.ddls) == 2 and self.version_updates == 1, "INCOMPLETE_WRITE_SET")
        ensure(
            any("page_permissions jsonb" in sql for sql in self.ddls)
            and any("permissions_version integer" in sql for sql in self.ddls),
            "MISSING_APPROVED_COLUMN",
        )


def isolated_or_prod_schema(
    url: str,
    *,
    revision: str,
    columns: int,
    require_tls: bool,
) -> str:
    engine = engine_from_config(
        {"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    try:
        with engine.begin() as conn:
            verify_session(conn, require_tls=require_tls)
            return check_schema(conn, revision, columns)
    finally:
        engine.dispose()


def verify_checkout(source: Path) -> None:
    source = Path(source)
    ensure(source.is_dir(), "CHECKOUT_MISSING")
    checks = [
        ("git", "-C", str(source), "rev-parse", "HEAD"),
        ("git", "-C", str(source), "status", "--porcelain"),
        ("git", "-C", str(source), "symbolic-ref", "-q", "--short", "HEAD"),
    ]
    results = [
        subprocess.run(c, capture_output=True, text=True, timeout=20, check=False)
        for c in checks
    ]
    ensure(
        results[0].returncode == 0 and results[0].stdout.strip() == APPROVED_SHA,
        "CHECKOUT_SHA_CHANGED",
    )
    ensure(results[1].returncode == 0 and not results[1].stdout.strip(), "CHECKOUT_DIRTY")
    ensure(results[2].returncode == 1, "CHECKOUT_NOT_DETACHED")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=source / "backend",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    ensure(
        result.returncode == 0 and result.stdout.strip() == f"{TO_REV} (head)",
        "ALEMBIC_HEAD_CHANGED",
    )


def perform_upgrade(url: str, source: Path, safety: TransactionGuard) -> dict:
    from alembic import command
    from alembic.config import Config

    backend = Path(source) / "backend"
    old_cwd = Path.cwd()
    old_url = os.environ.get("MIGRATION_DATABASE_URL")
    with safety:
        os.environ["MIGRATION_DATABASE_URL"] = url
        try:
            os.chdir(backend)
            sys.path.insert(0, str(backend))
            command.upgrade(Config(str(backend / "alembic.ini")), TO_REV)
            safety.verify()
            ensure(safety.precommit_verifications == 1, "PRECOMMIT_VERIFICATION_MISSING")
        finally:
            os.chdir(old_cwd)
            if old_url is None:
                os.environ.pop("MIGRATION_DATABASE_URL", None)
            else:
                os.environ["MIGRATION_DATABASE_URL"] = old_url
    isolated_or_prod_schema(
        url, revision=TO_REV, columns=2, require_tls=safety.require_tls
    )
    return {
        "revision": TO_REV,
        "transactions": safety.transactions,
        "precommit_verifications": safety.precommit_verifications,
        "backend_pid": safety.backend_pid,
        "transaction_id": safety.transaction_id,
        "ddl_count": len(safety.ddls),
        "version_writes": safety.version_updates,
    }


class GitHubReader:
    def __init__(self, token: str):
        ensure(bool(token), "GITHUB_TOKEN_MISSING")
        self.token = token

    def get(self, endpoint: str):
        request = urllib.request.Request(
            "https://api.github.com/" + endpoint,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                return json.load(response)
        except (urllib.error.URLError, ValueError) as exc:
            raise Blocked("GITHUB_GUARD_UNAVAILABLE") from exc

    def get_all(self, endpoint: str, collection_key: str | None = None) -> list:
        results = []
        for page in range(1, MAX_API_PAGES + 1):
            separator = "&" if "?" in endpoint else "?"
            payload = self.get(
                f"{endpoint}{separator}per_page={PAGE_SIZE}&page={page}"
            )
            if collection_key is None:
                items = payload
                total_count = None
            else:
                ensure(isinstance(payload, dict), "GITHUB_HISTORY_UNVERIFIED")
                items = payload.get(collection_key)
                total_count = payload.get("total_count")
            ensure(isinstance(items, list), "GITHUB_HISTORY_UNVERIFIED")
            results.extend(items)
            if len(items) < PAGE_SIZE:
                if isinstance(total_count, int):
                    ensure(len(results) >= total_count, "GITHUB_HISTORY_TRUNCATED")
                return results
        raise Blocked("GITHUB_HISTORY_PAGINATION_LIMIT")


def verify_environment_reviewers(environment: dict) -> None:
    ensure(environment.get("name") == "production", "ENVIRONMENT_NAME_MISMATCH")
    rules = environment.get("protection_rules")
    ensure(isinstance(rules, list), "ENV_PROTECTION_UNVERIFIED")
    required_rules = [
        rule for rule in rules
        if rule.get("type") == "required_reviewers"
    ]
    ensure(bool(required_rules), "ENV_REQUIRED_REVIEWERS_MISSING")
    for rule in required_rules:
        valid_reviewers = []
        for reviewer in rule.get("reviewers") or []:
            if not isinstance(reviewer, dict):
                continue
            detail = reviewer.get("reviewer")
            reviewer_id = reviewer.get("id")
            if isinstance(detail, dict):
                reviewer_id = detail.get("id", reviewer_id)
            if isinstance(reviewer_id, int) and reviewer_id > 0:
                valid_reviewers.append(reviewer_id)
        ensure(bool(valid_reviewers), "ENV_REQUIRED_REVIEWERS_MISSING")
        ensure(
            rule.get("prevent_self_review") is True,
            "ENV_SELF_REVIEW_NOT_PREVENTED",
        )

def verify_environment_branch_policy(environment: dict, branch_policies: list) -> None:
    policy = environment.get("deployment_branch_policy")
    ensure(isinstance(policy, dict), "ENV_BRANCH_POLICY_UNVERIFIED")
    ensure(
        policy.get("protected_branches") is False
        and policy.get("custom_branch_policies") is True,
        "ENV_BRANCH_POLICY_NOT_MAIN_ONLY",
    )
    ensure(
        isinstance(branch_policies, list)
        and len(branch_policies) == 1
        and branch_policies[0].get("type") == "branch"
        and branch_policies[0].get("name") == "main",
        "ENV_BRANCH_POLICY_NOT_MAIN_ONLY",
    )


def verify_railway_database_target() -> None:
    ensure(
        RAILWAY_DATABASE_TARGET_ATTESTED is True,
        "PRODUCTION_RAILWAY_TARGET_UNVERIFIED",
    )


def verify_railway_single_writer() -> None:
    ensure(
        RAILWAY_SINGLE_WRITER_PROOF_VERIFIED is True,
        "PRODUCTION_SINGLE_WRITER_UNVERIFIED",
    )


def verify_release_controls(
    gh: GitHubReader,
    *,
    freeze_comment_id: int,
    workflow_sha: str,
    run_id: int,
    attempt: int,
    now: datetime,
    enforce_railway: bool = True,
) -> dict:
    ensure(
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY,
        "NOT_MANUAL_MAIN_RELEASE",
    )
    ensure(attempt == 1, "RERUN_FORBIDDEN")

    main = gh.get(f"repos/{REPOSITORY}/branches/main")
    protection = main.get("protection")
    ensure(
        main.get("protected") is True
        and isinstance(protection, dict)
        and protection.get("enabled") is True,
        "MAIN_BRANCH_PROTECTION_MISSING",
    )
    ensure(main.get("commit", {}).get("sha") == workflow_sha, "MAIN_MOVED")
    pr = gh.get(f"repos/{REPOSITORY}/pulls/306")
    ensure(
        pr.get("state") == "open"
        and pr.get("draft") is True
        and pr.get("head", {}).get("sha") == APPROVED_SHA
        and pr.get("merged_at") is None,
        "PERMISSIONS_PR_CHANGED",
    )

    environment = gh.get(f"repos/{REPOSITORY}/environments/production")
    verify_environment_reviewers(environment)
    branch_policies = gh.get_all(
        f"repos/{REPOSITORY}/environments/production/deployment-branch-policies",
        "branch_policies",
    )
    verify_environment_branch_policy(environment, branch_policies)

    release_runs = gh.get_all(
        f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs",
        "workflow_runs",
    )
    dispatched = [run for run in release_runs if run.get("event") == "workflow_dispatch"]
    ensure(
        len(dispatched) == 1 and dispatched[0].get("id") == run_id,
        "PRIOR_OR_UNREGISTERED_RELEASE_RUN",
    )

    for workflow in MIGRATION_WRITER_WORKFLOWS:
        runs = gh.get_all(
            f"repos/{REPOSITORY}/actions/workflows/{workflow}/runs",
            "workflow_runs",
        )
        for run in runs:
            ensure(run.get("status") == "completed", "OTHER_DATABASE_MIGRATION_ACTIVE")

    note = gh.get(f"repos/{REPOSITORY}/issues/comments/{freeze_comment_id}")
    ensure(note.get("issue_url", "").endswith("/issues/306"), "FREEZE_COMMENT_NOT_ON_PR306")
    freeze = freeze_contract(
        note.get("body", ""),
        note.get("user", {}).get("login"),
        note.get("created_at", ""),
        note.get("updated_at", ""),
        workflow_sha=workflow_sha,
        now=now,
    )
    comments = gh.get_all(
        f"repos/{REPOSITORY}/issues/306/comments",
        collection_key=None,
    )
    verify_freeze_history(note, comments, freeze["freeze_id"])

    if enforce_railway:
        verify_railway_database_target()
        verify_railway_single_writer()
    return freeze


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("controls", "execute"), required=True)
    parser.add_argument("--freeze-comment-id", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    try:
        ensure(os.environ.get("EXECUTION_ACK") == ACK, "MANUAL_ACK_MISSING")
        workflow_sha = os.environ.get("GITHUB_SHA", "")
        run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
        attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
        github = GitHubReader(os.environ.get("GH_TOKEN", ""))
        freeze = verify_release_controls(
            github,
            freeze_comment_id=args.freeze_comment_id,
            workflow_sha=workflow_sha,
            run_id=run_id,
            attempt=attempt,
            now=datetime.now(UTC),
        )
        if args.phase == "controls":
            print("PASS: GitHub controls verified; Railway single-writer gate is independently required.")
            return

        verify_checkout(args.source)
        url = target_url(os.environ.get("MIGRATION_DATABASE_URL", ""))
        isolated_or_prod_schema(
            url, revision=FROM_REV, columns=0, require_tls=True
        )
        freeze = verify_release_controls(
            github,
            freeze_comment_id=args.freeze_comment_id,
            workflow_sha=workflow_sha,
            run_id=run_id,
            attempt=attempt,
            now=datetime.now(UTC),
        )
        safety = TransactionGuard(
            write=True,
            expires=utc(freeze["end_utc"]),
            require_tls=True,
        )
        result = perform_upgrade(url, args.source, safety)
        print(
            "PASS: approved 0097 migration and post-schema verified: "
            f"{result['transactions']} transaction, PID/XID bound, "
            f"{result['ddl_count']} DDL, {result['version_writes']} version write."
        )
    except Blocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception:  # noqa: BLE001 - secret-safe fail closed
        print("BLOCKED: UNEXPECTED_RELEASE_ERROR", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
