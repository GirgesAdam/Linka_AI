"""One-time 0097 release gate. Original Alembic runs from a SHA-pinned checkout.

This script is only for the dedicated manual, protected GitHub Actions workflow.
It deliberately fails closed on missing environment protection, freeze or target.
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
PROTOCOL = "linka_0097_release_freeze_v1"
ACK = "EXECUTE_APPROVED_0097_ONLY"


class Blocked(Exception):
    """Safe fixed code; never include driver errors or secrets."""


def ensure(condition, reason):
    if not condition:
        raise Blocked(reason)


def utc(value):
    try:
        date = datetime.fromisoformat(value)
    except (AttributeError, ValueError) as exc:
        raise Blocked("INVALID_FREEZE_TIME") from exc
    ensure(date.tzinfo is not None, "FREEZE_TIMEZONE_MISSING")
    return date.astimezone(UTC)


def freeze_contract(body, author, created, *, workflow_sha, now):
    ensure(author == OWNER, "FREEZE_SIGNER_MISMATCH")
    try:
        data = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise Blocked("INVALID_FREEZE_FORMAT") from exc
    ensure(isinstance(data, dict), "INVALID_FREEZE_FORMAT")
    for key, val in {
        "protocol": PROTOCOL, "status": "ACTIVE", "pr": 306,
        "operator": OWNER, "permissions_sha": APPROVED_SHA,
        "workflow_sha": workflow_sha, "project_ref": PROJECT_REF,
        "database": "postgres", "migration": TO_REV,
    }.items():
        ensure(data.get(key) == val, "FREEZE_SCOPE_MISMATCH")
    conditions = data.get("coordination")
    ensure(isinstance(conditions, dict), "COORDINATION_MISSING")
    for key in ("github_merges", "github_migrations", "railway_deploys",
                "other_migration_runners"):
        ensure(conditions.get(key) == "FROZEN", "COORDINATION_MISSING")
    ensure(conditions.get("clinics_remain_online") is True,
           "COORDINATION_MISSING")
    start, end = utc(data.get("start_utc")), utc(data.get("end_utc"))
    ensure(timedelta(0) < end - start <= timedelta(minutes=30),
           "FREEZE_DURATION_INVALID")
    ensure(utc(created) <= now and start <= now < end, "FREEZE_NOT_ACTIVE")
    ensure(isinstance(data.get("freeze_id"), str)
           and bool(re.fullmatch(r"LINKA-306-0097-[A-Z0-9_-]{8,72}",
                                 data["freeze_id"])), "FREEZE_ID_INVALID")
    return data


def target_url(raw, *, isolated=False):
    ensure(bool(raw), "MIGRATION_SECRET_MISSING")
    try:
        url = make_url(raw)
    except (TypeError, ValueError) as exc:
        raise Blocked("INVALID_DATABASE_URL") from exc
    ensure(url.drivername == "postgresql+psycopg"
           and url.database == "postgres", "WRONG_DATABASE_OR_DRIVER")
    if isolated:
        ensure(url.host in ("localhost", "127.0.0.1") and url.port is not None,
               "LOCAL_DATABASE_REQUIRED")
    else:
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
    return raw


def normalized(sql):
    return re.sub(r"\s+", " ", sql.strip().replace('"', "")).lower()


def allowed_ddl(sql):
    sql = normalized(sql)
    return bool(
        re.fullmatch(
            r"alter table (?:public\.)?workspace_members add column "
            r"page_permissions jsonb", sql
        )
        or re.fullmatch(
            r"alter table (?:public\.)?workspace_members add column "
            r"permissions_version integer default 0 not null", sql
        )
    )


def version_write(sql, params):
    sql = normalized(sql)
    # Alembic can render the exact trusted revision IDs as SQL literals
    # rather than driver bind parameters; accept no other literals.
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
        r"(?:%\([^)]+\)s|%s|\$[0-9]+|:[a-z_0-9]+)", sql
    )
    if isinstance(params, dict):
        vals = list(params.values())
    elif isinstance(params, (tuple, list)):
        vals = list(params)
    else:
        return False
    return bool(match) and len(vals) == 2 and TO_REV in vals and FROM_REV in vals


def check_schema(conn, revision, columns):
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
        ensure(values.get("page_permissions") == ("jsonb", "YES", None),
               "JSONB_COLUMN_MISMATCH")
        v = values.get("permissions_version")
        ensure(v is not None and v[0] == "int4" and v[1] == "NO"
               and v[2] in ("0", "'0'::integer"), "VERSION_COLUMN_MISMATCH")
    return current


class TransactionGuard:
    def __init__(self, *, write=False, expires=None):
        self.write = write
        self.expires = expires
        self.ddls = []
        self.version_updates = 0
        self.transactions = 0
        self.begin_handler = None
        self.query_handler = None

    def __enter__(self):
        @event.listens_for(Engine, "begin")
        def begin(conn):
            conn.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            conn.exec_driver_sql("SET LOCAL statement_timeout = '60s'")
            database, lock, statement, pid = conn.exec_driver_sql(
                "SELECT current_database(), current_setting('lock_timeout'), "
                "current_setting('statement_timeout'), pg_backend_pid()"
            ).one()
            ensure(database == "postgres", "WRONG_DATABASE")
            ensure(lock in ("5s", "5000ms"), "LOCK_TIMEOUT_MISSING")
            ensure(statement in ("1min", "60s", "60000ms"),
                   "STATEMENT_TIMEOUT_MISSING")
            if self.write:
                available = conn.exec_driver_sql(
                    "SELECT pg_try_advisory_xact_lock(%s, %s)", (306, 97)
                ).scalar_one()
                ensure(available is True, "MIGRATION_ALREADY_RUNNING")
                check_schema(conn, FROM_REV, 0)
            conn.info["release_pid"] = pid
            self.transactions += 1

        @event.listens_for(Engine, "before_cursor_execute")
        def before_sql(conn, cursor, sql, params, context, executemany):
            word = normalized(sql).split(" ", 1)[0] if sql.strip() else ""
            if word not in (
                "alter", "create", "drop", "truncate", "comment", "grant",
                "revoke", "insert", "update", "delete", "copy", "do"
            ):
                return
            ensure(self.write, "WRITES_DISABLED")
            if self.expires is not None:
                ensure(datetime.now(UTC) < self.expires,
                       "FREEZE_EXPIRED_DURING_DDL")
            ensure(conn.in_transaction() and "release_pid" in conn.info,
                   "UNGUARDED_TRANSACTION")
            lock, timeout, pid = conn.exec_driver_sql(
                "SELECT current_setting('lock_timeout'), "
                "current_setting('statement_timeout'), pg_backend_pid()"
            ).one()
            ensure(lock in ("5s", "5000ms")
                   and timeout in ("1min", "60s", "60000ms")
                   and pid == conn.info["release_pid"], "DDL_GUARD_LOST")
            if word == "alter":
                ensure(allowed_ddl(sql), "UNAPPROVED_DDL")
                ensure(len(self.ddls) < 2, "EXTRA_DDL")
                self.ddls.append(normalized(sql))
            elif word == "update":
                ensure(version_write(sql, params), "UNAPPROVED_VERSION_UPDATE")
                self.version_updates += 1
                ensure(self.version_updates <= 1, "EXTRA_VERSION_UPDATE")
            else:
                raise Blocked("UNAPPROVED_WRITE")

        self.begin_handler, self.query_handler = begin, before_sql
        return self

    def __exit__(self, exc_type, exc, traceback):
        event.remove(Engine, "begin", self.begin_handler)
        event.remove(Engine, "before_cursor_execute", self.query_handler)

    def verify(self):
        ensure(len(self.ddls) == 2 and self.version_updates == 1,
               "INCOMPLETE_WRITE_SET")
        ensure(any("page_permissions jsonb" in sql for sql in self.ddls)
               and any("permissions_version integer" in sql
                       for sql in self.ddls), "MISSING_APPROVED_COLUMN")


def isolated_or_prod_schema(url, *, revision, columns):
    engine = engine_from_config(
        {"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    try:
        with engine.connect() as conn:
            return check_schema(conn, revision, columns)
    finally:
        engine.dispose()


def verify_checkout(source):
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
    ensure(results[0].returncode == 0
           and results[0].stdout.strip() == APPROVED_SHA,
           "CHECKOUT_SHA_CHANGED")
    ensure(results[1].returncode == 0
           and not results[1].stdout.strip(), "CHECKOUT_DIRTY")
    ensure(results[2].returncode == 1, "CHECKOUT_NOT_DETACHED")
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=source / "backend", capture_output=True, text=True, timeout=30,
        check=False,
    )
    ensure(r.returncode == 0
           and r.stdout.strip() == f"{TO_REV} (head)",
           "ALEMBIC_HEAD_CHANGED")


def perform_upgrade(url, source, safety):
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
        finally:
            os.chdir(old_cwd)
            if old_url is None:
                os.environ.pop("MIGRATION_DATABASE_URL", None)
            else:
                os.environ["MIGRATION_DATABASE_URL"] = old_url
    isolated_or_prod_schema(url, revision=TO_REV, columns=2)
    return {"revision": TO_REV, "ddl_count": len(safety.ddls),
            "version_writes": safety.version_updates}


class GitHubReader:
    def __init__(self, token):
        ensure(bool(token), "GITHUB_TOKEN_MISSING")
        self.token = token

    def get(self, endpoint):
        req = urllib.request.Request(
            "https://api.github.com/" + endpoint,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                return json.load(response)
        except (urllib.error.URLError, ValueError) as exc:
            raise Blocked("GITHUB_GUARD_UNAVAILABLE") from exc


def verify_release_controls(gh, *, freeze_comment_id, workflow_sha,
                            run_id, attempt, now):
    ensure(
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY,
        "NOT_MANUAL_MAIN_RELEASE",
    )
    ensure(attempt == 1, "RERUN_FORBIDDEN")
    main = gh.get(f"repos/{REPOSITORY}/branches/main")
    ensure(main.get("commit", {}).get("sha") == workflow_sha,
           "MAIN_MOVED")
    pr = gh.get(f"repos/{REPOSITORY}/pulls/306")
    ensure(pr.get("state") == "open" and pr.get("draft") is True
           and pr.get("head", {}).get("sha") == APPROVED_SHA
           and pr.get("merged_at") is None, "PERMISSIONS_PR_CHANGED")
    environment = gh.get(f"repos/{REPOSITORY}/environments/production")
    reviews = [
        rule for rule in environment.get("protection_rules", [])
        if rule.get("type") == "required_reviewers" and rule.get("reviewers")
    ]
    ensure(bool(reviews), "ENV_REQUIRED_REVIEWERS_MISSING")
    runs = gh.get(
        f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs?per_page=100"
    )
    history = runs.get("workflow_runs", [])
    ensure(runs.get("total_count", len(history)) <= len(history),
           "RELEASE_HISTORY_TRUNCATED")
    dispatched = [run for run in history
                  if run.get("event") == "workflow_dispatch"]
    ensure(len(dispatched) == 1 and dispatched[0].get("id") == run_id,
           "PRIOR_OR_UNREGISTERED_RELEASE_RUN")
    activity = gh.get(f"repos/{REPOSITORY}/actions/runs?per_page=100")
    for run in activity.get("workflow_runs", []):
        if ("/database-migrate.yml" in run.get("path", "")
                or run.get("name") == "Database Migration"):
            ensure(run.get("status") == "completed",
                   "OTHER_DATABASE_MIGRATION_ACTIVE")
    note = gh.get(f"repos/{REPOSITORY}/issues/comments/{freeze_comment_id}")
    ensure(note.get("issue_url", "").endswith("/issues/306"),
           "FREEZE_COMMENT_NOT_ON_PR306")
    frozen = freeze_contract(
        note.get("body", ""),
        note.get("user", {}).get("login"),
        note.get("created_at", ""),
        workflow_sha=workflow_sha, now=now,
    )
    comments = gh.get(f"repos/{REPOSITORY}/issues/306/comments?per_page=100")
    ensure(isinstance(comments, list) and len(comments) < 100,
           "FREEZE_REVOCATION_HISTORY_UNVERIFIED")
    for later in comments:
        if later.get("created_at", "") > note.get("created_at", ""):
            body = later.get("body") or ""
            if frozen["freeze_id"] in body and (
                "FREEZE CLOSED" in body.upper()
                or "FREEZE REVOKED" in body.upper()
                or '"status": "CLOSED"' in body
            ):
                raise Blocked("FREEZE_CLOSED")
    return frozen


def main():
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
        gh = GitHubReader(os.environ.get("GH_TOKEN", ""))
        freeze = verify_release_controls(
            gh, freeze_comment_id=args.freeze_comment_id,
            workflow_sha=workflow_sha, run_id=run_id, attempt=attempt,
            now=datetime.now(UTC),
        )
        if args.phase == "controls":
            print("PASS: protected environment and owner-attested freeze verified.")
            return
        verify_checkout(args.source)
        url = target_url(os.environ.get("MIGRATION_DATABASE_URL", ""))
        isolated_or_prod_schema(url, revision=FROM_REV, columns=0)
        freeze = verify_release_controls(
            gh, freeze_comment_id=args.freeze_comment_id,
            workflow_sha=workflow_sha, run_id=run_id, attempt=attempt,
            now=datetime.now(UTC),
        )
        safety = TransactionGuard(write=True, expires=utc(freeze["end_utc"]))
        result = perform_upgrade(url, args.source, safety)
        print("PASS: approved 0097 migration and post-schema verified: "
              f"{result['ddl_count']} DDL, {result['version_writes']} version write.")
    except Blocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception:  # noqa: BLE001 - secret-safe fail closed
        # Some psycopg/SQLAlchemy exceptions include URLs and credentials.
        print("BLOCKED: UNEXPECTED_RELEASE_ERROR", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
